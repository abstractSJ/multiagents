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
    StepDef("audit", ORCHESTRATOR, "script", "研究状态盘点", None),
    StepDef("collector_fetch", INFO_COLLECTOR, "script", "财报采集下载", "collector"),
    StepDef("processor_parse", INFO_PROCESSOR, "script", "PDF 解析", "processor"),
    StepDef("processor_digest", INFO_PROCESSOR, "script", "LLM Digest 构建", "processor"),
    StepDef("processor_rag", INFO_PROCESSOR, "script", "RAG 索引构建", "processor"),
    StepDef("processor_compare", INFO_PROCESSOR, "script", "摘要交叉比对", "processor"),
    StepDef("financial_evidence_draft", FINANCIAL_ANALYST, "script", "财务证据草稿", "financial_evidence_draft"),
    StepDef("formal_financial_analysis", FINANCIAL_ANALYST, "llm", "正式财务分析", "formal_financial_analysis"),
    StepDef("market_context_update", MARKET_CONTEXT_COLLECTOR, "script", "市场上下文采集", "market_context"),
    StepDef("valuation_update", VALUATION_ANALYST, "llm", "估值更新", "valuation"),
    StepDef("final_audit", ORCHESTRATOR, "script", "终局状态盘点", None),
    StepDef("deliver", ORCHESTRATOR, "synthetic", "结论交付", None),
]

# 行业链路步骤
INDUSTRY_STEP_DEFS: list[StepDef] = [
    StepDef("industry_collect", INDUSTRY_INFO_COLLECTOR, "script", "行业输入包收集", None),
    StepDef("industry_validate", INDUSTRY_INFO_COLLECTOR, "script", "行业包校验", None),
    StepDef("industry_research", INDUSTRY_RESEARCHER, "llm", "行业研究结论", None),
    StepDef("industry_deliver", ORCHESTRATOR, "synthetic", "行业交付", None),
]

COMPANY_STEP_MAP = {item.step_id: item for item in COMPANY_STEP_DEFS}
INDUSTRY_STEP_MAP = {item.step_id: item for item in INDUSTRY_STEP_DEFS}

SKIP_REASON_REUSE = "复用已有产物"
SKIP_REASON_LLM_MODE = "llm_mode=skip，跳过 LLM 步骤（交付降级）"
SKIP_REASON_MARKET_OFF = "run_market_context=false，跳过市场上下文采集"


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
        raise ValueError("cutoff 必须是 date 或严格 ISO 日期 YYYY-MM-DD，不能包含时间")
    if isinstance(value, _dt.date):
        return value
    text = str(value or "").strip()
    if len(text) != 10 or text[4:5] != "-" or text[7:8] != "-":
        raise ValueError(f"cutoff 必须是严格 ISO 日期 YYYY-MM-DD，当前值：{text or '<empty>'}")
    try:
        parsed = _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cutoff 不是有效日期：{text}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"cutoff 必须是严格 ISO 日期 YYYY-MM-DD，当前值：{text}")
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


def build_processor_parse_cmd(
    stock_code: str,
    report_type: str,
    report_year: str,
    overwrite: bool = False,
) -> list[str]:
    """构建 PDF 解析命令。

    参数：
        stock_code: 股票代码。
        report_type: 报告类型。
        report_year: 财报年度。
        overwrite: force_refresh 时覆盖已有 content.json。
    返回值：
        run_pdf_processing.py 命令参数列表。
    """
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


def _coordinator_arg(value: Any) -> str:
    """把 /rec 参数编码为不会因空格或中文标点被拆分的单个值。

    参数：
        value: 任意可字符串化参数。
    返回值：
        JSON 字符串形式的参数值；布尔值使用小写 true/false。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value), ensure_ascii=False)


def build_company_coordinator_prompt(params: dict[str, Any], run_id: str) -> str:
    """构造一次性完整 /rec 主协调会话提示词。

    功能：
        把 company run 的现有参数完整透传给项目 /rec Skill，并明确这次调用
        必须由同一个持续会话按 research_state 复用规则调度 custom agents。
        控制台事件来自 Claude stream-json 与工作区 audit，提示词不要求角色手工发事件。
    参数：
        params: company run 参数。
        run_id: 控制台运行标识，仅用于审计关联。
    返回值：
        可直接作为 ``claude -p`` 单个参数的完整提示词。
    """
    target = params.get("target") or params.get("stock_code") or params.get("company_name") or ""
    fiscal_year = params.get("fiscal_year") or params.get("report_year") or ""
    ordered = [
        ("target", target),
        ("stock_code", params.get("stock_code")),
        ("company_name", params.get("company_name")),
        ("fiscal_year", fiscal_year),
        ("report_type", params.get("report_type") or "annual"),
        ("depth", params.get("depth") or "standard"),
        ("focus", params.get("focus")),
        ("as_of_date", params.get("as_of_date")),
        ("force_refresh", bool(params.get("force_refresh"))),
        ("run_market_context", params.get("run_market_context", True) is not False),
        ("market_context_freshness", params.get("market_context_freshness")),
        ("market_price", params.get("market_price")),
        ("valuation_method", params.get("valuation_method")),
        ("run_industry", params.get("run_industry", False) is True),
    ]
    command_parts = ["/rec"]
    for key, value in ordered:
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        command_parts.append(f"{key}={_coordinator_arg(value)}")
    command = " ".join(command_parts)
    return _single_line(
        f"{command}。这是 research_console run_id={run_id} 的一个完整公司研究主协调会话。"
        "必须沿用项目公司研究 Skill、research_state 复用规则与对应 custom agents 完成端到端研究，"
        "不要把财务分析、估值或市场上下文拆成彼此独立的顶层 claude CLI 会话。"
        "控制台会从 stream-json 与工作区 audit 自动生成状态事件，不需要为控制台手工输出事件。"
        "若使用 TaskCreate 管理工作项，拿到任务编号后，后续对应 Agent 调用的 description 必须以"
        "[任务#编号] 开头并紧跟简短任务名，便于控制台把真实 Agent 与调度任务稳定关联。"
        "本次只执行阶段一，不实现或输出阶段二 research_requests 协议。"
        "完成后请交付结论前置的完整公司研究报告。"
    )


def build_claude_stream_command(prompt: str, claude_path: str = "claude") -> list[str]:
    """构造 Claude Code stream-json 命令参数。

    参数：
        prompt: 完整 /rec 提示词。
        claude_path: claude 可执行文件路径；测试可注入固定值。
    返回值：
        不经 shell 的 subprocess 参数列表。
    """
    return [
        str(claude_path),
        "-p",
        str(prompt),
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        config.COORDINATOR_PERMISSION_MODE,
    ]


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
    focus_text = str(ctx.get("focus") or "无特别聚焦")
    prompt = _single_line(
        f"请使用 financial-analyst agent 完成正式财务分析。研究对象：{ctx.get('company_name') or ''}"
        f"（{ctx.get('stock_code') or ''}）{ctx.get('report_year') or ''} 年 {ctx.get('report_type') or 'annual'} 财报，"
        f"分析深度 {ctx.get('depth') or 'standard'}，研究重点：{focus_text}。"
        f"证据输入：信息处理员报告目录 {ctx.get('report_dir') or ''}；"
        f"llm_digest：{ctx.get('llm_digest_path') or ''}；"
        f"RAG 索引：{ctx.get('rag_chunks_path') or ''}；"
        f"摘要比对：{ctx.get('summary_comparison_path') or ''}；"
        f"财务证据草稿：{ctx.get('analyst_report_path') or ''}。"
        f"知识截止日 as_of_date={ctx.get('as_of_date') or ''}；来源财报披露日="
        f"{ctx.get('source_report_published_at') or '未提供'}。"
        f"要求：基于以上证据完成经营、盈利、现金流与资产质量判断，输出预期差、风险与证伪条件；"
        f"任何晚于 as_of_date 的公告、财报、网页解释或市场数据必须排除，不能进入事实或推断。"
        f"并把 formal_financial_analysis.json 与 formal_financial_analysis.md 写入 {formal_dir}。"
        f"JSON 顶层需包含 analysis_metadata（analysis_depth、focus 与 as_of_date）以及 cutoff_audit，"
        f"cutoff_audit 至少记录 cutoff_date、status/compliant、最大纳入日期、未来来源排除数和无日期来源数。"
    )
    instructions = (
        "在 Claude Code 中执行下方提示词（或点击复制后直接粘贴运行）。\n"
        "后端每 2 秒轮询期望产物，两份文件全部落盘后本步骤自动完成：\n"
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
        f"请使用 valuation-analyst agent 完成估值更新。研究对象：{ctx.get('company_name') or ''}"
        f"（{ctx.get('stock_code') or ''}），财报年度 {ctx.get('report_year') or ''}，"
        f"估值观察日 as_of_date={ctx.get('as_of_date') or ''}，同时也是所有输入的硬性知识截止日。"
        f"输入：财务证据草稿目录 {ctx.get('analyst_dir') or ''}；"
        f"正式财务分析：{ctx.get('formal_json_path') or ''}；"
        f"市场上下文包：{ctx.get('market_context_package_path') or '（缺失，按低置信处理）'}。"
        f"要求：输出估值区间、三档每股合理价值（bear/base/bull）、基准目标价、相对现价上下行空间、"
        f"关键假设与估值证伪条件；财报、价格、股本、同行、历史估值、利率和网页来源均不得晚于 as_of_date，"
        f"历史序列必须先截断再计算，缺历史价格时不得拿今天价格代替。缺市场数据时给低置信估值边界并写明补数请求。"
        f"valuation_audit.json 必须包含 cutoff_audit，记录截止日、最大纳入日期、未来来源排除数和合规状态。"
        f"把 {'、'.join(filenames)} 四件套写入 {valuation_dir}。"
    )
    instructions = (
        "在 Claude Code 中执行下方提示词。后端同时监视新旧两种目录布局，任一凑齐四件套即自动完成：\n"
        + "\n".join(f"- {item}" for item in expected)
        + f"\n（兼容旧布局：{ctx.get('valuation_dir_legacy') or ''}）"
        + "\n若产出 upstream_request.json，控制台会发出回流提示。"
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
        f"请使用 industry-researcher agent 完成行业研究。研究对象：{ctx.get('industry_name') or ctx.get('target') or ''}。"
        f"行业输入包三件套：{ctx.get('package_json') or ''}；"
        f"{ctx.get('package_md') or ''}；"
        f"{ctx.get('evidence_table') or ''}。"
        f"要求：基于输入包完成行业归属、景气、供需、竞争格局与锚点公司位置判断，"
        f"区分已验证事实、基于证据的推断、未证实假设与题材映射，给出可跟踪变量与证伪条件，"
        f"并把结构化结论另存为 {expected[0]}。"
    )
    instructions = (
        "在 Claude Code 中执行下方提示词。行业研究结论文件落盘后本步骤自动完成；\n"
        "若长期不产出，可在前端点击“标记完成”兜底：\n" + "\n".join(f"- {item}" for item in expected)
    )
    return {"instructions": instructions, "prompt": prompt, "expected_artifacts": expected}
