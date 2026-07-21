"""Python 强约束的可调用 agent 层。

本模块在 ``worker_runtime`` 之上再加一层“角色契约”：

- Python 拥有 agent 注册表：每个 agent 的角色、工具白名单、输入槽位、输出槽位、
  提示词模板和领域输出校验全部由代码声明，而不是让 Claude Code 自行选 agent；
- 调用方只传 ``agent_id`` + 具体文件路径 + 上下文；本模块把它们编译成一次
  ``WorkerTask``，再交给受限 Claude Code worker 执行；
- 校验权威仍在 Python：worker 的自然语言“已完成”永远不能替代文件与 schema 检查。

生产角色通过 ``AgentSpec`` / ``run_agent`` 声明 I/O 与校验；
当前注册表包含正式财务分析与估值分析两个 LLM worker，由
``company_research_coordinator`` 按 research_state 调度。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from research_console.worker_runtime import (
    WorkerRunResult,
    WorkerTask,
    is_api_request_rejected,
    json_output_validator,
    run_worker,
    scrub_command,
)

# Claude Code print 模式下实测可用的写工具只有 Edit；新建产物由 Python 预创建
# PLACEHOLDER，worker 用 Edit 整文件替换。
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit")
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Write",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "Task",
    "Agent",
)
PLACEHOLDER_SENTINEL = "PLACEHOLDER\n"


@dataclass(frozen=True)
class AgentIOSpec:
    """描述 agent 的一个输入或输出文件槽位。

    参数：
        slot: 稳定槽位名（如 ``manifest`` / ``result``），用于绑定具体路径。
        description: 给人看的槽位说明（英文，进入控制台/报告）。
        required_keys: 若文件是 JSON，要求顶层必须包含的键集合。
        media_type: 当前仅支持 ``json``；后续可扩展 md/jsonl。
    """

    slot: str
    description: str
    required_keys: tuple[str, ...] = ()
    media_type: str = "json"


@dataclass(frozen=True)
class AgentSpec:
    """一个由 Python 声明、可被协调器调用的受限 agent 契约。

    参数：
        agent_id: 稳定标识；Python 调度只认这个 ID，不让模型自选。
        role: 对应研究角色 owner（如 information-collector）。
        description: 一句话职责（英文）。
        inputs: 输入槽位列表；执行前必须全部存在。
        outputs: 输出槽位列表；执行前由 Python 预创建 PLACEHOLDER。
        build_system_prompt: 根据路径与上下文生成 worker 系统提示词。
        build_user_prompt: 生成 ``-p`` 用户消息（保持简短）。
        allowed_tools / disallowed_tools: 工具边界。
        build_expected_output: 可选；合成任务返回精确预言，供精确比对校验。
        build_output_validator: 可选；真实任务用 schema 校验替代精确比对。
        fixture_builder: 可选；为单点测试写入合成输入并返回期望输出。
    """

    # 无默认值字段必须排在有默认值字段之前（dataclass 约束）。
    agent_id: str
    role: str
    description: str
    inputs: tuple[AgentIOSpec, ...]
    outputs: tuple[AgentIOSpec, ...]
    build_system_prompt: Callable[[dict[str, Path], dict[str, Path], dict[str, Any]], str] = field(
        repr=False
    )
    build_user_prompt: Callable[[dict[str, Path], dict[str, Path], dict[str, Any]], str] = field(
        repr=False
    )
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    disallowed_tools: tuple[str, ...] = DEFAULT_DISALLOWED_TOOLS
    build_expected_output: Callable[[dict[str, Any]], dict[str, Any]] | None = field(
        default=None, repr=False
    )
    build_output_validator: Callable[[dict[str, Any]], Callable[[Path], tuple[bool, str | None]]] | None = field(
        default=None, repr=False
    )
    fixture_builder: Callable[[Path], tuple[dict[str, Path], dict[str, Path], dict[str, Any], dict[str, Any]]] | None = field(
        default=None, repr=False
    )

    def input_slots(self) -> tuple[str, ...]:
        """返回输入槽位名列表。"""

        return tuple(item.slot for item in self.inputs)

    def output_slots(self) -> tuple[str, ...]:
        """返回输出槽位名列表。"""

        return tuple(item.slot for item in self.outputs)


@dataclass(frozen=True)
class AgentInvocation:
    """一次具体的 agent 调用请求。

    参数：
        agent_id: 必须已在注册表中。
        cwd: worker 工作目录；合同外文件变化按此目录归因。
        input_paths: 槽位 → 绝对路径；必须覆盖全部 required 输入槽位。
        output_paths: 槽位 → 绝对路径；必须覆盖全部输出槽位。
        context: 进入提示词与校验器的额外上下文（股票代码、截止日等）。
        mcp_config: 显式 MCP 配置；默认应由调用方提供空配置。
        claude_bin: Claude 可执行文件。
        tool_restriction: ``request`` / ``permission``；网关环境用 permission。
        timeout_seconds / max_budget_usd: 进程与预算边界。
        precreate_outputs: 是否由 Python 预创建 PLACEHOLDER（默认是）。
        readable_paths: 额外允许读取的证据文件（不要求全部读完）。
        require_all_inputs_read: 是否强制读完全部 input_paths。
        require_no_extra_files: 是否禁止 cwd 内合同外文件变化。
        allowed_tools / disallowed_tools: 可选覆盖注册表默认工具边界。
    """

    agent_id: str
    cwd: Path
    input_paths: dict[str, Path]
    output_paths: dict[str, Path]
    context: dict[str, Any] = field(default_factory=dict)
    mcp_config: Path | None = None
    claude_bin: str = "claude"
    tool_restriction: str = "permission"
    timeout_seconds: int = 600
    max_budget_usd: float = 1.0
    precreate_outputs: bool = True
    readable_paths: tuple[Path, ...] = ()
    require_all_inputs_read: bool = True
    require_no_extra_files: bool = True
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] | None = None


@dataclass
class AgentRunResult:
    """一次 agent 调用的完整结果边界。"""

    spec: AgentSpec
    invocation: AgentInvocation
    worker: WorkerRunResult
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """是否通过全部契约校验。"""

        return self.worker.passed

    def to_dict(self) -> dict[str, Any]:
        """返回可 JSON 序列化的诊断报告（不含长系统提示词）。"""

        return {
            "agent_id": self.spec.agent_id,
            "role": self.spec.role,
            "description": self.spec.description,
            "passed": self.passed,
            "input_paths": {k: str(v) for k, v in self.invocation.input_paths.items()},
            "output_paths": {k: str(v) for k, v in self.invocation.output_paths.items()},
            "context": self.invocation.context,
            "tool_restriction": self.invocation.tool_restriction,
            "attempts": self.attempts,
            "command_without_prompt_body": scrub_command(self.worker.command),
            "validation": self.worker.validation.to_dict(),
            "observed": {
                "return_code": self.worker.trace.return_code,
                "tool_names": self.worker.trace.tool_names,
                "mcp_servers": self.worker.trace.mcp_servers,
                "permission_denials": self.worker.trace.permission_denials,
                "result_event": self.worker.trace.result_event,
            },
        }


def write_json(path: Path, payload: Any) -> None:
    """以确定格式写入 UTF-8 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_placeholder(path: Path) -> None:
    """预创建 Edit 目标文件的纯文本哨兵。

    为什么用无结构纯文本：若预置 JSON 骨架，模型常做子串式 Edit，容易留下
    半截字段或损坏结构；整文件替换 PLACEHOLDER 更稳。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLACEHOLDER_SENTINEL, encoding="utf-8", newline="\n")


def required_keys_validator(required_keys: tuple[str, ...]) -> Callable[[Path], tuple[bool, str | None]]:
    """校验输出 JSON 至少包含声明的顶层键。"""

    def validate(path: Path) -> tuple[bool, str | None]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"Output is not valid JSON: {exc}"
        if not isinstance(payload, dict):
            return False, f"Output root must be an object: {path}"
        missing = [key for key in required_keys if key not in payload]
        if missing:
            return False, f"Output missing required keys {missing!r} at {path}"
        return True, None

    return validate


def _contract_block(
    input_paths: dict[str, Path],
    output_paths: dict[str, Path],
    allowed_tools: tuple[str, ...],
    disallowed_tools: tuple[str, ...],
) -> str:
    """生成所有微 agent 共用的硬边界说明（英文，进入 worker 系统提示词）。"""

    input_lines = "\n".join(f"- {slot}: {path}" for slot, path in sorted(input_paths.items()))
    output_lines = "\n".join(f"- {slot}: {path}" for slot, path in sorted(output_paths.items()))
    allowed = ", ".join(allowed_tools)
    disallowed = ", ".join(disallowed_tools)
    return f"""
Hard contract:
- Read only the exact input files listed below, plus the output files before editing them.
- Edit only the exact output files listed below.
- Replace each output file's complete PLACEHOLDER sentinel with the required JSON object.
- Do not create, modify, delete, or rename any other file.
- Allowed tools only: {allowed}.
- Do not use: {disallowed}, or any MCP tool.
- Do not access the network.
- Do not inspect the broader project, user settings, skills, or any undeclared path.
- Do not delegate this task.
- Do not perform any work outside the task description.

Declared inputs:
{input_lines}

Declared outputs:
{output_lines}
""".strip()


# ---------------------------------------------------------------------------
# 生产角色：正式财务分析 / 估值（原项目 LLM 步骤的 Python 调度版）
# ---------------------------------------------------------------------------


def _formal_financial_system(
    input_paths: dict[str, Path],
    output_paths: dict[str, Path],
    context: dict[str, Any],
) -> str:
    """正式财务分析员系统提示词（英文交付，路径由协调器注入）。"""

    stock_code = str(context.get("stock_code") or "")
    company_name = str(context.get("company_name") or "")
    as_of_date = str(context.get("as_of_date") or "")
    depth = str(context.get("depth") or "standard")
    focus = str(context.get("focus") or "No specific focus")
    fingerprint = str(context.get("financial_input_fingerprint") or "")
    evidence_lines = "\n".join(
        f"- {path}" for path in context.get("evidence_paths") or []
    )
    return f"""
You are the project's financial-analyst worker for a Python-owned company-research coordinator.

Target: {company_name} ({stock_code})
Hard knowledge cutoff as_of_date: {as_of_date}
Analysis depth: {depth}
Research focus: {focus}
financial_input_fingerprint: {fingerprint}

{_contract_block(input_paths, output_paths, DEFAULT_ALLOWED_TOOLS, DEFAULT_DISALLOWED_TOOLS)}

Additional read-only evidence files the coordinator authorizes (read only what you need):
{evidence_lines or '- (none listed)'}

Task:
- Read research_state and filing_set first.
- Then read only the authorized evidence needed for a defendable formal financial analysis
  (analyst_report.json, llm_digest.json, summary_comparison.json, targeted RAG/content).
- Assess operations, earnings quality, cash flow, asset quality, expectation gaps, risks,
  and falsification conditions that matter for valuation inputs.
- Do NOT output a target price or buy/sell instruction.
- Write English narrative and human-readable JSON values.
- formal_financial_analysis.json root must include analysis_metadata with analysis_depth,
  focus, as_of_date, financial_input_fingerprint, plus source_filings and cutoff_audit.
- cutoff_audit must use exact keys: cutoff_date, strict_cutoff, status,
  source_report_published_at, maximum_included_information_date, future_source_count,
  future_excluded_count, undated_source_count, future_fact_claim_count,
  undated_fact_claim_count, cutoff_compliant.
- Copy financial_input_fingerprint from the filing set when present.
- Replace PLACEHOLDER in BOTH output files completely.

After both outputs are written, return one short plain-text completion sentence and stop.
""".strip()


def _formal_financial_user(
    input_paths: dict[str, Path],
    output_paths: dict[str, Path],
    context: dict[str, Any],
) -> str:
    return (
        "Execute formal_financial_analyst now. "
        f"Required reads: {input_paths.get('research_state')}, {input_paths.get('filing_set')}. "
        f"Write only {output_paths.get('formal_json')} and {output_paths.get('formal_md')}. "
        "Replace every PLACEHOLDER sentinel fully, then stop."
    )


def _formal_financial_validator(
    context: dict[str, Any],
) -> Callable[[Path], tuple[bool, str | None]]:
    """弱 schema：JSON 必须是对象且含 analysis_metadata；md 必须非空非 PLACEHOLDER。"""

    fingerprint = str(context.get("financial_input_fingerprint") or "")

    def validate(path: Path) -> tuple[bool, str | None]:
        text = path.read_text(encoding="utf-8")
        if text.strip() in {"", "PLACEHOLDER"}:
            return False, f"Output is still placeholder: {path}"
        if path.suffix.lower() == ".md":
            if len(text.strip()) < 40:
                return False, f"Markdown formal analysis is too short: {path}"
            return True, None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"formal_financial_analysis.json invalid: {exc}"
        if not isinstance(payload, dict):
            return False, "formal_financial_analysis.json root must be an object"
        meta = payload.get("analysis_metadata")
        if not isinstance(meta, dict):
            return False, "analysis_metadata object is required"
        if fingerprint and str(meta.get("financial_input_fingerprint") or "") not in {
            "",
            fingerprint,
        }:
            # 允许缺省复制失败时仍以 low-confidence 交付，但若写了错误指纹则失败。
            if str(meta.get("financial_input_fingerprint") or "") != fingerprint:
                return (
                    False,
                    f"financial_input_fingerprint mismatch: expected {fingerprint!r}",
                )
        return True, None

    return validate


def _valuation_system(
    input_paths: dict[str, Path],
    output_paths: dict[str, Path],
    context: dict[str, Any],
) -> str:
    """估值分析员系统提示词。"""

    stock_code = str(context.get("stock_code") or "")
    company_name = str(context.get("company_name") or "")
    as_of_date = str(context.get("as_of_date") or "")
    fingerprint = str(context.get("financial_input_fingerprint") or "")
    evidence_lines = "\n".join(
        f"- {path}" for path in context.get("evidence_paths") or []
    )
    return f"""
You are the project's valuation-analyst worker for a Python-owned company-research coordinator.

Target: {company_name} ({stock_code})
Valuation observation date / hard cutoff as_of_date: {as_of_date}
financial_input_fingerprint: {fingerprint}

{_contract_block(input_paths, output_paths, DEFAULT_ALLOWED_TOOLS, DEFAULT_DISALLOWED_TOOLS)}

Additional read-only evidence files the coordinator authorizes:
{evidence_lines or '- (none listed)'}

Task:
- Read research_state and formal_financial_analysis.json first.
- formal_financial_analysis.json is the authoritative financial input; do not invent numbers
  that contradict it.
- Use only authorized market-context sources if present; treat public web market context as
  public_web_search_proxy, not formal consensus.
- Output bear/base/bull fair value per share (or market-cap equivalents), base target price,
  upside/downside vs current price when price is available, key assumptions, and valuation
  falsifiers.
- If current price/share count is missing, still give low-confidence three-scenario fair values
  with price_source=missing and confidence=low; never deliver only a method framework.
- Write English narrative and human-readable JSON values.
- valuation_report.json MUST be console-extractable. Prefer this dual-compatible shape:
  - valuation_view: undervalued|fair|overvalued|watch_only
  - one_sentence_conclusion: one English sentence
  - market_snapshot.current_price + price_source (or price_context.current_price)
  - fair_value_scenarios.bear/base/bull each with fair_value_per_share
  - ALSO mirror legacy keys when possible: fair_value_range_per_share, base_case_target_price,
    upside_downside_vs_current_price, scenario_fair_values
  - base_target_price may be a number or {{"value": number}}
- valuation_audit.json must include financial_input_fingerprint and cutoff_audit with exact keys:
  cutoff_date, strict_cutoff, status, financial_input_max_date, market_price_max_date,
  share_count_max_date, peer_data_max_date, historical_valuation_max_date,
  interest_rate_max_date, market_context_max_date, future_source_count, future_excluded_count,
  undated_source_count, future_fact_claim_count, undated_fact_claim_count, cutoff_compliant.
- Replace PLACEHOLDER in ALL four output files completely.
- Do NOT give final buy/sell execution orders.

After all outputs are written, return one short plain-text completion sentence and stop.
""".strip()


def _valuation_user(
    input_paths: dict[str, Path],
    output_paths: dict[str, Path],
    context: dict[str, Any],
) -> str:
    outs = ", ".join(str(path) for path in output_paths.values())
    return (
        "Execute company_valuation_analyst now. "
        f"Required reads: {input_paths.get('research_state')}, {input_paths.get('formal_json')}. "
        f"Write only these outputs: {outs}. "
        "Replace every PLACEHOLDER sentinel fully, then stop."
    )


def _valuation_validator(
    context: dict[str, Any],
) -> Callable[[Path], tuple[bool, str | None]]:
    """估值四件套弱 schema：非 PLACEHOLDER；JSON 为对象。"""

    def validate(path: Path) -> tuple[bool, str | None]:
        text = path.read_text(encoding="utf-8")
        if text.strip() in {"", "PLACEHOLDER"}:
            return False, f"Output is still placeholder: {path}"
        if path.suffix.lower() == ".md":
            if len(text.strip()) < 40:
                return False, f"Markdown valuation report is too short: {path}"
            return True, None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"Valuation JSON invalid at {path}: {exc}"
        if not isinstance(payload, dict):
            return False, f"Valuation JSON root must be an object: {path}"
        return True, None

    return validate


# ---------------------------------------------------------------------------
# 注册表：Python 拥有，模型不能自选
# ---------------------------------------------------------------------------


AGENT_REGISTRY: dict[str, AgentSpec] = {
    # 生产角色：原项目 financial-analyst / valuation-analyst 的 Python 调度形态
    "formal_financial_analyst": AgentSpec(
        agent_id="formal_financial_analyst",
        role="financial-analyst",
        description="Produce formal financial analysis from filing-set evidence for valuation handoff.",
        inputs=(
            AgentIOSpec("research_state", "Company research_state.json.", ("layers", "target")),
            AgentIOSpec("filing_set", "Multi-period filing_set.json handoff."),
        ),
        outputs=(
            AgentIOSpec(
                "formal_json",
                "formal_financial_analysis.json",
                ("analysis_metadata",),
            ),
            AgentIOSpec("formal_md", "formal_financial_analysis.md"),
        ),
        build_system_prompt=_formal_financial_system,
        build_user_prompt=_formal_financial_user,
        build_output_validator=_formal_financial_validator,
    ),
    "company_valuation_analyst": AgentSpec(
        agent_id="company_valuation_analyst",
        role="valuation-analyst",
        description="Produce valuation package from formal financial analysis and market context.",
        inputs=(
            AgentIOSpec("research_state", "Company research_state.json.", ("layers", "target")),
            AgentIOSpec("formal_json", "formal_financial_analysis.json"),
        ),
        outputs=(
            AgentIOSpec("valuation_report_json", "valuation_report.json"),
            AgentIOSpec("valuation_report_md", "valuation_report.md"),
            AgentIOSpec("valuation_evidence_table_json", "valuation_evidence_table.json"),
            AgentIOSpec("valuation_audit_json", "valuation_audit.json"),
        ),
        build_system_prompt=_valuation_system,
        build_user_prompt=_valuation_user,
        build_output_validator=_valuation_validator,
    ),
}



def list_agents() -> list[dict[str, Any]]:
    """返回注册表摘要，供控制台/CLI 发现可用 agent。"""

    items: list[dict[str, Any]] = []
    for spec in AGENT_REGISTRY.values():
        items.append(
            {
                "agent_id": spec.agent_id,
                "role": spec.role,
                "description": spec.description,
                "inputs": [asdict(item) for item in spec.inputs],
                "outputs": [asdict(item) for item in spec.outputs],
                "allowed_tools": list(spec.allowed_tools),
            }
        )
    return items


def get_agent(agent_id: str) -> AgentSpec:
    """按 ID 取 agent；未知 ID 直接抛错，禁止静默回退到通用 agent。"""

    try:
        return AGENT_REGISTRY[agent_id]
    except KeyError as exc:
        known = ", ".join(sorted(AGENT_REGISTRY))
        raise KeyError(f"Unknown agent_id={agent_id!r}. Known agents: {known}") from exc


def _validate_slot_binding(spec: AgentSpec, invocation: AgentInvocation) -> None:
    """校验调用方提供的路径绑定是否覆盖全部声明槽位。"""

    missing_inputs = [slot for slot in spec.input_slots() if slot not in invocation.input_paths]
    missing_outputs = [slot for slot in spec.output_slots() if slot not in invocation.output_paths]
    if missing_inputs:
        raise ValueError(f"Agent {spec.agent_id} missing input slots: {missing_inputs}")
    if missing_outputs:
        raise ValueError(f"Agent {spec.agent_id} missing output slots: {missing_outputs}")

    extra_inputs = sorted(set(invocation.input_paths) - set(spec.input_slots()))
    extra_outputs = sorted(set(invocation.output_paths) - set(spec.output_slots()))
    if extra_inputs:
        raise ValueError(f"Agent {spec.agent_id} has undeclared input slots: {extra_inputs}")
    if extra_outputs:
        raise ValueError(f"Agent {spec.agent_id} has undeclared output slots: {extra_outputs}")


def build_worker_task(spec: AgentSpec, invocation: AgentInvocation) -> WorkerTask:
    """把 AgentSpec + 路径绑定编译成底层 WorkerTask。

    功能：
        这里是“Python 强约束 agent 调用”的编译边界：模型看不到注册表，
        只能执行本次内联的 agent_definition；输入/输出路径与工具白名单
        同时写进系统提示词和 WorkerTask 字段，由 worker_runtime 做事后校验。
    """

    _validate_slot_binding(spec, invocation)
    mcp_config = invocation.mcp_config
    if mcp_config is None:
        raise ValueError("mcp_config is required; pass an empty MCP config for isolation")

    system_prompt = spec.build_system_prompt(
        invocation.input_paths,
        invocation.output_paths,
        invocation.context,
    )
    user_prompt = spec.build_user_prompt(
        invocation.input_paths,
        invocation.output_paths,
        invocation.context,
    )
    agent_definition = {
        "description": spec.description,
        "prompt": system_prompt,
    }
    return WorkerTask(
        agent_name=spec.agent_id,
        agent_definition=agent_definition,
        prompt=user_prompt,
        cwd=invocation.cwd,
        input_paths=tuple(invocation.input_paths[slot] for slot in spec.input_slots()),
        output_paths=tuple(invocation.output_paths[slot] for slot in spec.output_slots()),
        mcp_config=mcp_config,
        allowed_tools=invocation.allowed_tools or spec.allowed_tools,
        disallowed_tools=invocation.disallowed_tools or spec.disallowed_tools,
        tool_restriction=invocation.tool_restriction,
        claude_bin=invocation.claude_bin,
        timeout_seconds=invocation.timeout_seconds,
        max_budget_usd=invocation.max_budget_usd,
        permission_mode="dontAsk",
        require_no_extra_files=invocation.require_no_extra_files,
        readable_paths=invocation.readable_paths,
        require_all_inputs_read=invocation.require_all_inputs_read,
    )


def resolve_output_validator(
    spec: AgentSpec,
    context: dict[str, Any],
    *,
    expected_output: dict[str, Any] | None = None,
) -> Callable[[Path], tuple[bool, str | None]]:
    """选择输出校验器：精确预言 > 自定义 schema 校验 > 仅检查 required_keys。"""

    if expected_output is not None:
        return json_output_validator(expected_output)
    if spec.build_expected_output is not None and expected_output is None:
        # 合成 agent 默认用精确预言；调用方也可显式传入 expected_output 覆盖。
        return json_output_validator(spec.build_expected_output(context))
    if spec.build_output_validator is not None:
        return spec.build_output_validator(context)

    # 回退：对每个输出文件检查 AgentIOSpec.required_keys。
    # 多输出时，json_output_validator 只能校验“当前这个 path”，所以这里用
    # 槽位无关的 required_keys 并集做弱校验；生产 agent 应提供自定义校验器。
    required: list[str] = []
    for item in spec.outputs:
        required.extend(item.required_keys)
    return required_keys_validator(tuple(dict.fromkeys(required)))


def prepare_invocation(spec: AgentSpec, invocation: AgentInvocation) -> None:
    """执行前准备：检查输入存在、可选预创建输出占位。"""

    _validate_slot_binding(spec, invocation)
    for slot, path in invocation.input_paths.items():
        if not path.exists():
            raise FileNotFoundError(f"Input slot {slot!r} does not exist: {path}")
    if invocation.precreate_outputs:
        for path in invocation.output_paths.values():
            # 已有非 PLACEHOLDER 内容时不覆盖，避免误伤调用方已写入的真实产物。
            if path.exists() and path.read_text(encoding="utf-8") != PLACEHOLDER_SENTINEL:
                continue
            write_placeholder(path)


def run_agent(
    invocation: AgentInvocation,
    *,
    expected_output: dict[str, Any] | None = None,
    auto_fallback_tool_mode: bool = True,
) -> AgentRunResult:
    """按注册表强约束调用指定 agent。

    功能：
        1) 用 agent_id 查表，禁止模型自选；
        2) 校验并准备 I/O；
        3) 编译 WorkerTask 并调用 run_worker；
        4) 若 auto_fallback 且 request 模式被网关 429 拒绝，则用 permission 重试。
    参数：
        invocation: 具体调用请求。
        expected_output: 可选精确预言；默认使用 agent 自带的 build_expected_output。
        auto_fallback_tool_mode: request 被网关拒绝时是否自动改 permission。
    返回值：
        AgentRunResult。
    """

    spec = get_agent(invocation.agent_id)
    prepare_invocation(spec, invocation)
    if invocation.mcp_config is None:
        raise ValueError("mcp_config is required")
    if not invocation.mcp_config.exists():
        write_json(invocation.mcp_config, {"mcpServers": {}})

    validator = resolve_output_validator(spec, invocation.context, expected_output=expected_output)
    attempts: list[dict[str, Any]] = []

    modes: list[str]
    if auto_fallback_tool_mode and invocation.tool_restriction == "request":
        modes = ["request", "permission"]
    else:
        modes = [invocation.tool_restriction]

    worker_result: WorkerRunResult | None = None
    final_invocation = invocation
    for mode in modes:
        final_invocation = AgentInvocation(
            agent_id=invocation.agent_id,
            cwd=invocation.cwd,
            input_paths=invocation.input_paths,
            output_paths=invocation.output_paths,
            context=invocation.context,
            mcp_config=invocation.mcp_config,
            claude_bin=invocation.claude_bin,
            tool_restriction=mode,
            timeout_seconds=invocation.timeout_seconds,
            max_budget_usd=invocation.max_budget_usd,
            precreate_outputs=False,  # 已在 prepare_invocation 做过
            readable_paths=invocation.readable_paths,
            require_all_inputs_read=invocation.require_all_inputs_read,
            require_no_extra_files=invocation.require_no_extra_files,
            allowed_tools=invocation.allowed_tools,
            disallowed_tools=invocation.disallowed_tools,
        )
        task = build_worker_task(spec, final_invocation)
        worker_result = run_worker(task, output_validator=validator)
        attempts.append(
            {
                "tool_restriction": mode,
                "passed": worker_result.passed,
                "api_error_status": worker_result.trace.result_event.get("api_error_status"),
                "request_shape_rejected": is_api_request_rejected(worker_result.trace),
            }
        )
        if worker_result.passed:
            break
        if not (auto_fallback_tool_mode and is_api_request_rejected(worker_result.trace)):
            break

    assert worker_result is not None
    return AgentRunResult(
        spec=spec,
        invocation=final_invocation,
        worker=worker_result,
        attempts=attempts,
    )


def run_agent_fixture(
    agent_id: str,
    workspace: Path,
    *,
    claude_bin: str = "claude",
    tool_restriction: str = "permission",
    timeout_seconds: int = 600,
    max_budget_usd: float = 1.0,
    auto_fallback_tool_mode: bool = True,
) -> tuple[AgentRunResult, dict[str, Any]]:
    """用注册表内置合成夹具单点跑一个 agent。

    返回值：
        (AgentRunResult, expected_output)
    """

    spec = get_agent(agent_id)
    if spec.fixture_builder is None:
        raise ValueError(f"Agent {agent_id!r} has no fixture_builder for single-point testing")

    sandbox = workspace / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    input_paths, output_paths, context, expected = spec.fixture_builder(sandbox)
    mcp_config = workspace / "empty_mcp.json"
    write_json(mcp_config, {"mcpServers": {}})

    result = run_agent(
        AgentInvocation(
            agent_id=agent_id,
            cwd=sandbox,
            input_paths=input_paths,
            output_paths=output_paths,
            context=context,
            mcp_config=mcp_config,
            claude_bin=claude_bin,
            tool_restriction=tool_restriction,
            timeout_seconds=timeout_seconds,
            max_budget_usd=max_budget_usd,
            precreate_outputs=True,
        ),
        expected_output=expected,
        auto_fallback_tool_mode=auto_fallback_tool_mode,
    )
    return result, expected
