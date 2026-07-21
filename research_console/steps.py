"""公司/行业链路步骤定义。

功能：
- 冻结的 step_id / owner / kind / 依赖关系定义（与契约逐字一致）；
- 由 research_state 构建执行计划（复用层映射为 skipped，缺口层映射为 pending）；
- 财报披露窗口推导（collector_fetch 的查询窗口）；
- 各确定性步骤的命令构建器（全部返回 subprocess 参数列表，不经 shell）；
- LLM 步骤的提示词模板（manual 与 claude_cli 共用）。

本模块是纯函数集合，不做 IO、不起子进程，便于单元测试。
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_console import config
from research_orchestrator_scripts.recent_filing_policy import derive_recent_filing_plan

# 角色名（与 .claude/agents 与契约的 owner 完全一致）
ORCHESTRATOR = "orchestrator"
INFO_COLLECTOR = "information-collector"
INFO_PROCESSOR = "information-processor"
FINANCIAL_ANALYST = "financial-analyst"
VALUATION_ANALYST = "valuation-analyst"
MARKET_CONTEXT_COLLECTOR = "market-context-collector"
INDUSTRY_INFO_COLLECTOR = "industry-info-collector"
INDUSTRY_RESEARCHER = "industry-researcher"

PROCESSOR_SUBSTEPS = ("processor_parse", "processor_digest", "processor_rag", "processor_compare")


@dataclass(frozen=True)
class StepDef:
    """单个步骤的静态定义。

    参数：
        step_id: 冻结的步骤标识。
        owner: 承担角色（决定前端小人）。
        kind: script（确定性脚本）、llm（等待产物落盘）或 synthetic（后端合成）。
        title: 前端展示标题。
        layer: 对应 research_state 层名；无对应层（audit/deliver 等）为 None。
    返回值：
        dataclass 实例。
    """

    step_id: str
    owner: str
    kind: str
    title: str
    layer: str | None = None


# 公司链路步骤（顺序即主线依赖顺序；market_context_update 与主线并行，汇入 valuation_update）
COMPANY_STEP_DEFS: list[StepDef] = [
    StepDef("audit", ORCHESTRATOR, "script", "Audit Research State", None),
    StepDef("collector_fetch", INFO_COLLECTOR, "script", "Collect Financial Filings", "collector"),
    StepDef("processor_parse", INFO_PROCESSOR, "script", "Parse PDF", "processor"),
    StepDef("processor_digest", INFO_PROCESSOR, "script", "Build LLM Digest", "processor"),
    StepDef("processor_rag", INFO_PROCESSOR, "script", "Build RAG Index", "processor"),
    StepDef("processor_compare", INFO_PROCESSOR, "script", "Cross-Check Summaries", "processor"),
    StepDef("financial_evidence_draft", FINANCIAL_ANALYST, "script", "Draft Financial Evidence", "financial_evidence_draft"),
    StepDef("formal_financial_analysis", FINANCIAL_ANALYST, "llm", "Complete Financial Analysis", "formal_financial_analysis"),
    StepDef("market_context_update", MARKET_CONTEXT_COLLECTOR, "script", "Collect Market Context", "market_context"),
    StepDef("valuation_update", VALUATION_ANALYST, "llm", "Update Valuation", "valuation"),
    StepDef("final_audit", ORCHESTRATOR, "script", "Run Final State Audit", None),
    StepDef("deliver", ORCHESTRATOR, "synthetic", "Deliver Conclusion", None),
]

# 行业链路步骤
INDUSTRY_STEP_DEFS: list[StepDef] = [
    StepDef("industry_collect", INDUSTRY_INFO_COLLECTOR, "script", "Collect Industry Input Package", None),
    StepDef("industry_validate", INDUSTRY_INFO_COLLECTOR, "script", "Validate Industry Package", None),
    StepDef("industry_research", INDUSTRY_RESEARCHER, "llm", "Complete Industry Research", None),
    StepDef("industry_deliver", ORCHESTRATOR, "synthetic", "Deliver Industry Conclusion", None),
]

COMPANY_STEP_MAP = {item.step_id: item for item in COMPANY_STEP_DEFS}
INDUSTRY_STEP_MAP = {item.step_id: item for item in INDUSTRY_STEP_DEFS}

SKIP_REASON_REUSE = "Reused existing artifacts"
SKIP_REASON_LLM_MODE = "llm_mode=skip; skipped the LLM step and downgraded the delivery"
SKIP_REASON_MARKET_OFF = "run_market_context=false; skipped market-context collection"


# ---------------------------------------------------------------------------
# 披露窗口推导
# ---------------------------------------------------------------------------

def _strict_cutoff_date(value: _dt.date | str) -> _dt.date:
    """把显式 cutoff 解析为严格 ``YYYY-MM-DD`` 日期。

    参数：
        value: date 对象或十位 ISO 日期字符串。
    返回值：
        标准 date。
    """
    if isinstance(value, _dt.datetime):
        raise ValueError("cutoff must be a date or strict ISO date in YYYY-MM-DD format; time values are not allowed")
    if isinstance(value, _dt.date):
        return value
    text = str(value or "").strip()
    if len(text) != 10 or text[4:5] != "-" or text[7:8] != "-":
        raise ValueError(f"cutoff must be a strict ISO date in YYYY-MM-DD format; received: {text or '<empty>'}")
    try:
        parsed = _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cutoff is not a valid date: {text}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"cutoff must be a strict ISO date in YYYY-MM-DD format; received: {text}")
    return parsed


def derive_disclosure_window(
    report_type: str,
    report_year: str | int,
    today: _dt.date | None = None,
    cutoff: _dt.date | str | None = None,
) -> tuple[str, str]:
    """由财年与报告类型推导巨潮披露日期查询窗口。

    功能：
        采集脚本的 start/end 是"披露日期"窗口而不是财报期间：
        - annual FYy：(y+1)-01-01 .. (y+1)-08-31（年报次年上半年披露）；
        - q1 y：y-04-01 .. y-08-31；
        - semiannual y：y-07-01 .. y-12-31；
        - q3 y：y-10-01 .. y-12-31。
        end 一律不晚于今天；显式提供 cutoff 时还必须不晚于知识截止日。
        窗口尚未开始（start 在有效上限之后）时把 start 收拢到 end，避免向数据源
        发送倒挂或未来区间。保留 today 为第三个位置参数，兼容既有调用。
    参数：
        report_type: annual/q1/semiannual/q3。
        report_year: 财报所属年度。
        today: 当前日期上限，默认取系统日期（测试可注入固定日期）。
        cutoff: 可选知识截止日，接受 date 或严格 ``YYYY-MM-DD`` 字符串。
    返回值：
        (start, end) ISO 日期字符串二元组。
    """
    year = int(str(report_year))
    today = today or _dt.date.today()
    cutoff_date = _strict_cutoff_date(cutoff) if cutoff is not None else None
    rtype = str(report_type or "annual").strip().lower()
    if rtype == "annual":
        start = _dt.date(year + 1, 1, 1)
        end = _dt.date(year + 1, 8, 31)
    elif rtype == "q1":
        start = _dt.date(year, 4, 1)
        end = _dt.date(year, 8, 31)
    elif rtype == "semiannual":
        start = _dt.date(year, 7, 1)
        end = _dt.date(year, 12, 31)
    elif rtype == "q3":
        start = _dt.date(year, 10, 1)
        end = _dt.date(year, 12, 31)
    else:
        # 未知类型按全年披露窗口兜底，保证采集步骤仍可执行。
        start = _dt.date(year, 1, 1)
        end = _dt.date(year + 1, 8, 31)
    # today 防止请求现实中的未来日期；cutoff 防止历史回测读取基准日后的披露。
    effective_end = min(today, cutoff_date) if cutoff_date is not None else today
    end = min(end, effective_end)
    if start > end:
        start = end
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# 计划构建
# ---------------------------------------------------------------------------

def _plan_step(step: StepDef, status: str, skip_reason: str | None = None) -> dict[str, Any]:
    """构造 plan_ready.steps 里的单个条目。

    参数：
        step: 步骤定义。
        status: "pending" 或 "skipped"。
        skip_reason: skipped 时的原因。
    返回值：
        计划条目字典。
    """
    entry: dict[str, Any] = {
        "step_id": step.step_id,
        "owner": step.owner,
        "kind": step.kind,
        "title": step.title,
        "status": status,
    }
    if step.layer:
        entry["layer"] = step.layer
    if skip_reason:
        entry["skip_reason"] = skip_reason
    return entry


def _processor_substep_pending(processor_layer: dict[str, Any]) -> dict[str, bool]:
    """把 processor 层状态细化到四个子步骤。

    功能：
        依据 quality_flags.missing_required_artifacts 与产物存在性判断
        parse/digest/rag/compare 哪些需要补跑：
        - content_json 缺失时四步全部重跑（后续产物都依赖解析结果）；
        - 否则按缺口精确补跑；digest 的 chunk 缺失/无效也触发 digest 重跑；
        - partial 但识别不出标准缺口时保守全跑，宁可多跑不可漏跑。
    参数：
        processor_layer: research_state.layers.processor。
    返回值：
        {step_id: 是否 pending} 字典。
    """
    artifacts = processor_layer.get("artifacts", {}) or {}
    flags = processor_layer.get("quality_flags", {}) or {}
    missing = set(flags.get("missing_required_artifacts") or [])
    digest_broken = bool(flags.get("missing_digest_chunks") or flags.get("invalid_digest_results"))

    def exists(key: str) -> bool:
        info = artifacts.get(key, {})
        return bool(isinstance(info, dict) and info.get("exists"))

    if "content_json" in missing or not exists("content_json"):
        return {step: True for step in PROCESSOR_SUBSTEPS}
    pending = {
        "processor_parse": False,
        "processor_digest": ("llm_digest_json" in missing or "digest_audit_json" in missing or digest_broken),
        "processor_rag": "rag_chunks_jsonl" in missing,
        "processor_compare": "summary_comparison_json" in missing,
    }
    if not any(pending.values()):
        # partial 却无标准缺口：保守全跑（对应 audit 的 processor_inspect 情况）。
        return {step: True for step in PROCESSOR_SUBSTEPS}
    return pending


def build_company_plan(
    research_state: dict[str, Any],
    force_refresh: bool = False,
    llm_mode: str = "manual",
    run_market_context: bool = True,
) -> list[dict[str, Any]]:
    """由 research_state 构建公司链路执行计划。

    功能：
        - reusable=true 的层对应步骤标记 skipped（复用已有产物）；
        - missing/partial/stale/incompatible 的层进入 pending；
        - processor 为 partial 时按缺口细化到四个子步骤；
        - force_refresh=true 时全部 pending；
        - llm_mode=skip 时 LLM 步骤标记 skipped（交付降级）；
        - run_market_context=false 时市场上下文步骤标记 skipped。
        audit/final_audit/deliver 属于编排器步骤，始终 pending。
    参数：
        research_state: audit 输出的研究状态。
        force_refresh: 是否强制全链路执行。
        llm_mode: manual/claude_cli/skip。
        run_market_context: 是否执行市场上下文采集。
    返回值：
        plan_ready.steps 列表。
    """
    reusable = research_state.get("reusable", {}) if isinstance(research_state, dict) else {}
    layers = research_state.get("layers", {}) if isinstance(research_state, dict) else {}
    plan: list[dict[str, Any]] = []
    processor_layer = layers.get("processor", {}) or {}
    processor_reusable = bool(reusable.get("processor")) and not force_refresh
    processor_pending = None if processor_reusable else _processor_substep_pending(processor_layer)

    for step in COMPANY_STEP_DEFS:
        # llm_mode=skip 优先级最高：即使层可复用也无妨（复用同样是 skipped，语义以 llm_mode 标注）。
        if step.kind == "llm" and llm_mode == "skip" and not (reusable.get(step.layer) and not force_refresh):
            plan.append(_plan_step(step, "skipped", SKIP_REASON_LLM_MODE))
            continue
        if step.step_id == "market_context_update" and not run_market_context:
            plan.append(_plan_step(step, "skipped", SKIP_REASON_MARKET_OFF))
            continue
        if step.layer is None or force_refresh:
            plan.append(_plan_step(step, "pending"))
            continue
        if step.layer == "processor":
            if processor_reusable:
                plan.append(_plan_step(step, "skipped", SKIP_REASON_REUSE))
            elif processor_pending is not None and not processor_pending.get(step.step_id, True):
                plan.append(_plan_step(step, "skipped", SKIP_REASON_REUSE))
            else:
                plan.append(_plan_step(step, "pending"))
            continue
        if reusable.get(step.layer):
            plan.append(_plan_step(step, "skipped", SKIP_REASON_REUSE))
        else:
            plan.append(_plan_step(step, "pending"))
    return plan


def build_industry_plan(llm_mode: str = "manual") -> list[dict[str, Any]]:
    """构建行业链路执行计划。

    功能：
        行业链路当前没有 research_state 复用审计，四步默认全 pending；
        llm_mode=skip 时行业研究 LLM 步骤标记 skipped。
    参数：
        llm_mode: manual/claude_cli/skip。
    返回值：
        plan_ready.steps 列表。
    """
    plan: list[dict[str, Any]] = []
    for step in INDUSTRY_STEP_DEFS:
        if step.kind == "llm" and llm_mode == "skip":
            plan.append(_plan_step(step, "skipped", SKIP_REASON_LLM_MODE))
        else:
            plan.append(_plan_step(step, "pending"))
    return plan


# ---------------------------------------------------------------------------
# 命令构建器（全部返回参数列表；路径参数使用绝对路径字符串）
# ---------------------------------------------------------------------------

def build_collector_cmd(
    stock_code: str,
    report_type: str,
    report_year: str,
    today: _dt.date | None = None,
    cutoff: _dt.date | str | None = None,
) -> list[str]:
    """构建财报采集下载命令。

    参数：
        stock_code: 股票代码（作为查询关键字）。
        report_type: 报告类型。
        report_year: 财报年度。
        today: 当前日期上限；保留该位置参数以兼容旧调用。
        cutoff: 可选知识截止日，传入后命令的 ``--end-date`` 绝不晚于该日。
    返回值：
        run_cninfo_collection.py 命令参数列表。
    """
    start, end = derive_disclosure_window(report_type, report_year, today, cutoff=cutoff)
    return [
        sys.executable,
        str(config.CNINFO_SCRIPT),
        "--start-date",
        start,
        "--end-date",
        end,
        "--report-types",
        str(report_type or "annual"),
        "--keyword",
        str(stock_code),
        "--download",
    ]


def build_recent_collector_cmds(
    stock_code: str,
    as_of_date: str,
    *,
    annual_lookback: int = 2,
) -> list[dict[str, Any]]:
    """为近期财报集合构建逐窗口采集命令。

    每条命令仍调用现有 collector，因此公告去重、修订版保留和下载状态合并均沿用
    原实现；这里只负责把多期策略展开为可审计的公司级小窗口。
    """
    result: list[dict[str, Any]] = []
    for item in derive_recent_filing_plan(as_of_date, annual_lookback=annual_lookback):
        result.append(
            {
                **item.to_dict(),
                "cmd": [
                    sys.executable,
                    str(config.CNINFO_SCRIPT),
                    "--start-date",
                    item.disclosure_start,
                    "--end-date",
                    item.disclosure_end,
                    "--report-types",
                    item.report_type,
                    "--keyword",
                    str(stock_code),
                    "--download",
                ],
            }
        )
    return result


def build_processor_parse_cmd(
    stock_code: str,
    report_type: str,
    report_year: str,
    overwrite: bool = False,
    announcement_id: str = "",
    pdf_path: str = "",
) -> list[str]:
    """构建 PDF 解析命令。

    参数：
        stock_code: 股票代码。
        report_type: 报告类型。
        report_year: 财报年度。
        overwrite: force_refresh 时覆盖已有 content.json。
        announcement_id: manifest 有公告 ID 时精确过滤。
        pdf_path: 公告 ID 缺失时使用精确 PDF 路径，优先级高于宽泛筛选。
    返回值：
        run_pdf_processing.py 命令参数列表。
    """
    if pdf_path:
        cmd = [sys.executable, str(config.PDF_PROCESS_SCRIPT), "--pdf", str(pdf_path)]
    else:
        cmd = [
            sys.executable,
            str(config.PDF_PROCESS_SCRIPT),
            "--stock-code",
            str(stock_code),
            "--report-type",
            str(report_type or "annual"),
            "--report-year",
            str(report_year),
        ]
        if announcement_id:
            cmd.extend(["--announcement-id", str(announcement_id)])
    if overwrite:
        cmd.append("--overwrite")
    return cmd


def build_digest_prepare_cmd(content_json: str, overwrite: bool = False) -> list[str]:
    """构建 digest prepare 命令。

    参数：
        content_json: content.json 绝对路径。
        overwrite: 是否覆盖已有 chunk、prompt 和 manifest。
    返回值：
        build_llm_digest.py prepare 命令参数列表。
    """
    cmd = [sys.executable, str(config.DIGEST_SCRIPT), "prepare", "--content-json", str(content_json)]
    if overwrite:
        cmd.append("--overwrite")
    return cmd


def build_digest_auto_cmd(pipeline_dir: str, overwrite: bool = False) -> list[str]:
    """构建 auto-digest 命令（确定性规则摘要器，无外部 LLM 调用）。

    参数：
        pipeline_dir: digest_pipeline 目录绝对路径。
        overwrite: 是否覆盖已有 agent_results。
    返回值：
        build_llm_digest.py auto-digest 命令参数列表。
    """
    cmd = [sys.executable, str(config.DIGEST_SCRIPT), "auto-digest", "--pipeline-dir", str(pipeline_dir)]
    if overwrite:
        cmd.append("--overwrite")
    return cmd


def build_digest_merge_cmd(pipeline_dir: str, allow_partial: bool = False) -> list[str]:
    """构建 digest merge 命令。

    参数：
        pipeline_dir: digest_pipeline 目录绝对路径。
        allow_partial: 缺 chunk 时是否允许生成不完整 digest（重试兜底用）。
    返回值：
        build_llm_digest.py merge 命令参数列表。
    """
    cmd = [sys.executable, str(config.DIGEST_SCRIPT), "merge", "--pipeline-dir", str(pipeline_dir)]
    if allow_partial:
        cmd.append("--allow-partial")
    return cmd


def build_rag_cmd(content_json: str, overwrite: bool = False) -> list[str]:
    """构建 RAG 索引构建命令。

    参数：
        content_json: content.json 绝对路径。
        overwrite: 是否覆盖已有索引。
    返回值：
        build_report_rag_index.py build 命令参数列表。
    """
    cmd = [sys.executable, str(config.RAG_SCRIPT), "build", "--content-json", str(content_json)]
    if overwrite:
        cmd.append("--overwrite")
    return cmd


def build_compare_cmd(content_json: str) -> list[str]:
    """构建摘要交叉比对命令（脚本自动从 collector 清单定位摘要 PDF）。

    参数：
        content_json: content.json 绝对路径。
    返回值：
        compare_digest_with_summary.py 命令参数列表。
    """
    return [sys.executable, str(config.COMPARE_SCRIPT), "--content-json", str(content_json)]


def build_financial_cmd(
    report_dir: str, depth: str = "standard", focus: str = "", allow_incomplete_digest: bool = False
) -> list[str]:
    """构建财务证据草稿命令。

    参数：
        report_dir: 信息处理员单份报告目录。
        depth: 分析深度。
        focus: 重点方向（可空）。
        allow_incomplete_digest: digest_audit.complete=false 时必须开启，否则脚本抛错。
    返回值：
        run_financial_analysis.py 命令参数列表。
    """
    cmd = [
        sys.executable,
        str(config.FINANCIAL_SCRIPT),
        "--report-dir",
        str(report_dir),
        "--analysis-depth",
        str(depth or "standard"),
    ]
    if focus:
        cmd.extend(["--focus", str(focus)])
    if allow_incomplete_digest:
        cmd.append("--allow-incomplete-digest")
    return cmd


def build_filing_set_cmd(research_state_path: str) -> list[str]:
    """构建多期财报交接包命令。"""
    return [
        sys.executable,
        str(config.FINANCIAL_SCRIPT),
        "--research-state",
        str(research_state_path),
    ]


def build_market_context_cmd(
    target: str,
    stock_code: str,
    company_name: str,
    as_of_date: str,
    depth: str = "standard",
    focus: str = "",
    freshness: str = "oneMonth",
    dry_run: bool = False,
    force_refresh: bool = False,
    strict_cutoff: bool = False,
) -> list[str]:
    """构建市场上下文采集命令。

    参数：
        target: 公司名或代码。
        stock_code: 股票代码。
        company_name: 公司名。
        as_of_date: 观察日。
        depth: 采集深度。
        focus: 关注点。
        freshness: Bocha freshness 参数。
        dry_run: 无 API key 时置 True，只生成查询计划与空包（交付降级）。
        force_refresh: 是否忽略查询缓存。
        strict_cutoff: 是否把 as_of_date 作为网页来源硬截止日。
    返回值：
        run_market_context_collection.py 命令参数列表。
    """
    cmd = [
        sys.executable,
        str(config.MARKET_CONTEXT_SCRIPT),
        "--target",
        str(target or stock_code or company_name),
        "--as-of-date",
        str(as_of_date),
        "--depth",
        str(depth or "standard"),
        "--freshness",
        str(freshness or "oneMonth"),
    ]
    if stock_code:
        cmd.extend(["--stock-code", str(stock_code)])
    if company_name:
        cmd.extend(["--company-name", str(company_name)])
    if focus:
        cmd.extend(["--focus", str(focus)])
    if dry_run:
        cmd.append("--dry-run")
    if force_refresh:
        cmd.append("--force-refresh")
    if strict_cutoff:
        cmd.append("--strict-cutoff")
    return cmd


# 行业主题模式的可选事件参数：params 键 → 命令行旗标。
_INDUSTRY_EVENT_FLAGS = [
    ("event_name", "--event-name"),
    ("event_type", "--event-type"),
    ("event_description", "--event-description"),
    ("event_start_date", "--event-start-date"),
    ("event_end_date", "--event-end-date"),
    ("event_status", "--event-status"),
    ("event_window", "--event-window"),
    ("baseline_period", "--baseline-period"),
    ("impact_variables", "--impact-variables"),
    ("pricing_variable", "--pricing-variable"),
    ("affected_segments", "--affected-segments"),
    ("geography_scope", "--geography-scope"),
    ("counterfactual_assumption", "--counterfactual-assumption"),
]


def build_industry_collect_cmd(params: dict[str, Any]) -> list[str]:
    """构建行业输入包收集命令。

    功能：
        - 公司验证模式：给了 stock_code 时用 --stock-code/--company-name/--fiscal-year；
        - 主题模式：用 --target/--industry-name/--deliverable-type + 可选事件参数；
        两种模式都带 --as-of-date。
    参数：
        params: 行业 run 参数字典。
    返回值：
        run_industry_collection.py 命令参数列表。
    """
    as_of_date = str(params.get("as_of_date") or _dt.date.today().isoformat())
    cmd = [sys.executable, str(config.INDUSTRY_COLLECT_SCRIPT), "--as-of-date", as_of_date]
    stock_code = str(params.get("stock_code") or "").strip()
    if stock_code:
        cmd.extend(["--stock-code", stock_code])
        if params.get("company_name"):
            cmd.extend(["--company-name", str(params["company_name"])])
        if params.get("fiscal_year"):
            cmd.extend(["--fiscal-year", str(params["fiscal_year"])])
    elif params.get("target"):
        cmd.extend(["--target", str(params["target"])])
    if params.get("industry_name"):
        cmd.extend(["--industry-name", str(params["industry_name"])])
    deliverable = str(params.get("deliverable_type") or "").strip()
    if deliverable:
        cmd.extend(["--deliverable-type", deliverable])
    for key, flag in _INDUSTRY_EVENT_FLAGS:
        raw_value = params.get(key)
        if isinstance(raw_value, (list, tuple)):
            value = ",".join(str(item).strip() for item in raw_value if str(item).strip())
        else:
            value = str(raw_value or "").strip()
        if value:
            cmd.extend([flag, value])
    return cmd


def build_industry_validate_cmd(package_json: str, deliverable_type: str = "") -> list[str]:
    """构建行业包校验命令。

    参数：
        package_json: industry_input_package.json 绝对路径。
        deliverable_type: 显式交付类型（可空，脚本会自动推断）。
    返回值：
        validate_industry_package.py 命令参数列表。
    """
    cmd = [sys.executable, str(config.INDUSTRY_VALIDATE_SCRIPT), "--package", str(package_json)]
    if deliverable_type:
        cmd.extend(["--deliverable-type", str(deliverable_type)])
    return cmd


def cmd_display(cmd: list[str]) -> str:
    """把命令参数列表转成可读展示字符串。

    功能：
        仅用于 step_started.payload.cmd 展示；含空格的参数加引号，
        不用于真实执行（执行始终走参数列表）。
    参数：
        cmd: 命令参数列表。
    返回值：
        展示字符串。
    """
    parts = []
    for item in cmd:
        text = str(item)
        parts.append(f'"{text}"' if (" " in text or "\t" in text) else text)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# LLM 提示词模板（manual 与 claude_cli 共用）
# ---------------------------------------------------------------------------
# 提示词保持单行、不含英文双引号，保证既可以复制到 Claude Code，
# 也可以安全地作为 claude CLI 的单个命令行参数在 Windows 上传递。


def _single_line(text: str) -> str:
    """把提示词压成单行。

    参数：
        text: 原始文本。
    返回值：
        去掉换行与多余空白的单行文本。
    """
    return " ".join(text.split())


def format_claude_cmd(prompt: str) -> str:
    """生成可复制的 claude CLI 命令展示串。

    参数：
        prompt: 单行提示词。
    返回值：
        形如 claude -p "<提示词>" --permission-mode acceptEdits 的展示字符串。
    """
    return f'claude -p "{prompt}" --permission-mode acceptEdits'


def build_formal_financial_analysis_prompt(ctx: dict[str, Any]) -> dict[str, Any]:
    """生成正式财务分析（LLM 步骤）的提示词与期望产物。

    参数：
        ctx: 含 company_name/stock_code/report_year/report_dir/llm_digest_path/
             rag_chunks_path/summary_comparison_path/analyst_report_path/
             analyst_dir/formal_dir/as_of_date/source_report_published_at/depth/focus 的上下文字典。
    返回值：
        {instructions, prompt, expected_artifacts} 字典，expected_artifacts 为绝对路径字符串列表。
    """
    analyst_dir = str(ctx.get("analyst_dir") or "")
    formal_dir = str(ctx.get("formal_dir") or analyst_dir)
    expected = [
        str(Path(formal_dir) / "formal_financial_analysis.json"),
        str(Path(formal_dir) / "formal_financial_analysis.md"),
    ]
    focus_text = str(ctx.get("focus") or "No specific focus")
    filing_set_path = str(ctx.get("filing_set_path") or "")
    if filing_set_path:
        evidence_intro = (
            f"Use the cutoff-safe multi-period filing-set handoff {filing_set_path}. "
            f"Its financial_input_fingerprint is {ctx.get('financial_input_fingerprint') or ''}. "
            "Read every source filing listed in that handoff using its exact digest/RAG/content paths; compare the two annual baselines, all available current/prior-year interim periods, revisions, and like-for-like periods. "
            "Treat Q1, semiannual, and Q3 flow values as 3M/6M/9M cumulative. Derive standalone quarters only when metric definition, unit, scope, and restatement basis match, and never add cumulative periods together. "
            "Prefix every page/chunk citation with the filing_id/source_ref_prefix so evidence from different documents cannot collide. "
        )
    else:
        evidence_intro = (
            f"Use the {ctx.get('report_year') or ''} {ctx.get('report_type') or 'annual'} filing. "
            f"Evidence inputs: processed-report directory {ctx.get('report_dir') or ''}; "
            f"LLM digest {ctx.get('llm_digest_path') or ''}; "
            f"RAG index {ctx.get('rag_chunks_path') or ''}; "
            f"summary comparison {ctx.get('summary_comparison_path') or ''}; "
            f"financial evidence draft {ctx.get('analyst_report_path') or ''}. "
        )
    prompt = _single_line(
        f"Use the financial-analyst agent to complete the formal financial analysis for "
        f"{ctx.get('company_name') or ''} ({ctx.get('stock_code') or ''}). "
        f"{evidence_intro}"
        f"Analysis depth: {ctx.get('depth') or 'standard'}. Research focus: {focus_text}. "
        "Use the compact canonical read order: research_state once, any compatible formal JSON once, analyst_report.json, llm_digest.json, summary_comparison.json, targeted RAG chunks, then targeted content.json only for unresolved evidence. Do not read both JSON and Markdown mirrors and do not reread unchanged files. "
        f"Hard knowledge cutoff: as_of_date={ctx.get('as_of_date') or ''}. Source filing publication date: "
        f"{ctx.get('source_report_published_at') or 'not provided'}. "
        "Based on this evidence, assess operations, earnings, cash flow, and asset quality, and provide expectation gaps, risks, and falsification conditions. "
        "Exclude any announcement, filing, web interpretation, or market data later than as_of_date from both facts and inferences. "
        f"Write formal_financial_analysis.json and formal_financial_analysis.md to {formal_dir}. "
        "Write all narrative content and human-readable JSON values in English; preserve Chinese proper nouns or source quotations only as evidence data with English explanation. "
        "The JSON root must include analysis_metadata with analysis_depth, focus, as_of_date, and financial_input_fingerprint, plus source_filings and cutoff_audit. "
        "When a filing-set handoff is provided, copy its fingerprint and ordered filing identities exactly. "
        "cutoff_audit must use these exact keys without aliases: cutoff_date, strict_cutoff, status, source_report_published_at, maximum_included_information_date, future_source_count, future_excluded_count, undated_source_count, future_fact_claim_count, undated_fact_claim_count, and cutoff_compliant."
    )
    instructions = (
        "Run the prompt below in Claude Code, or copy and paste it directly.\n"
        "The backend polls for the expected artifacts every two seconds and completes this step after both files are written:\n"
        + "\n".join(f"- {item}" for item in expected)
    )
    return {"instructions": instructions, "prompt": prompt, "expected_artifacts": expected}


def build_valuation_prompt(ctx: dict[str, Any]) -> dict[str, Any]:
    """生成估值更新（LLM 步骤）的提示词与期望产物。

    参数：
        ctx: 含 company_name/stock_code/report_year/as_of_date/analyst_dir/
             formal_json_path/market_context_package_path/valuation_dir 的上下文字典。
    返回值：
        {instructions, prompt, expected_artifacts} 字典。
    """
    valuation_dir = str(ctx.get("valuation_dir") or "")
    filenames = ["valuation_report.json", "valuation_report.md", "valuation_evidence_table.json", "valuation_audit.json"]
    expected = [str(Path(valuation_dir) / name) for name in filenames]
    prompt = _single_line(
        f"Use the valuation-analyst agent to update the valuation for {ctx.get('company_name') or ''} "
        f"({ctx.get('stock_code') or ''}), using fiscal-year {ctx.get('report_year') or ''} evidence. "
        f"The valuation observation date and hard knowledge cutoff for every input is as_of_date={ctx.get('as_of_date') or ''}. "
        f"Inputs: financial evidence draft directory {ctx.get('analyst_dir') or ''}; "
        f"formal financial analysis {ctx.get('formal_json_path') or ''}; "
        f"financial input fingerprint {ctx.get('financial_input_fingerprint') or ''}; "
        f"market-context package {ctx.get('market_context_package_path') or '(missing; treat as low confidence)'}. "
        "Treat formal_financial_analysis.json as the authoritative financial input and do not also read its Markdown mirror. Use only market-context sources explicitly marked cutoff_status=eligible; do not read raw_search_results.json, excluded sources, future or undated discovery-only rows, or valuation templates from unrelated companies. "
        "Output a valuation range, bear/base/bull fair value per share, base target price, upside or downside versus current price, key assumptions, and valuation falsification conditions. "
        "No filing, price, share-count, peer, historical valuation, interest-rate, or web source may be later than as_of_date. "
        "Truncate historical series before calculating metrics, and never substitute today's price when historical price is missing. "
        "If market data is missing, provide low-confidence valuation boundaries and a specific data request. "
        "Write all narrative content and human-readable JSON values in English; preserve Chinese proper nouns or source quotations only as evidence data with English explanation. "
        "valuation_audit.json must copy financial_input_fingerprint and source_filing_ids from the formal financial analysis so reuse can be invalidated when a newer filing enters the set. "
        "valuation_audit.json must include cutoff_audit using these exact keys without aliases: cutoff_date, strict_cutoff, status, financial_input_max_date, market_price_max_date, share_count_max_date, peer_data_max_date, historical_valuation_max_date, interest_rate_max_date, market_context_max_date, future_source_count, future_excluded_count, undated_source_count, future_fact_claim_count, undated_fact_claim_count, and cutoff_compliant. "
        f"Write the four-file package ({', '.join(filenames)}) to {valuation_dir}."
    )
    instructions = (
        "Run the prompt below in Claude Code. The backend monitors both the current and legacy directory layouts and completes automatically when either contains the full four-file package:\n"
        + "\n".join(f"- {item}" for item in expected)
        + f"\nLegacy layout compatibility: {ctx.get('valuation_dir_legacy') or ''}"
        + "\nIf upstream_request.json is produced, the console emits a backflow notice."
    )
    return {"instructions": instructions, "prompt": prompt, "expected_artifacts": expected}


def build_industry_research_prompt(ctx: dict[str, Any]) -> dict[str, Any]:
    """生成行业研究（LLM 步骤）的提示词与期望产物。

    参数：
        ctx: 含 target/industry_name/package_dir/package_json/package_md/
             evidence_table 的上下文字典。
    返回值：
        {instructions, prompt, expected_artifacts} 字典。
    """
    package_dir = str(ctx.get("package_dir") or "")
    expected = [str(Path(package_dir) / "industry_research_view.json")]
    prompt = _single_line(
        f"Use the industry-researcher agent to complete industry research on {ctx.get('industry_name') or ctx.get('target') or ''}. "
        f"Industry input package: {ctx.get('package_json') or ''}; "
        f"{ctx.get('package_md') or ''}; "
        f"{ctx.get('evidence_table') or ''}. "
        "Use the package to assess industry classification, cycle conditions, supply and demand, competitive structure, and anchor-company positioning. "
        "Separate verified facts, evidence-based inferences, unverified hypotheses, and theme-only mappings. "
        "Provide trackable variables and falsification conditions. "
        "Write all narrative content and human-readable JSON values in English; preserve Chinese proper nouns or source quotations only as evidence data with English explanation. "
        f"Save the structured conclusion to {expected[0]}."
    )
    instructions = (
        "Run the prompt below in Claude Code. This step completes automatically after the industry research conclusion file is written.\n"
        "If generation remains stalled, use the frontend's Mark Complete fallback:\n" + "\n".join(f"- {item}" for item in expected)
    )
    return {"instructions": instructions, "prompt": prompt, "expected_artifacts": expected}
