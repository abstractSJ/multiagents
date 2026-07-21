"""原项目公司研究流程的 Python 主会话协调器。

把 console / ``/rec`` 原先依赖“Claude 主会话自选 agent”的链路，改成：

1. Python 跑 ``audit_company_research_state`` 得到 ``research_state``；
2. Python 用 ``build_company_plan`` 决定哪些层复用、哪些层补跑；
3. 确定性层（collector / processor / financial draft / market context）调用原项目脚本；
4. LLM 层（正式财务分析 / 估值）通过 ``agent_runtime`` 点名调用注册表 agent，
   由 Claude Code 受限 worker 执行，Python 校验产物；
5. 任一步失败由 Python 停止或降级，模型不能改调度表。

本模块刻意同步实现，便于 CLI 冒烟；console 后续可用 ``asyncio.to_thread`` 接入。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from research_console import config, state_reader, steps
from research_console.agent_runtime import (
    AgentInvocation,
    get_agent,
    run_agent,
    write_json,
    write_placeholder,
)


@dataclass
class CoordinatorStepRecord:
    """主会话单步执行记录。"""

    step_id: str
    owner: str
    kind: str
    status: str
    detail: str = ""
    cmd: list[str] | None = None
    agent_id: str | None = None
    artifacts: list[str] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    tool_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好表示。"""

        return asdict(self)


@dataclass
class CompanyResearchRunResult:
    """整次公司研究主会话结果。"""

    stock_code: str
    as_of_date: str
    workspace: Path
    research_state_path: str | None
    plan: list[dict[str, Any]]
    steps: list[CoordinatorStepRecord] = field(default_factory=list)
    final_status: str = "not_started"
    errors: list[str] = field(default_factory=list)
    research_state: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        """无编排错误且最终状态不是 failed。"""

        return self.final_status in {"completed", "partial", "reused"} and not self.errors

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好表示（不含完整 research_state 大对象时可另存）。"""

        return {
            "stock_code": self.stock_code,
            "as_of_date": self.as_of_date,
            "workspace": str(self.workspace),
            "research_state_path": self.research_state_path,
            "passed": self.passed,
            "final_status": self.final_status,
            "errors": self.errors,
            "plan": self.plan,
            "steps": [item.to_dict() for item in self.steps],
        }


def _run_cmd(
    cmd: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """同步执行原项目脚本；返回 (code, stdout, stderr)。"""

    completed = subprocess.run(
        cmd,
        cwd=str(cwd or config.PROJECT_ROOT),
        env=state_reader.subprocess_env(strip_claude=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def run_research_audit(
    params: dict[str, Any],
    *,
    write_state: bool = True,
    timeout: float | None = None,
) -> dict[str, Any]:
    """调用原 audit 脚本，返回 research_state 字典。"""

    cmd = state_reader.build_audit_command(params, write_state=write_state)
    code, stdout, stderr = _run_cmd(cmd, timeout=timeout or config.AUDIT_TIMEOUT_SECONDS)
    if code != 0:
        raise RuntimeError(f"audit failed (exit {code}): {stderr or stdout}")
    # audit 把 JSON 打到 stdout；容错取最后一个 JSON 对象。
    text = stdout.strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    # 可能混有日志：从最后一个 { 起解析。
    idx = text.rfind("{")
    if idx < 0:
        raise RuntimeError(f"audit produced no JSON: {stdout[:500]}")
    payload = json.loads(text[idx:])
    if not isinstance(payload, dict):
        raise RuntimeError("audit JSON root is not an object")
    return payload


def _layer_artifact_path(state: dict[str, Any], layer: str, key: str) -> Path | None:
    """从 research_state.layers[layer].artifacts[key].path 取路径。"""

    layers = state.get("layers") if isinstance(state, dict) else None
    if not isinstance(layers, dict):
        return None
    layer_obj = layers.get(layer) or {}
    if not isinstance(layer_obj, dict):
        return None
    artifacts = layer_obj.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        return None
    item = artifacts.get(key) or {}
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    if not path:
        return None
    return Path(str(path))


def _existing(path: Path | None) -> Path | None:
    """路径存在则返回，否则 None。"""

    if path is None:
        return None
    return path if path.exists() else None


def _target_fields(state: dict[str, Any], params: dict[str, Any]) -> dict[str, str]:
    """合并 request/target/params 中的身份字段。"""

    target = state.get("target") if isinstance(state.get("target"), dict) else {}
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    stock_code = str(
        params.get("stock_code")
        or target.get("stock_code")
        or request.get("stock_code")
        or ""
    )
    company_name = str(
        params.get("company_name")
        or target.get("company_name")
        or request.get("company_name")
        or ""
    )
    as_of_date = str(
        params.get("as_of_date")
        or request.get("as_of_date")
        or state.get("as_of_date")
        or ""
    )
    report_year = str(
        params.get("report_year")
        or params.get("fiscal_year")
        or target.get("report_year")
        or request.get("report_year")
        or ""
    )
    report_type = str(
        params.get("report_type")
        or target.get("report_type")
        or request.get("report_type")
        or "annual"
    )
    return {
        "stock_code": stock_code,
        "company_name": company_name,
        "as_of_date": as_of_date,
        "report_year": report_year,
        "report_type": report_type,
    }


def _fingerprint(state: dict[str, Any]) -> str:
    """提取 financial_input_fingerprint。"""

    if state.get("financial_input_fingerprint"):
        return str(state["financial_input_fingerprint"])
    draft = (state.get("layers") or {}).get("financial_evidence_draft") or {}
    if isinstance(draft, dict) and draft.get("financial_input_fingerprint"):
        return str(draft["financial_input_fingerprint"])
    formal = (state.get("layers") or {}).get("formal_financial_analysis") or {}
    if isinstance(formal, dict) and formal.get("financial_input_fingerprint"):
        return str(formal["financial_input_fingerprint"])
    return ""


def _append_existing_path(paths: list[Path], raw: Any) -> None:
    """若 raw 是存在的文件路径则加入列表。"""

    if not raw:
        return
    path = Path(str(raw))
    if path.exists() and path.is_file():
        paths.append(path)


def _collect_evidence_paths(state: dict[str, Any]) -> list[Path]:
    """从 research_state / filing_set 收集可选证据路径（存在才纳入）。

    filing_set 真实结构使用 ``source_filings[].paths``，不是顶层 flat keys。
    """

    paths: list[Path] = []
    filing_set = _layer_artifact_path(state, "financial_evidence_draft", "filing_set_json")
    if filing_set and filing_set.exists():
        paths.append(filing_set)
        try:
            payload = json.loads(filing_set.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            _append_existing_path(paths, payload.get("research_state_path"))
            filings = payload.get("source_filings") or payload.get("filings") or []
            if isinstance(filings, list):
                for item in filings:
                    if not isinstance(item, dict):
                        continue
                    nested = item.get("paths") if isinstance(item.get("paths"), dict) else {}
                    # 同时兼容 nested paths 与扁平字段。
                    candidates = [
                        nested.get("content_json"),
                        nested.get("llm_digest_json"),
                        nested.get("digest_audit_json"),
                        nested.get("rag_chunks_jsonl"),
                        nested.get("summary_comparison_json"),
                        nested.get("analyst_report_json"),
                        item.get("llm_digest_json"),
                        item.get("llm_digest_path"),
                        item.get("analyst_report_json"),
                        item.get("analyst_report_path"),
                        item.get("summary_comparison_json"),
                        item.get("summary_comparison_path"),
                        item.get("rag_chunks_jsonl"),
                        item.get("rag_chunks_path"),
                        item.get("content_json"),
                        item.get("content_json_path"),
                        item.get("digest_audit_json"),
                    ]
                    for raw in candidates:
                        _append_existing_path(paths, raw)
    # 单份兼容产物 + 市场上下文 + 正式财务（估值阶段可读）
    for layer, keys in (
        ("processor", ("llm_digest_json", "rag_chunks_jsonl", "summary_comparison_json", "content_json")),
        ("financial_evidence_draft", ("analyst_report_json", "evidence_check_json", "analyst_audit_json", "filing_set_json")),
        ("market_context", ("market_context_package_json", "market_context_sources_json", "collection_audit_json")),
        ("formal_financial_analysis", ("formal_financial_analysis_json", "formal_financial_analysis_md")),
    ):
        for key in keys:
            path = _layer_artifact_path(state, layer, key)
            if path and path.exists():
                paths.append(path)
    # 去重保序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve()).replace("\\", "/").casefold()
        except OSError:
            key = str(path).replace("\\", "/").casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _plan_map(plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """step_id → plan item。"""

    return {str(item["step_id"]): item for item in plan}


def _is_pending(plan_map: dict[str, dict[str, Any]], step_id: str) -> bool:
    """计划中该步是否 pending。"""

    item = plan_map.get(step_id) or {}
    return str(item.get("status") or "") == "pending"


def _record(
    result: CompanyResearchRunResult,
    *,
    step_id: str,
    owner: str,
    kind: str,
    status: str,
    detail: str = "",
    cmd: list[str] | None = None,
    agent_id: str | None = None,
    artifacts: list[str] | None = None,
    validation: dict[str, Any] | None = None,
    tool_names: list[str] | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    input_paths: dict[str, str] | None = None,
    output_paths: dict[str, str] | None = None,
) -> None:
    """追加一步记录，并可选推送控制台步骤事件。"""

    result.steps.append(
        CoordinatorStepRecord(
            step_id=step_id,
            owner=owner,
            kind=kind,
            status=status,
            detail=detail,
            cmd=cmd,
            agent_id=agent_id,
            artifacts=artifacts or [],
            validation=validation,
            tool_names=tool_names or [],
        )
    )
    payload: dict[str, Any] = {
        "summary": detail,
        "kind": kind,
        "artifacts": artifacts or [],
    }
    if cmd is not None:
        payload["cmd"] = steps.cmd_display(cmd)
    if agent_id:
        payload["agent_id"] = agent_id
        payload["execution"] = "python_owned_agent_runtime"
    if tool_names:
        payload["tool_names"] = tool_names
    if validation is not None:
        payload["validation"] = {
            "passed": bool((validation or {}).get("passed", status == "completed")),
            "checks": (validation or {}).get("checks") or {},
            "errors": (validation or {}).get("errors") or [],
        }
    if input_paths:
        payload["input_paths"] = input_paths
    if output_paths:
        payload["output_paths"] = output_paths
    event_type = {
        "completed": "step_completed",
        "failed": "step_failed",
        "skipped": "step_skipped",
        "degraded": "step_completed",
    }.get(status, "step_completed")
    if status == "failed":
        payload["error"] = detail or f"{step_id} failed"
    if status == "skipped":
        payload["reason"] = detail or "skipped"
    if status == "degraded":
        payload["degraded"] = True
    _emit_event(event_sink, event_type, step_id=step_id, owner=owner, payload=payload)
    for art in artifacts or []:
        if art:
            _emit_event(
                event_sink,
                "artifact_created",
                step_id=step_id,
                owner=owner,
                payload={"path": art, "kind": Path(art).suffix.lstrip(".") or "other"},
            )


def _run_script_step(
    result: CompanyResearchRunResult,
    *,
    step_id: str,
    owner: str,
    cmd: list[str],
    timeout: float,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
) -> bool:
    """执行确定性脚本步骤；成功 True。"""

    _emit_event(
        event_sink,
        "step_started",
        step_id=step_id,
        owner=owner,
        payload={"cmd": steps.cmd_display(cmd), "kind": "script"},
    )
    code, stdout, stderr = _run_cmd(cmd, timeout=timeout)
    if code != 0:
        _record(
            result,
            step_id=step_id,
            owner=owner,
            kind="script",
            status="failed",
            detail=(stderr or stdout)[-2000:],
            cmd=cmd,
            event_sink=event_sink,
        )
        result.errors.append(f"{step_id} failed with exit {code}")
        return False
    _record(
        result,
        step_id=step_id,
        owner=owner,
        kind="script",
        status="completed",
        detail="script ok",
        cmd=cmd,
        event_sink=event_sink,
    )
    return True


def _ensure_report_dir_from_state(state: dict[str, Any]) -> Path | None:
    """尽量定位 processor 报告目录（content.json 父目录）。"""

    content = _layer_artifact_path(state, "processor", "content_json")
    if content and content.exists():
        return content.parent
    # filings 列表里找
    filings = state.get("filings")
    if isinstance(filings, list):
        for item in filings:
            if not isinstance(item, dict):
                continue
            for key in ("content_json_path", "content_json", "report_dir"):
                raw = item.get(key)
                if not raw:
                    continue
                path = Path(str(raw))
                if key == "report_dir" and path.exists():
                    return path
                if path.exists():
                    return path.parent
    return None


def _emit_event(
    event_sink: Callable[[dict[str, Any]], None] | None,
    event_type: str,
    *,
    step_id: str | None = None,
    owner: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """向控制台/调用方推送结构化事件（线程安全由调用方保证）。"""

    if event_sink is None:
        return
    event_sink(
        {
            "type": event_type,
            "step_id": step_id,
            "owner": owner,
            "payload": payload or {},
        }
    )


def run_company_research(
    params: dict[str, Any],
    *,
    workspace: Path,
    claude_bin: str = "claude",
    tool_restriction: str = "permission",
    llm_timeout_seconds: int = 1800,
    max_budget_usd: float = 5.0,
    run_scripts: bool = True,
    run_llm_agents: bool = True,
    auto_fallback_tool_mode: bool = True,
    progress: Callable[[str], None] | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
) -> CompanyResearchRunResult:
    """Python 主会话执行原项目公司研究流程（脚本 + 新 agent 运行时）。

    参数：
        params: 与 console/audit 兼容的研究参数
            （stock_code/target/as_of_date/depth/focus/force_refresh/...）。
        workspace: 本次协调器诊断与 MCP 空配置目录（不等于研究工作区）。
        claude_bin: Claude Code 可执行文件。
        tool_restriction: worker 工具限制层。
        llm_timeout_seconds: 单个 LLM agent 超时。
        max_budget_usd: 单个 LLM agent 预算。
        run_scripts: 是否执行确定性脚本（False 时仅对已有产物跑 LLM/审计）。
        run_llm_agents: 是否执行正式财务/估值 agent。
        auto_fallback_tool_mode: request 被网关拒时是否降 permission。
        progress: 可选进度回调（人类可读一行日志）。
        event_sink: 可选结构化事件回调，供 research_console SSE 实时推送；
            事件形如 ``{type, step_id, owner, payload}``，type 对齐控制台契约
            （step_started/step_completed/step_failed/step_skipped/plan_ready 等）。
    返回值：
        CompanyResearchRunResult。
    """

    def log(msg: str) -> None:
        if progress:
            progress(msg)
        _emit_event(
            event_sink,
            "coordinator_message",
            payload={"text": msg, "partial": False},
        )

    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    mcp_config = workspace / "empty_mcp.json"
    write_json(mcp_config, {"mcpServers": {}})

    # 归一化参数
    params = dict(params or {})
    if not params.get("report_year") and params.get("fiscal_year"):
        params["report_year"] = params["fiscal_year"]
    if not params.get("as_of_date"):
        # 无截止日时用今天，与 audit 默认一致。
        from datetime import date as _date

        params["as_of_date"] = _date.today().isoformat()

    result = CompanyResearchRunResult(
        stock_code=str(params.get("stock_code") or params.get("target") or ""),
        as_of_date=str(params.get("as_of_date") or ""),
        workspace=workspace,
        research_state_path=None,
        plan=[],
        final_status="running",
    )

    def record(**kwargs: Any) -> None:
        """绑定 event_sink 的步骤记账包装。"""
        kwargs.setdefault("event_sink", event_sink)
        _record(result, **kwargs)

    # ------------------------------------------------------------------
    # 1) 初始 audit
    # ------------------------------------------------------------------
    log("audit: initial research_state")
    try:
        state = run_research_audit(params, write_state=True)
    except Exception as exc:  # noqa: BLE001 - 主会话要落报告
        result.final_status = "failed"
        result.errors.append(f"initial audit failed: {exc}")
        write_json(workspace / "company_research_report.json", result.to_dict())
        return result

    identity = _target_fields(state, params)
    result.stock_code = identity["stock_code"] or result.stock_code
    result.as_of_date = identity["as_of_date"] or result.as_of_date
    state_path = default_state_path(identity, params)
    result.research_state_path = str(state_path) if state_path else None
    result.research_state = state
    record(
        step_id="audit",
        owner=steps.ORCHESTRATOR,
        kind="script",
        status="completed",
        detail="initial research_state ready",
        artifacts=[str(state_path)] if state_path else [],
    )

    # ------------------------------------------------------------------
    # 2) 计划：完全由 Python / build_company_plan 决定
    # ------------------------------------------------------------------
    force_refresh = bool(params.get("force_refresh"))
    run_market_context = params.get("run_market_context", True) is not False
    plan = steps.build_company_plan(
        state,
        force_refresh=force_refresh,
        llm_mode="claude_cli" if run_llm_agents else "skip",
        run_market_context=run_market_context,
    )
    result.plan = plan
    plan_map = _plan_map(plan)
    write_json(workspace / "plan.json", {"steps": plan})
    log(f"plan ready: {sum(1 for s in plan if s['status']=='pending')} pending steps")
    _emit_event(
        event_sink,
        "plan_ready",
        payload={
            "steps": plan,
            "research_state_path": result.research_state_path,
            "execution_mode": "python_agent_coordinator",
            "display_only": False,
            "trace_mode": "runtime",
        },
    )

    # ------------------------------------------------------------------
    # 3) collector_fetch（确定性脚本）
    # ------------------------------------------------------------------
    if run_scripts and _is_pending(plan_map, "collector_fetch"):
        log("script: collector_fetch")
        stock_code = identity["stock_code"]
        filing_policy = str(
            params.get("filing_policy")
            or (state.get("request") or {}).get("filing_policy")
            or "recent_history"
        )
        ok = True
        if filing_policy == "recent_history":
            items = steps.build_recent_collector_cmds(
                stock_code,
                identity["as_of_date"],
                annual_lookback=int(params.get("annual_lookback") or 2),
            )
            for item in items:
                if not _run_script_step(
                    result,
                    step_id="collector_fetch",
                    owner=steps.INFO_COLLECTOR,
                    cmd=item["cmd"],
                    timeout=config.SCRIPT_TIMEOUT_SECONDS,
                    event_sink=event_sink,
                ):
                    ok = False
                    break
        else:
            cmd = steps.build_collector_cmd(
                stock_code,
                identity["report_type"],
                identity["report_year"],
                cutoff=identity["as_of_date"],
            )
            ok = _run_script_step(
                result,
                step_id="collector_fetch",
                owner=steps.INFO_COLLECTOR,
                cmd=cmd,
                timeout=config.SCRIPT_TIMEOUT_SECONDS,
                    event_sink=event_sink,
                )
        if not ok:
            result.final_status = "failed"
            write_json(workspace / "company_research_report.json", result.to_dict())
            return result
        state = run_research_audit(params, write_state=True)
        result.research_state = state
        identity = _target_fields(state, params)
    elif not _is_pending(plan_map, "collector_fetch"):
        record(
            step_id="collector_fetch",
            owner=steps.INFO_COLLECTOR,
            kind="script",
            status="skipped",
            detail=str((plan_map.get("collector_fetch") or {}).get("reason") or "reused"),
        )

    # ------------------------------------------------------------------
    # 4) processor 子步骤
    # ------------------------------------------------------------------
    # 4) processor sub-steps (per-filing from research_state.filings / next_actions)
    # ------------------------------------------------------------------
    def _filing_pdf_and_content(item: dict[str, Any]) -> tuple[str, str, str, str]:
        """Extract pdf/content/report_type/report_year from one filing entry."""
        paths = item.get("paths") if isinstance(item.get("paths"), dict) else {}
        pdf = str(
            item.get("pdf_path")
            or item.get("local_pdf_path")
            or paths.get("pdf_path")
            or item.get("local_relative_path")
            or ""
        )
        if pdf and not Path(pdf).is_absolute() and not Path(pdf).exists():
            candidate = config.COLLECTOR_WORKSPACE / pdf
            if candidate.exists():
                pdf = str(candidate)
        content = str(
            item.get("content_json_path")
            or item.get("content_json")
            or paths.get("content_json")
            or ""
        )
        return (
            pdf,
            content,
            str(item.get("report_type") or "annual"),
            str(item.get("report_year") or identity["report_year"] or ""),
        )

    filings_list: list[dict[str, Any]] = []
    raw_filings = state.get("filings")
    if isinstance(raw_filings, list):
        filings_list = [item for item in raw_filings if isinstance(item, dict)]
    next_actions = state.get("next_actions") if isinstance(state.get("next_actions"), list) else []
    processor_any_pending = any(
        _is_pending(plan_map, sid)
        for sid in ("processor_parse", "processor_digest", "processor_rag", "processor_compare")
    )

    if run_scripts and processor_any_pending:
        parse_targets: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for action in next_actions:
            if not isinstance(action, dict):
                continue
            step_name = str(action.get("step") or "")
            if "parse" not in step_name:
                continue
            fid = str(action.get("filing_id") or "")
            match = next(
                (
                    item
                    for item in filings_list
                    if str(item.get("filing_id") or "") == fid
                    or (
                        str(item.get("report_type") or "") == str(action.get("report_type") or "")
                        and str(item.get("report_year") or "") == str(action.get("report_year") or "")
                    )
                ),
                None,
            )
            if match is None:
                match = {
                    "filing_id": fid,
                    "report_type": action.get("report_type"),
                    "report_year": action.get("report_year"),
                    "pdf_path": action.get("pdf_path") or action.get("local_pdf_path") or "",
                }
            key = str(match.get("filing_id") or f"{match.get('report_type')}:{match.get('report_year')}")
            if key not in seen_ids:
                seen_ids.add(key)
                parse_targets.append(match)
        if not parse_targets and _is_pending(plan_map, "processor_parse"):
            for item in filings_list:
                _pdf, content, _rt, _ry = _filing_pdf_and_content(item)
                if content and Path(content).exists():
                    continue
                key = str(item.get("filing_id") or f"{item.get('report_type')}:{item.get('report_year')}")
                if key not in seen_ids:
                    seen_ids.add(key)
                    parse_targets.append(item)
        if not parse_targets and _is_pending(plan_map, "processor_parse"):
            parse_targets.append(
                {
                    "report_type": identity["report_type"],
                    "report_year": identity["report_year"],
                    "pdf_path": str((state.get("target") or {}).get("pdf_path") or ""),
                }
            )

        if parse_targets:
            log(f"script: processor_parse ({len(parse_targets)} filings)")
            for item in parse_targets:
                pdf, _content, report_type, report_year = _filing_pdf_and_content(item)
                cmd = steps.build_processor_parse_cmd(
                    identity["stock_code"],
                    report_type,
                    report_year,
                    overwrite=force_refresh,
                    announcement_id=str(item.get("announcement_id") or ""),
                    pdf_path=pdf,
                )
                if not _run_script_step(
                    result,
                    step_id="processor_parse",
                    owner=steps.INFO_PROCESSOR,
                    cmd=cmd,
                    timeout=config.SCRIPT_TIMEOUT_SECONDS,
                    event_sink=event_sink,
                ):
                    log(f"processor_parse degraded for {item.get('filing_id') or report_year}")
            state = run_research_audit(params, write_state=True)
            result.research_state = state
        elif not _is_pending(plan_map, "processor_parse"):
            record(
                step_id="processor_parse",
                owner=steps.INFO_PROCESSOR,
                kind="script",
                status="skipped",
                detail=str((plan_map.get("processor_parse") or {}).get("reason") or "reused"),
            )

        raw_filings = state.get("filings") if isinstance(state.get("filings"), list) else filings_list
        content_paths: list[Path] = []
        for item in raw_filings:
            if not isinstance(item, dict):
                continue
            _pdf, content, _rt, _ry = _filing_pdf_and_content(item)
            if content and Path(content).exists():
                content_paths.append(Path(content))
        layer_content = _layer_artifact_path(state, "processor", "content_json")
        if layer_content and layer_content.exists() and layer_content not in content_paths:
            content_paths.append(layer_content)

        if content_paths:
            log(f"script: processor post-parse on {len(content_paths)} content.json files")
            for content_json in content_paths:
                pipeline_dir = content_json.parent / "digest_pipeline"
                digest_path = content_json.parent / "llm_digest.json"
                need_digest = _is_pending(plan_map, "processor_digest") or not digest_path.exists()
                if need_digest:
                    for cmd in (
                        steps.build_digest_prepare_cmd(str(content_json), overwrite=force_refresh),
                        steps.build_digest_auto_cmd(str(pipeline_dir), overwrite=force_refresh),
                        steps.build_digest_merge_cmd(str(pipeline_dir), allow_partial=True),
                    ):
                        _run_script_step(
                            result,
                            step_id="processor_digest",
                            owner=steps.INFO_PROCESSOR,
                            cmd=cmd,
                            timeout=config.SCRIPT_TIMEOUT_SECONDS,
                            event_sink=event_sink,
                        )
                rag_path = content_json.parent / "rag_index" / "rag_chunks.jsonl"
                need_rag = _is_pending(plan_map, "processor_rag") or not rag_path.exists()
                if need_rag:
                    _run_script_step(
                        result,
                        step_id="processor_rag",
                        owner=steps.INFO_PROCESSOR,
                        cmd=steps.build_rag_cmd(str(content_json), overwrite=force_refresh),
                        timeout=config.SCRIPT_TIMEOUT_SECONDS,
                        event_sink=event_sink,
                    )
                compare_path = content_json.parent / "summary_comparison.json"
                need_compare = _is_pending(plan_map, "processor_compare") or not compare_path.exists()
                if need_compare:
                    cmd = steps.build_compare_cmd(str(content_json))
                    _emit_event(
                        event_sink,
                        "step_started",
                        step_id="processor_compare",
                        owner=steps.INFO_PROCESSOR,
                        payload={"cmd": steps.cmd_display(cmd), "kind": "script"},
                    )
                    code, stdout, stderr = _run_cmd(cmd, timeout=config.SCRIPT_TIMEOUT_SECONDS)
                    record(
                        step_id="processor_compare",
                        owner=steps.INFO_PROCESSOR,
                        kind="script",
                        status="completed" if code == 0 else "degraded",
                        detail=(stderr or stdout)[-1000:],
                        cmd=cmd,
                    )
            state = run_research_audit(params, write_state=True)
            result.research_state = state
        else:
            for step_id in ("processor_digest", "processor_rag", "processor_compare"):
                if not _is_pending(plan_map, step_id):
                    record(
                        step_id=step_id,
                        owner=steps.INFO_PROCESSOR,
                        kind="script",
                        status="skipped",
                        detail=str((plan_map.get(step_id) or {}).get("reason") or "reused"),
                    )
                else:
                    record(
                        step_id=step_id,
                        owner=steps.INFO_PROCESSOR,
                        kind="script",
                        status="skipped",
                        detail="No content.json available after parse",
                    )
    else:
        for step_id in ("processor_parse", "processor_digest", "processor_rag", "processor_compare"):
            if not _is_pending(plan_map, step_id):
                record(
                    step_id=step_id,
                    owner=steps.INFO_PROCESSOR,
                    kind="script",
                    status="skipped",
                    detail=str((plan_map.get(step_id) or {}).get("reason") or "reused"),
                )

    # ------------------------------------------------------------------
    # 5) financial evidence draft / filing_set
    # ------------------------------------------------------------------
    if run_scripts and _is_pending(plan_map, "financial_evidence_draft"):
        log("script: financial_evidence_draft")
        # 优先多期 filing_set；否则单份 report_dir。
        state_path_for_cmd = result.research_state_path or (
            str(state_path) if state_path else ""
        )
        if state_path_for_cmd and Path(state_path_for_cmd).exists():
            cmd = steps.build_filing_set_cmd(state_path_for_cmd)
        else:
            report_dir = _ensure_report_dir_from_state(state)
            if report_dir is None:
                result.errors.append("financial_evidence_draft: no report_dir or research_state path")
                result.final_status = "failed"
                write_json(workspace / "company_research_report.json", result.to_dict())
                return result
            cmd = steps.build_financial_cmd(
                str(report_dir),
                depth=str(params.get("depth") or "standard"),
                focus=str(params.get("focus") or ""),
                allow_incomplete_digest=True,
            )
        if not _run_script_step(
            result,
            step_id="financial_evidence_draft",
            owner=steps.FINANCIAL_ANALYST,
            cmd=cmd,
            timeout=config.SCRIPT_TIMEOUT_SECONDS,
                    event_sink=event_sink,
                ):
            result.final_status = "failed"
            write_json(workspace / "company_research_report.json", result.to_dict())
            return result
        state = run_research_audit(params, write_state=True)
        result.research_state = state
    elif not _is_pending(plan_map, "financial_evidence_draft"):
        record(
            step_id="financial_evidence_draft",
            owner=steps.FINANCIAL_ANALYST,
            kind="script",
            status="skipped",
            detail=str((plan_map.get("financial_evidence_draft") or {}).get("reason") or "reused"),
        )

    # ------------------------------------------------------------------
    # 6) market context（确定性脚本 + Bocha；无 key 时 dry_run）
    # ------------------------------------------------------------------
    if run_scripts and _is_pending(plan_map, "market_context_update") and run_market_context:
        log("script: market_context_update")
        has_key = bool(
            config.MARKET_CONTEXT_LOCAL_CONFIG.exists()
            or (config.BOCHA_KEY_ENV in __import__("os").environ)
        )
        cmd = steps.build_market_context_cmd(
            target=str(params.get("target") or identity["stock_code"]),
            stock_code=identity["stock_code"],
            company_name=identity["company_name"],
            as_of_date=identity["as_of_date"],
            depth=str(params.get("depth") or "standard"),
            focus=str(params.get("focus") or ""),
            freshness=str(params.get("market_context_freshness") or "oneMonth"),
            dry_run=not has_key,
            force_refresh=force_refresh,
            strict_cutoff=True,
        )
        _emit_event(
            event_sink,
            "step_started",
            step_id="market_context_update",
            owner=steps.MARKET_CONTEXT_COLLECTOR,
            payload={"cmd": steps.cmd_display(cmd), "kind": "script"},
        )
        code, stdout, stderr = _run_cmd(cmd, timeout=config.SCRIPT_TIMEOUT_SECONDS)
        status = "completed" if code == 0 else "degraded"
        record(
            step_id="market_context_update",
            owner=steps.MARKET_CONTEXT_COLLECTOR,
            kind="script",
            status=status,
            detail=(stderr or stdout)[-1000:],
            cmd=cmd,
        )
        state = run_research_audit(params, write_state=True)
        result.research_state = state
    elif not _is_pending(plan_map, "market_context_update"):
        record(
            step_id="market_context_update",
            owner=steps.MARKET_CONTEXT_COLLECTOR,
            kind="script",
            status="skipped",
            detail=str((plan_map.get("market_context_update") or {}).get("reason") or "reused"),
        )

    # ------------------------------------------------------------------
    # 7) formal financial analysis — 新 agent 运行时
    # ------------------------------------------------------------------
    identity = _target_fields(state, params)
    fingerprint = _fingerprint(state)
    evidence_paths = _collect_evidence_paths(state)
    filing_set_path = _existing(
        _layer_artifact_path(state, "financial_evidence_draft", "filing_set_json")
    )
    research_state_file = Path(result.research_state_path) if result.research_state_path else None
    if research_state_file is None or not research_state_file.exists():
        # 把内存 state 落到 workspace，供 worker 读取。
        research_state_file = workspace / "research_state.json"
        write_json(research_state_file, state)
        result.research_state_path = str(research_state_file)

    formal_json = _layer_artifact_path(state, "formal_financial_analysis", "formal_financial_analysis_json")
    formal_md = _layer_artifact_path(state, "formal_financial_analysis", "formal_financial_analysis_md")
    if formal_json is None and filing_set_path is not None:
        formal_json = filing_set_path.parent / "formal_financial_analysis.json"
        formal_md = filing_set_path.parent / "formal_financial_analysis.md"

    if run_llm_agents and _is_pending(plan_map, "formal_financial_analysis"):
        log("agent: formal_financial_analyst")
        _emit_event(
            event_sink,
            "step_started",
            step_id="formal_financial_analysis",
            owner=steps.FINANCIAL_ANALYST,
            payload={
                "kind": "llm_agent",
                "agent_id": "formal_financial_analyst",
                "execution": "python_owned_agent_runtime",
            },
        )
        if filing_set_path is None:
            result.errors.append("formal_financial_analysis pending but filing_set.json is missing")
            result.final_status = "failed"
            write_json(workspace / "company_research_report.json", result.to_dict())
            return result
        assert formal_json is not None and formal_md is not None
        for path in (formal_json, formal_md):
            write_placeholder(path)
        # worker cwd = formal 输出目录的父级，便于 no_extra_files 归因到输出文件。
        agent_cwd = formal_json.parent
        agent_cwd.mkdir(parents=True, exist_ok=True)
        # 把 research_state / filing_set 链到 cwd 下的只读副本路径？为简单起见直接用绝对路径，
        # require_no_extra_files 只扫 cwd；输入在 cwd 外只做指纹检查。
        get_agent("formal_financial_analyst")
        agent_result = run_agent(
            AgentInvocation(
                agent_id="formal_financial_analyst",
                cwd=agent_cwd,
                input_paths={
                    "research_state": research_state_file,
                    "filing_set": filing_set_path,
                },
                output_paths={
                    "formal_json": formal_json,
                    "formal_md": formal_md,
                },
                context={
                    "stock_code": identity["stock_code"],
                    "company_name": identity["company_name"],
                    "as_of_date": identity["as_of_date"],
                    "depth": str(params.get("depth") or "standard"),
                    "focus": str(params.get("focus") or ""),
                    "financial_input_fingerprint": fingerprint,
                    "evidence_paths": [str(p) for p in evidence_paths],
                },
                mcp_config=mcp_config,
                claude_bin=claude_bin,
                tool_restriction=tool_restriction,
                timeout_seconds=llm_timeout_seconds,
                max_budget_usd=max_budget_usd,
                precreate_outputs=True,
                readable_paths=tuple(evidence_paths),
                require_all_inputs_read=False,
                require_no_extra_files=True,
            ),
            auto_fallback_tool_mode=auto_fallback_tool_mode,
        )
        record(
            step_id="formal_financial_analysis",
            owner=steps.FINANCIAL_ANALYST,
            kind="llm_agent",
            status="completed" if agent_result.passed else "failed",
            detail="formal_financial_analyst via agent_runtime",
            agent_id="formal_financial_analyst",
            artifacts=[str(formal_json), str(formal_md)],
            validation=agent_result.worker.validation.to_dict(),
            tool_names=list(agent_result.worker.trace.tool_names),
        )
        # 落盘 agent 诊断
        write_json(
            workspace / "formal_financial_agent_report.json",
            agent_result.to_dict(),
        )
        if not agent_result.passed:
            result.errors.append("formal_financial_analyst failed contract validation")
            result.final_status = "failed"
            write_json(workspace / "company_research_report.json", result.to_dict())
            return result
        state = run_research_audit(params, write_state=True)
        result.research_state = state
    elif not _is_pending(plan_map, "formal_financial_analysis"):
        record(
            step_id="formal_financial_analysis",
            owner=steps.FINANCIAL_ANALYST,
            kind="llm_agent",
            status="skipped",
            detail=str((plan_map.get("formal_financial_analysis") or {}).get("reason") or "reused"),
            artifacts=[str(p) for p in (formal_json, formal_md) if p],
        )

    # ------------------------------------------------------------------
    # 8) valuation — 新 agent 运行时
    # ------------------------------------------------------------------
    state = result.research_state or state
    identity = _target_fields(state, params)
    fingerprint = _fingerprint(state)
    formal_json = _existing(
        _layer_artifact_path(state, "formal_financial_analysis", "formal_financial_analysis_json")
    ) or _existing(formal_json)
    market_pkg = _existing(
        _layer_artifact_path(state, "market_context", "market_context_package_json")
    )
    valuation_json = _layer_artifact_path(state, "valuation", "valuation_report_json")
    if valuation_json is None:
        valuation_dir = (
            config.VALUATION_WORKSPACE
            / "reports"
            / identity["stock_code"]
            / identity["as_of_date"]
        )
    else:
        valuation_dir = valuation_json.parent
    valuation_paths = {
        "valuation_report_json": valuation_dir / "valuation_report.json",
        "valuation_report_md": valuation_dir / "valuation_report.md",
        "valuation_evidence_table_json": valuation_dir / "valuation_evidence_table.json",
        "valuation_audit_json": valuation_dir / "valuation_audit.json",
    }

    if run_llm_agents and _is_pending(plan_map, "valuation_update"):
        log("agent: company_valuation_analyst")
        _emit_event(
            event_sink,
            "step_started",
            step_id="valuation_update",
            owner=steps.VALUATION_ANALYST,
            payload={
                "kind": "llm_agent",
                "agent_id": "company_valuation_analyst",
                "execution": "python_owned_agent_runtime",
            },
        )
        if formal_json is None:
            result.errors.append("valuation_update pending but formal_financial_analysis.json is missing")
            result.final_status = "failed"
            write_json(workspace / "company_research_report.json", result.to_dict())
            return result
        valuation_dir.mkdir(parents=True, exist_ok=True)
        for path in valuation_paths.values():
            write_placeholder(path)
        research_state_file = Path(result.research_state_path or workspace / "research_state.json")
        if not research_state_file.exists():
            write_json(research_state_file, state)
        val_evidence = list(_collect_evidence_paths(state))
        if market_pkg is not None:
            val_evidence.append(market_pkg)
        agent_result = run_agent(
            AgentInvocation(
                agent_id="company_valuation_analyst",
                cwd=valuation_dir,
                input_paths={
                    "research_state": research_state_file,
                    "formal_json": formal_json,
                },
                output_paths=valuation_paths,
                context={
                    "stock_code": identity["stock_code"],
                    "company_name": identity["company_name"],
                    "as_of_date": identity["as_of_date"],
                    "financial_input_fingerprint": fingerprint,
                    "evidence_paths": [str(p) for p in val_evidence],
                },
                mcp_config=mcp_config,
                claude_bin=claude_bin,
                tool_restriction=tool_restriction,
                timeout_seconds=llm_timeout_seconds,
                max_budget_usd=max_budget_usd,
                precreate_outputs=True,
                readable_paths=tuple(val_evidence),
                require_all_inputs_read=False,
                require_no_extra_files=True,
            ),
            auto_fallback_tool_mode=auto_fallback_tool_mode,
        )
        record(
            step_id="valuation_update",
            owner=steps.VALUATION_ANALYST,
            kind="llm_agent",
            status="completed" if agent_result.passed else "failed",
            detail="company_valuation_analyst via agent_runtime",
            agent_id="company_valuation_analyst",
            artifacts=[str(p) for p in valuation_paths.values()],
            validation=agent_result.worker.validation.to_dict(),
            tool_names=list(agent_result.worker.trace.tool_names),
        )
        write_json(workspace / "valuation_agent_report.json", agent_result.to_dict())
        if not agent_result.passed:
            result.errors.append("company_valuation_analyst failed contract validation")
            result.final_status = "failed"
            write_json(workspace / "company_research_report.json", result.to_dict())
            return result
        state = run_research_audit(params, write_state=True)
        result.research_state = state
    elif not _is_pending(plan_map, "valuation_update"):
        record(
            step_id="valuation_update",
            owner=steps.VALUATION_ANALYST,
            kind="llm_agent",
            status="skipped",
            detail=str((plan_map.get("valuation_update") or {}).get("reason") or "reused"),
            artifacts=[str(p) for p in valuation_paths.values()],
        )

    # ------------------------------------------------------------------
    # 9) final audit + 交付状态
    # ------------------------------------------------------------------
    log("audit: final research_state")
    try:
        state = run_research_audit(params, write_state=True)
        result.research_state = state
        record(
            step_id="final_audit",
            owner=steps.ORCHESTRATOR,
            kind="script",
            status="completed",
            detail="final research_state refreshed",
            artifacts=[result.research_state_path or ""],
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"final audit failed: {exc}")
        record(
            step_id="final_audit",
            owner=steps.ORCHESTRATOR,
            kind="script",
            status="failed",
            detail=str(exc),
        )

    layers = (state or {}).get("layers") or {}
    formal_status = str((layers.get("formal_financial_analysis") or {}).get("status") or "")
    valuation_status = str((layers.get("valuation") or {}).get("status") or "")
    # 若除 audit/final_audit/deliver 外没有任何实际执行（脚本或 agent 均 skipped），
    # 说明完全命中 reusable，标记 reused；否则按层状态给出 completed/partial。
    work_steps = [
        s
        for s in result.steps
        if s.step_id not in {"audit", "final_audit", "deliver"}
    ]
    all_skipped = bool(work_steps) and all(s.status == "skipped" for s in work_steps)
    if result.errors:
        result.final_status = "failed"
    elif all_skipped:
        result.final_status = "reused"
    elif formal_status == "ready" and valuation_status == "ready":
        result.final_status = "completed"
    elif any(s.status == "completed" for s in result.steps if s.kind in {"llm_agent", "script"}):
        result.final_status = "partial" if valuation_status != "ready" else "completed"
    else:
        result.final_status = "partial"

    record(
        step_id="deliver",
        owner=steps.ORCHESTRATOR,
        kind="synthetic",
        status=result.final_status,
        detail=(
            f"formal={formal_status or 'n/a'}; valuation={valuation_status or 'n/a'}; "
            f"coordinator=python_agent_runtime"
        ),
    )
    write_json(workspace / "company_research_report.json", result.to_dict())
    if result.research_state is not None:
        write_json(workspace / "final_research_state.json", result.research_state)
    log(f"done: {result.final_status}")
    return result


def default_state_path(identity: dict[str, str], params: dict[str, Any]) -> Path | None:
    """推导 research_state 默认落盘路径。"""

    stock = identity.get("stock_code") or ""
    as_of = identity.get("as_of_date") or str(params.get("as_of_date") or "")
    year = identity.get("report_year") or str(params.get("report_year") or "")
    if not stock:
        return None
    # recent_history 用 as_of_date 子目录；single 常用 report_year。
    if as_of and (Path(config.ORCHESTRATOR_WORKSPACE) / "company_state" / stock / as_of / "research_state.json").exists():
        return Path(config.ORCHESTRATOR_WORKSPACE) / "company_state" / stock / as_of / "research_state.json"
    if year and (Path(config.ORCHESTRATOR_WORKSPACE) / "company_state" / stock / year / "research_state.json").exists():
        return Path(config.ORCHESTRATOR_WORKSPACE) / "company_state" / stock / year / "research_state.json"
    if as_of:
        return Path(config.ORCHESTRATOR_WORKSPACE) / "company_state" / stock / as_of / "research_state.json"
    if year:
        return Path(config.ORCHESTRATOR_WORKSPACE) / "company_state" / stock / year / "research_state.json"
    return Path(config.ORCHESTRATOR_WORKSPACE) / "company_state" / stock / "research_state.json"
