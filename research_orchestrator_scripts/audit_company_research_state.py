"""公司研究状态审计器。

该脚本是公司研究链路的第 0 步：在委派信息收集员、信息处理员、
财务分析员或估值分析员之前，先扫描本地工作区已经存在的产物，
输出哪些层可以复用、哪些层需要补齐、哪些层因为日期或研究重点不匹配需要刷新。

设计原则：
- 只做产物盘点和续跑规划，不替代任何 custom agent 做研究判断。
- 默认不覆盖、不重跑；只有缺失、部分完成、过期或不兼容的层才进入 next_actions。
- 输出结构化 JSON，方便主会话、/rec 和 /re 直接据此调度。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    # 控制台和文档都允许直接执行本文件；此时 Python 只会把脚本所在目录加入
    # 模块搜索路径，因此必须显式加入项目根目录，才能解析同级顶层包的绝对导入。
    sys.path.insert(0, str(PROJECT_ROOT))

from research_orchestrator_scripts.recent_filing_policy import (
    INTERIM_REPORT_TYPES,
    PERIOD_MONTHS,
    build_filing_identity,
    calculate_financial_input_fingerprint,
    derive_recent_filing_plan,
    filing_id,
    normalize_filing_policy,
)


DEPTH_RANK = {"quick": 1, "standard": 2, "deep": 3}
PROCESSOR_REQUIRED_KEYS = [
    "content_json",
    "content_md",
    "llm_digest_json",
    "digest_audit_json",
    "rag_chunks_jsonl",
    "summary_comparison_json",
]
FINANCIAL_DRAFT_REQUIRED_KEYS = ["analyst_report_json", "analyst_report_md", "evidence_check_json", "analyst_audit_json"]
FORMAL_FINANCIAL_REQUIRED_KEYS = ["formal_financial_analysis_json", "formal_financial_analysis_md"]
VALUATION_REQUIRED_KEYS = [
    "valuation_report_json",
    "valuation_report_md",
    "valuation_evidence_table_json",
    "valuation_audit_json",
]
MARKET_CONTEXT_REQUIRED_KEYS = [
    "market_context_package_json",
    "market_context_package_md",
    "market_context_sources_json",
    "collection_audit_json",
]
MARKET_CONTEXT_READY_STATUS = "ready_public_proxy"


@dataclass(frozen=True)
class ResearchAuditRequest:
    """公司研究状态审计请求。

    参数：
        target: 用户传入的公司名或股票代码；如果是 6 位数字，会被视作Stock code.
        stock_code: 明确的Stock code.
        company_name: 明确的公司简称或全称。
        report_year: 显式单份模式的财报年度；为空时默认近期历史模式。
        report_type: 显式单份模式的报告类型；为空时默认近期历史模式。
        filing_policy: recent_history 或 single_filing；为空时按显式筛选自动判断。
        annual_lookback: 近期历史模式保留的实际可得年报数量。
        depth: 本次研究深度，影响正式财务分析复用兼容性。
        focus: 本次研究重点，影响正式财务分析复用兼容性。
        as_of_date: 估值观察日；同日估值可复用，旧估值标记为 stale。
        force_refresh: 是否强制刷新所有层；默认 False，防止误重跑。
        write_state: 是否把状态写入默认工作区。
        output: 显式输出路径；为空时由 write_state 决定是否使用默认路径。
    返回值：
        dataclass 实例，无额外返回值。
    """

    target: str = ""
    stock_code: str = ""
    company_name: str = ""
    report_year: str = ""
    report_type: str = ""
    filing_policy: str = ""
    annual_lookback: int = 2
    depth: str = "standard"
    focus: str = ""
    as_of_date: str = ""
    force_refresh: bool = False
    write_state: bool = False
    output: str = ""


@dataclass(frozen=True)
class CollectorSelection:
    """财报采集层目标选择结果。

    参数：
        target: 标准化后的目标信息。
        matched_records: manifest 中命中的全部目标记录。
        eligible_records: 不晚于知识截止日、可供选择的记录。
        future_records: 晚于知识截止日、仅用于审计的记录。
        undated_records: 缺少或无法解析披露日期、仅用于审计的记录。
        main_record: 正式财报记录。
        summary_record: 摘要财报记录。
        ambiguous_choices: 无法唯一识别时的候选集合。
    返回值：
        dataclass 实例，无额外返回值。
    """

    target: dict[str, str]
    matched_records: list[dict[str, Any]]
    eligible_records: list[dict[str, Any]]
    future_records: list[dict[str, Any]]
    undated_records: list[dict[str, Any]]
    main_record: dict[str, Any] | None
    summary_record: dict[str, Any] | None
    ambiguous_choices: list[dict[str, str]]


@dataclass(frozen=True)
class FinancialArtifacts:
    """财务分析层候选目录集合。

    参数：
        evidence_dir: 财务证据草稿所在目录。
        formal_dir: 正式财务分析所在目录。
    返回值：
        dataclass 实例，无额外返回值。
    """

    evidence_dir: Path | None
    formal_dir: Path | None


@dataclass(frozen=True)
class DatedCandidates:
    """按知识截止日划分的日期目录候选集合。

    参数：
        exact: 目录日期等于知识截止日的候选。
        before: 目录日期早于知识截止日的候选。
        future: 目录日期晚于知识截止日的候选，仅保留用于审计。
        undated: 目录名不是严格 ISO 日期的候选，不能用于历史状态复用。
    返回值：
        dataclass 实例，无额外返回值。
    """

    exact: list[Path]
    before: list[Path]
    future: list[Path]
    undated: list[Path]


def audit_company_research_state(project_root: str | Path, request: ResearchAuditRequest) -> dict[str, Any]:
    """审计单家公司研究链路的本地复用状态。

    默认近期历史模式会同时审计多份财报；显式固定报告类型和财年时继续使用原有
    单份财报语义。两种模式共享顶层兼容字段，避免旧调用方在升级时立即失效。
    """
    root = Path(project_root).resolve()
    normalized_request = normalize_request(request)
    if normalized_request.filing_policy == "recent_history":
        return audit_recent_history_state(root, normalized_request)
    return audit_single_filing_state(root, normalized_request)


def audit_single_filing_state(root: Path, request: ResearchAuditRequest) -> dict[str, Any]:
    """执行向后兼容的单份财报状态审计。"""
    collector_layer, selection = audit_collector_layer(root, request)
    target = selection.target
    processor_layer = audit_processor_layer(
        root,
        target,
        selection.main_record,
        summary_required=bool(selection.summary_record),
    )
    processor_pdf_sha256, processor_identity_gap = verified_processor_pdf_hash(
        root,
        processor_layer,
        selection.main_record,
    )
    processor_layer = apply_processor_identity_gap(processor_layer, processor_identity_gap)
    financial_dirs = find_financial_artifact_dirs(root, target, request)
    financial_draft_layer = audit_financial_draft_layer(financial_dirs.evidence_dir)
    formal_financial_layer = audit_formal_financial_layer(financial_dirs.formal_dir, request)

    if collector_layer["status"] == "future_incompatible":
        processor_layer = block_layer_for_future_cutoff(processor_layer, "Information processing layer")
        financial_draft_layer = block_layer_for_future_cutoff(financial_draft_layer, "Financial evidence draft layer")
        formal_financial_layer = block_layer_for_future_cutoff(formal_financial_layer, "Formal financial analysis layer")

    identity = (
        build_filing_identity(selection.main_record or {}, pdf_sha256=processor_pdf_sha256)
        if selection.main_record
        else {}
    )
    fingerprint = calculate_financial_input_fingerprint([identity]) if identity else ""
    valuation_layer = audit_valuation_layer(root, target, request)
    market_context_layer = audit_market_context_layer(root, target, request)
    layers = {
        "collector": collector_layer,
        "processor": processor_layer,
        "financial_evidence_draft": financial_draft_layer,
        "formal_financial_analysis": formal_financial_layer,
        "valuation": valuation_layer,
        "market_context": market_context_layer,
    }
    reusable = build_reusable_flags(layers)
    next_actions = build_next_actions(layers, request)
    skipped_actions = build_skipped_actions(layers, request, next_actions)
    filing_entry = build_single_filing_state_entry(target, collector_layer, processor_layer, identity)
    return {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_cutoff": request.as_of_date,
        "request": request_to_dict(request),
        "filing_policy": request.filing_policy,
        "filing_plan": [],
        "filings": [filing_entry] if filing_entry else [],
        "financial_input_fingerprint": fingerprint,
        "target": target,
        "layers": layers,
        "reusable": reusable,
        "skipped_actions": skipped_actions,
        "next_actions": next_actions,
        "summary": build_summary(layers, reusable, skipped_actions, next_actions),
    }


def audit_recent_history_state(root: Path, request: ResearchAuditRequest) -> dict[str, Any]:
    """审计截止日前近期年报与中报集合，并生成逐份续跑状态。"""
    collector_workspace = root / "info_collector_scripts" / "collector_workspace"
    manifest_path = collector_workspace / "manifests" / "cninfo_all_reports.json"
    records = load_json_list(manifest_path)
    company_records = [record for record in records if record_matches_company(record, request)]
    plan_items = derive_recent_filing_plan(request.as_of_date, annual_lookback=request.annual_lookback)
    entries = [
        audit_planned_filing(root, request, collector_workspace, manifest_path, company_records, item.to_dict())
        for item in plan_items
    ]
    mark_required_recent_filings(entries, request.annual_lookback)
    required_entries = [entry for entry in entries if entry.get("required")]
    available_entries = [entry for entry in required_entries if entry.get("selected_record")]
    primary_entry = choose_primary_filing_entry(available_entries)
    primary_record = primary_entry.get("selected_record") if primary_entry else None
    target = build_target_from_records(request, primary_record, company_records)

    identities = [entry["identity"] for entry in available_entries if entry.get("identity")]
    fingerprint = calculate_financial_input_fingerprint(identities) if identities else ""
    collector_layer = aggregate_recent_layer(required_entries, "collector")
    processor_layer = aggregate_recent_layer(required_entries, "processor")

    filing_set_dir = (
        root
        / "financial_analyst_scripts"
        / "analyst_workspace"
        / "filing_sets"
        / (target.get("stock_code") or "unknown_code")
        / request.as_of_date
    )
    financial_draft_layer = audit_filing_set_layer(filing_set_dir, fingerprint)
    formal_financial_layer = audit_formal_financial_layer(
        filing_set_dir,
        request,
        financial_input_fingerprint=fingerprint,
    )
    valuation_layer = audit_valuation_layer(root, target, request, financial_input_fingerprint=fingerprint)
    market_context_layer = audit_market_context_layer(root, target, request)
    layers = {
        "collector": collector_layer,
        "processor": processor_layer,
        "financial_evidence_draft": financial_draft_layer,
        "formal_financial_analysis": formal_financial_layer,
        "valuation": valuation_layer,
        "market_context": market_context_layer,
    }
    reusable = build_reusable_flags(layers)
    next_actions = build_recent_next_actions(required_entries, layers, request)
    skipped_actions = build_skipped_actions(layers, request, next_actions)
    state = {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_cutoff": request.as_of_date,
        "request": request_to_dict(request),
        "filing_policy": request.filing_policy,
        "filing_plan": [dict(item.to_dict(), required=next((e.get("required", False) for e in entries if e["report_type"] == item.report_type and e["report_year"] == item.report_year), False)) for item in plan_items],
        "filings": required_entries,
        "financial_input_fingerprint": fingerprint,
        "target": target,
        "layers": layers,
        "reusable": reusable,
        "skipped_actions": skipped_actions,
        "next_actions": next_actions,
        "summary": build_summary(layers, reusable, skipped_actions, next_actions),
    }
    return state


def record_matches_company(record: dict[str, Any], request: ResearchAuditRequest) -> bool:
    """只按公司身份过滤记录，故意忽略单份财报类型和财年字段。"""
    if request.stock_code and str(record.get("stock_code") or "") != request.stock_code:
        return False
    if request.company_name:
        haystack = f"{record.get('company_name', '')} {record.get('title', '')}"
        if request.company_name not in haystack:
            return False
    return bool(request.stock_code or request.company_name or request.target)


def audit_planned_filing(
    root: Path,
    request: ResearchAuditRequest,
    collector_workspace: Path,
    manifest_path: Path,
    company_records: list[dict[str, Any]],
    plan_item: dict[str, Any],
) -> dict[str, Any]:
    """审计一个候选类型/财年对应的采集与处理产物。"""
    report_type = str(plan_item["report_type"])
    report_year = str(plan_item["report_year"])
    matched = [
        record
        for record in company_records
        if str(record.get("report_type") or "") == report_type
        and str(record.get("report_year") or "") == report_year
    ]
    cutoff = parse_strict_iso_date(request.as_of_date, "as_of_date")
    eligible, future, undated = classify_report_records_by_cutoff(matched, cutoff)
    main_record = choose_best_record(
        [record for record in eligible if not is_summary_record(record) and not is_english_record(record)],
        collector_workspace,
    )
    summary_record = None
    if report_type == "annual":
        summary_record = choose_best_record([record for record in eligible if is_summary_record(record)], collector_workspace)
    main_pdf = resolve_record_pdf_path(collector_workspace, main_record)
    if not manifest_path.exists():
        collector_status = "missing"
        collector_gaps = ["The financial-report manifest does not exist."]
    elif not main_record:
        collector_status = "missing"
        collector_gaps = [f"No eligible official {report_type} filing for fiscal year {report_year} is recorded on or before the cutoff."]
    elif not main_pdf or not main_pdf.exists():
        collector_status = "partial"
        collector_gaps = ["The official filing record exists, but its local PDF is missing."]
    else:
        collector_status = "ready"
        collector_gaps = []
    target = {
        "stock_code": str((main_record or {}).get("stock_code") or request.stock_code or ""),
        "company_name": str((main_record or {}).get("company_name") or request.company_name or request.target or ""),
        "report_year": report_year,
        "report_type": report_type,
        "report_stem": Path(str((main_record or {}).get("local_relative_path") or "")).stem,
        "announcement_id": str((main_record or {}).get("announcement_id") or ""),
        "published_at": str((main_record or {}).get("published_at") or ""),
    }
    summary_required = bool(report_type == "annual" and summary_record)
    processor = audit_processor_layer(root, target, main_record, summary_required=summary_required)
    processor_pdf_sha256, processor_identity_gap = verified_processor_pdf_hash(root, processor, main_record)
    processor = apply_processor_identity_gap(processor, processor_identity_gap)
    identity = build_filing_identity(main_record or {}, pdf_sha256=processor_pdf_sha256) if main_record else {}
    return {
        "filing_id": filing_id(identity) if identity else f"{target['stock_code'] or 'unknown'}:{report_type}:{report_year}:pending",
        "role": plan_item.get("role"),
        "required": False,
        "expected_by_cutoff": bool(plan_item.get("expected_by_cutoff", True)),
        "report_type": report_type,
        "report_year": report_year,
        "period_months": int(plan_item.get("period_months") or PERIOD_MONTHS[report_type]),
        "disclosure_window": {
            "start": plan_item.get("disclosure_start"),
            "end": plan_item.get("disclosure_end"),
        },
        "identity": identity,
        "selected_record": trim_record(main_record),
        "summary_record": trim_record(summary_record),
        "summary_comparison": "required" if summary_required else "not_applicable",
        "collector": {
            "status": collector_status,
            "artifacts": {"main_pdf": path_state(main_pdf), "summary_pdf": path_state(resolve_record_pdf_path(collector_workspace, summary_record))},
            "date_audit": {
                "cutoff": request.as_of_date,
                "matched_count": len(matched),
                "eligible_count": len(eligible),
                "future_count": len(future),
                "undated_count": len(undated),
            },
            "gaps": collector_gaps,
        },
        "processor": processor,
    }


def mark_required_recent_filings(entries: list[dict[str, Any]], annual_lookback: int) -> None:
    """原地标记真正必需的年报基线和所有已开放中报候选。"""
    annual_entries = sorted(
        [entry for entry in entries if entry["report_type"] == "annual"],
        key=lambda entry: int(entry["report_year"]),
        reverse=True,
    )
    available = [entry for entry in annual_entries if entry.get("selected_record")]
    selected = available[:annual_lookback]
    for entry in selected:
        entry["required"] = True
        entry["role"] = "latest_annual" if entry is selected[0] else "historical_annual"
    missing_needed = annual_lookback - len(selected)
    if missing_needed > 0:
        for entry in annual_entries:
            if entry.get("required") or entry.get("selected_record"):
                continue
            entry["required"] = True
            entry["role"] = "missing_annual_baseline"
            missing_needed -= 1
            if missing_needed == 0:
                break
    for entry in entries:
        if entry["report_type"] in INTERIM_REPORT_TYPES:
            # 尚未到常规披露截止日的当前期中报只做发现查询；一旦实际披露就立即纳入。
            entry["required"] = bool(entry.get("selected_record") or entry.get("expected_by_cutoff"))


def choose_primary_filing_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """选择截止日前最新披露的财报作为旧顶层 target 的兼容目标。"""
    if not entries:
        return None
    return sorted(
        entries,
        key=lambda entry: (
            str((entry.get("selected_record") or {}).get("published_at") or ""),
            int(entry.get("report_year") or 0),
            int(entry.get("period_months") or 0),
        ),
        reverse=True,
    )[0]


def aggregate_recent_layer(entries: list[dict[str, Any]], layer_name: str) -> dict[str, Any]:
    """把逐份状态聚合成兼容旧 console 的顶层层状态。"""
    statuses = [str((entry.get(layer_name) or {}).get("status") or "missing") for entry in entries]
    if statuses and all(status == "ready" for status in statuses):
        status = "ready"
    elif statuses and all(status in {"missing", "blocked", "future_incompatible"} for status in statuses):
        status = "missing"
    else:
        status = "partial"
    gaps: list[str] = []
    for entry in entries:
        layer = entry.get(layer_name) or {}
        for gap in layer.get("gaps") or []:
            gaps.append(f"{entry['report_type']} {entry['report_year']}: {gap}")
    return {
        "status": status,
        "filing_count": len(entries),
        "ready_count": sum(1 for item in statuses if item == "ready"),
        "filings": [
            {
                "filing_id": entry["filing_id"],
                "report_type": entry["report_type"],
                "report_year": entry["report_year"],
                "status": (entry.get(layer_name) or {}).get("status"),
            }
            for entry in entries
        ],
        "gaps": gaps,
    }


def build_recent_next_actions(
    entries: list[dict[str, Any]],
    layers: dict[str, dict[str, Any]],
    request: ResearchAuditRequest,
) -> list[dict[str, str]]:
    """按财报逐份生成采集/处理动作，再衔接财务分析和估值动作。"""
    actions: list[dict[str, str]] = []
    if request.force_refresh:
        return build_next_actions(layers, request)
    for entry in entries:
        collector_status = str((entry.get("collector") or {}).get("status") or "missing")
        if collector_status != "ready":
            item = action(
                "collector_fetch",
                "information-collector",
                f"Collect the eligible {entry['report_type']} filing for fiscal year {entry['report_year']} within the cutoff-safe disclosure window.",
            )
            item["filing_id"] = entry["filing_id"]
            item["report_type"] = entry["report_type"]
            item["report_year"] = entry["report_year"]
            actions.append(item)
    if actions:
        return actions
    for entry in entries:
        processor_status = str((entry.get("processor") or {}).get("status") or "missing")
        if processor_status == "ready":
            continue
        for item in build_processor_next_actions(entry.get("processor") or {}):
            item["filing_id"] = entry["filing_id"]
            item["report_type"] = entry["report_type"]
            item["report_year"] = entry["report_year"]
            actions.append(item)
    if actions:
        actions.extend(build_market_context_next_actions(layers["market_context"]))
        return actions
    return build_next_actions(layers, request)


def build_single_filing_state_entry(
    target: dict[str, str],
    collector_layer: dict[str, Any],
    processor_layer: dict[str, Any],
    identity: dict[str, str],
) -> dict[str, Any]:
    """把旧单份状态映射为 schema 2.0 的统一 filings 条目。"""
    if not any(target.values()):
        return {}
    report_type = target.get("report_type") or "annual"
    return {
        "filing_id": filing_id(identity) if identity else f"{target.get('stock_code') or 'unknown'}:{report_type}:{target.get('report_year') or 'unknown'}:pending",
        "role": "explicit_single_filing",
        "required": True,
        "report_type": report_type,
        "report_year": target.get("report_year") or "",
        "period_months": PERIOD_MONTHS.get(report_type, 12),
        "identity": identity,
        "selected_record": collector_layer.get("selected_record") or {},
        "summary_record": collector_layer.get("summary_record") or {},
        "summary_comparison": (processor_layer.get("artifacts") or {}).get("summary_comparison_applicability", {}).get("status", "required"),
        "collector": collector_layer,
        "processor": processor_layer,
    }


def parse_strict_iso_date(value: Any, field_name: str) -> date:
    """严格解析 ``YYYY-MM-DD`` 日期，拒绝宽松或带时间的输入。

    为什么额外校验格式：``date.fromisoformat`` 在不同 Python 版本可能接受紧凑日期等
    ISO 变体；知识截止日会直接决定哪些证据可见，因此必须把外部契约固定为十位日期。

    参数：
        value: 待解析日期值。
        field_name: 错误消息中的字段名。
    返回值：
        解析后的 date。
    """
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{field_name} must be a strict ISO date in YYYY-MM-DD format; received: {text or '<empty>'}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid date: {text}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field_name} must be a strict ISO date in YYYY-MM-DD format; received: {text}")
    return parsed


def normalize_request(request: ResearchAuditRequest) -> ResearchAuditRequest:
    """标准化请求参数，统一股票代码、年度、深度和 focus 的表示。

    参数：
        request: 原始请求。
    返回值：
        标准化后的请求。
    """
    target = str(request.target or "").strip()
    stock_code = str(request.stock_code or "").strip()
    company_name = str(request.company_name or "").strip()
    if not stock_code and target.isdigit() and len(target) == 6:
        stock_code = target
    if not company_name and target and not (target.isdigit() and len(target) == 6):
        company_name = target
    depth = request.depth if request.depth in DEPTH_RANK else "standard"
    as_of_date = str(request.as_of_date or "").strip()
    if as_of_date:
        as_of_date = parse_strict_iso_date(as_of_date, "as_of_date").isoformat()
    report_year = str(request.report_year or "").strip()
    report_type = str(request.report_type or "").strip().lower()
    filing_policy = normalize_filing_policy(
        request.filing_policy,
        report_type=report_type,
        report_year=report_year,
    )
    if filing_policy == "recent_history" and not as_of_date:
        as_of_date = date.today().isoformat()
    if filing_policy == "single_filing" and not report_type:
        report_type = "annual"
    annual_lookback = int(request.annual_lookback or 2)
    if annual_lookback < 1 or annual_lookback > 5:
        raise ValueError("annual_lookback must be between 1 and 5")
    return ResearchAuditRequest(
        target=target,
        stock_code=stock_code,
        company_name=company_name,
        report_year=report_year,
        report_type=report_type,
        filing_policy=filing_policy,
        annual_lookback=annual_lookback,
        depth=depth,
        focus=str(request.focus or "").strip(),
        as_of_date=as_of_date,
        force_refresh=bool(request.force_refresh),
        write_state=bool(request.write_state),
        output=str(request.output or "").strip(),
    )


def request_to_dict(request: ResearchAuditRequest) -> dict[str, Any]:
    """将请求 dataclass 转成 JSON 友好的字典。

    参数：
        request: 标准化请求。
    返回值：
        字典形式的请求。
    """
    return {
        "target": request.target,
        "stock_code": request.stock_code,
        "company_name": request.company_name,
        "report_year": request.report_year,
        "report_type": request.report_type,
        "filing_policy": request.filing_policy,
        "annual_lookback": request.annual_lookback,
        "depth": request.depth,
        "focus": request.focus,
        "as_of_date": request.as_of_date,
        "force_refresh": request.force_refresh,
    }


def audit_collector_layer(root: Path, request: ResearchAuditRequest) -> tuple[dict[str, Any], CollectorSelection]:
    """审计财报采集层，定位 manifest、正式年报 PDF 和摘要 PDF。

    参数：
        root: 项目根目录。
        request: 标准化请求。
    返回值：
        二元组：采集层状态、目标选择结果。
    """
    collector_workspace = root / "info_collector_scripts" / "collector_workspace"
    manifest_path = collector_workspace / "manifests" / "cninfo_all_reports.json"
    records = load_json_list(manifest_path)
    selection = select_collector_records(records, request, collector_workspace)

    main_pdf_path = resolve_record_pdf_path(collector_workspace, selection.main_record)
    summary_pdf_path = resolve_record_pdf_path(collector_workspace, selection.summary_record)
    gaps: list[str] = []
    status = "ready"
    future_main_records = [
        record for record in selection.future_records if not is_summary_record(record) and not is_english_record(record)
    ]
    undated_main_records = [
        record for record in selection.undated_records if not is_summary_record(record) and not is_english_record(record)
    ]

    if selection.ambiguous_choices:
        status = "ambiguous"
        gaps.append("The target matched multiple stock codes or report years. Specify stock_code/report_year first.")
    elif not manifest_path.exists():
        status = "missing"
        gaps.append("The financial-report manifest does not exist. Run information-collector first.")
    elif not selection.main_record and future_main_records and not undated_main_records:
        status = "future_incompatible"
        gaps.append(
            f"All matched official reports were published after the knowledge cutoff {request.as_of_date} and cannot be used for historical as-of-date research."
        )
    elif not selection.main_record:
        status = "missing"
        gaps.append("The manifest contains no matched official report published on or before the knowledge cutoff.")
        if undated_main_records:
            gaps.append("Some official report records have missing or unparseable published_at values and cannot be selected for historical auditing.")
    elif not main_pdf_path or not main_pdf_path.exists():
        status = "partial"
        gaps.append("An official report PDF record exists, but the local file is missing and must be downloaded.")

    layer = {
        "status": status,
        "manifest_path": path_state(manifest_path),
        "artifacts": {
            "main_pdf": path_state(main_pdf_path),
            "summary_pdf": path_state(summary_pdf_path),
        },
        "selected_record": trim_record(selection.main_record),
        "summary_record": trim_record(selection.summary_record),
        "ambiguous_choices": selection.ambiguous_choices,
        "date_audit": {
            "cutoff": request.as_of_date,
            "matched_count": len(selection.matched_records),
            "eligible_count": len(selection.eligible_records),
            "future_count": len(selection.future_records),
            "undated_count": len(selection.undated_records),
            "future_main_count": len(future_main_records),
            "undated_main_count": len(undated_main_records),
            "future_record_samples": [trim_record(record) for record in selection.future_records[:20]],
            "undated_record_samples": [trim_record(record) for record in selection.undated_records[:20]],
        },
        "gaps": gaps,
    }
    return layer, selection


def select_collector_records(
    records: list[dict[str, Any]], request: ResearchAuditRequest, collector_workspace: Path
) -> CollectorSelection:
    """从 manifest 记录中选择目标公司和财报记录。

    参数：
        records: manifest 记录列表。
        request: 标准化请求。
        collector_workspace: 信息收集员工作区。
    返回值：
        CollectorSelection。
    """
    all_matched = [record for record in records if record_matches_request(record, request)]
    cutoff = parse_strict_iso_date(request.as_of_date, "as_of_date") if request.as_of_date else None
    eligible_all, _, _ = classify_report_records_by_cutoff(all_matched, cutoff)

    if not request.report_year and all_matched:
        # 自动财年选择必须基于截止日前可知记录；否则未来年度会把当时已披露的旧年度挤掉。
        year_source = eligible_all or all_matched
        latest_year = select_latest_report_year(year_source)
        matched = [record for record in all_matched if str(record.get("report_year", "")) == latest_year]
    else:
        matched = all_matched

    eligible, future, undated = classify_report_records_by_cutoff(matched, cutoff)
    target_choices = sorted(
        {
            (
                str(record.get("stock_code", "")),
                str(record.get("company_name", "")),
                str(record.get("report_year", "")),
            )
            for record in matched
            if record.get("stock_code") or record.get("company_name")
        }
    )
    ambiguous_choices: list[dict[str, str]] = []
    if len({choice[0] for choice in target_choices if choice[0]}) > 1:
        ambiguous_choices = [
            {"stock_code": code, "company_name": name, "report_year": year} for code, name, year in target_choices
        ]

    # 有知识截止日时只允许从 eligible 中选正式版和摘要版；未来/无日期记录仅进入审计统计。
    selectable = eligible if cutoff else matched
    main_candidates = [record for record in selectable if not is_summary_record(record) and not is_english_record(record)]
    summary_candidates = [record for record in selectable if is_summary_record(record)]
    main_record = choose_best_record(main_candidates, collector_workspace)
    summary_record = choose_best_record(summary_candidates, collector_workspace)
    target = build_target_from_records(request, main_record, matched)
    return CollectorSelection(
        target=target,
        matched_records=matched,
        eligible_records=eligible,
        future_records=future,
        undated_records=undated,
        main_record=main_record,
        summary_record=summary_record,
        ambiguous_choices=ambiguous_choices,
    )


def record_matches_request(record: dict[str, Any], request: ResearchAuditRequest) -> bool:
    """判断 manifest 记录是否匹配本次请求。

    参数：
        record: 单条 manifest 记录。
        request: 标准化请求。
    返回值：
        匹配返回 True，否则返回 False。
    """
    if request.stock_code and str(record.get("stock_code", "")) != request.stock_code:
        return False
    if request.company_name:
        haystack = f"{record.get('company_name', '')} {record.get('title', '')}"
        if request.company_name not in haystack:
            return False
    if request.report_type and str(record.get("report_type", "")) != request.report_type:
        return False
    if request.report_year and str(record.get("report_year", "")) != request.report_year:
        return False
    if not request.stock_code and not request.company_name and not request.target:
        return False
    return True


def parse_record_published_date(record: dict[str, Any]) -> date | None:
    """解析 manifest 记录的披露日期。

    功能：
        同时兼容 ``YYYY-MM-DD`` 与标准 ISO datetime；空值、非法值返回 None，
        让调用方把记录明确归入 undated，而不是误当成截止日前证据。
    参数：
        record: manifest 记录。
    返回值：
        披露日期；缺失或非法时返回 None。
    """
    raw_value = record.get("published_at")
    text = str(raw_value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        # 巨潮历史记录可能保存 ISO datetime；只比较其日历日期，避免时区时刻改变披露日边界。
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.date()


def classify_report_records_by_cutoff(
    records: list[dict[str, Any]], cutoff: date | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """按知识截止日把财报记录分为可用、未来和无日期三组。

    参数：
        records: 已按公司、报告类型和可选财年过滤的 manifest 记录。
        cutoff: 知识截止日；None 表示保持旧行为，全部记录可选。
    返回值：
        ``(eligible, future, undated)`` 三元组。
    """
    if cutoff is None:
        return list(records), [], []
    eligible: list[dict[str, Any]] = []
    future: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    for record in records:
        published_date = parse_record_published_date(record)
        if published_date is None:
            undated.append(record)
        elif published_date <= cutoff:
            eligible.append(record)
        else:
            future.append(record)
    return eligible, future, undated


def select_latest_report_year(records: list[dict[str, Any]]) -> str:
    """选择命中记录中的最新财报年度。

    参数：
        records: manifest 记录列表。
    返回值：
        最新年度字符串；没有年度时返回空字符串。
    """
    years = [str(record.get("report_year", "")) for record in records if str(record.get("report_year", "")).isdigit()]
    return max(years) if years else ""


def choose_best_record(records: list[dict[str, Any]], collector_workspace: Path) -> dict[str, Any] | None:
    """从候选记录中选择最适合复用的一条。

    参数：
        records: 候选 manifest 记录。
        collector_workspace: 信息收集员工作区。
    返回值：
        最佳记录；没有候选时返回 None。
    """
    if not records:
        return None

    def score(record: dict[str, Any]) -> tuple[str, int, int]:
        # 披露日期必须优先于本地存在性：较新的修订版即使尚未下载，也应被选中并把
        # collector 标成 partial，随后精确补下载；否则旧本地文件会永久遮蔽正式修订。
        pdf_path = resolve_record_pdf_path(collector_workspace, record)
        exists_score = 1 if pdf_path and pdf_path.exists() else 0
        classification = str(record.get("title_classification", ""))
        class_score = 1 if classification.endswith("_full") or classification == "report_full" else 0
        date_score = str(record.get("published_at", ""))
        return date_score, class_score, exists_score

    return sorted(records, key=score, reverse=True)[0]


def is_summary_record(record: dict[str, Any]) -> bool:
    """判断记录是否为摘要版报告。

    参数：
        record: manifest 记录。
    返回值：
        是摘要版返回 True。
    """
    title = str(record.get("title", ""))
    classification = str(record.get("title_classification", ""))
    record_kind = str(record.get("record_kind", ""))
    return "摘要" in title or "summary" in classification or record_kind == "summary"


def is_english_record(record: dict[str, Any]) -> bool:
    """判断记录是否为英文版报告。

    参数：
        record: manifest 记录。
    返回值：
        是英文版返回 True。
    """
    title = str(record.get("title", ""))
    classification = str(record.get("title_classification", ""))
    return "英文" in title or "english" in classification.lower()


def build_target_from_records(
    request: ResearchAuditRequest, main_record: dict[str, Any] | None, matched: list[dict[str, Any]]
) -> dict[str, str]:
    """基于请求和 manifest 记录构造标准目标信息。

    参数：
        request: 标准化请求。
        main_record: 已选正式财报记录。
        matched: 所有命中记录。
    返回值：
        标准目标信息。
    """
    source = main_record or (matched[0] if matched else {})
    return {
        "stock_code": str(source.get("stock_code") or request.stock_code or ""),
        "company_name": str(source.get("company_name") or request.company_name or request.target or ""),
        "report_year": str(source.get("report_year") or request.report_year or ""),
        "report_type": str(source.get("report_type") or request.report_type or "annual"),
        "report_stem": Path(str(source.get("local_relative_path", ""))).stem if source.get("local_relative_path") else "",
        "announcement_id": str(source.get("announcement_id") or ""),
        "published_at": str(source.get("published_at") or ""),
    }


def resolve_record_pdf_path(collector_workspace: Path, record: dict[str, Any] | None) -> Path | None:
    """把 manifest 中的相对路径解析成本地 PDF 路径。

    参数：
        collector_workspace: 信息收集员工作区。
        record: manifest 记录。
    返回值：
        本地 PDF 路径；无记录或无路径时返回 None。
    """
    if not record:
        return None
    relative_path = str(record.get("local_relative_path", ""))
    if not relative_path:
        return None
    return collector_workspace / relative_path


def audit_processor_layer(
    root: Path,
    target: dict[str, str],
    main_record: dict[str, Any] | None,
    *,
    summary_required: bool = True,
) -> dict[str, Any]:
    """审计信息处理层，检查解析、digest、RAG 和按需摘要比对产物。

    参数：
        root: 项目根目录。
        target: 标准目标信息。
        main_record: 正式财报记录。
    返回值：
        信息处理层状态。
    """
    report_dir = find_processor_report_dir(root, target, main_record)
    artifacts = build_processor_artifacts(report_dir)
    required_keys = [key for key in PROCESSOR_REQUIRED_KEYS if summary_required or key != "summary_comparison_json"]
    missing = [key for key in required_keys if not artifacts[key]["exists"]]
    gaps = [f"Missing {key}" for key in missing]
    artifacts["summary_comparison_applicability"] = {
        "status": "required" if summary_required else "not_applicable",
        "exists": artifacts["summary_comparison_json"]["exists"] if summary_required else False,
    }
    digest_audit = load_json(Path(artifacts["digest_audit_json"]["path"])) if artifacts["digest_audit_json"]["path"] else {}
    missing_chunks = digest_audit.get("missing_chunks", []) if isinstance(digest_audit, dict) else []
    invalid_results = digest_audit.get("invalid_results", []) if isinstance(digest_audit, dict) else []
    if missing_chunks:
        gaps.append(f"digest_audit reports {len(missing_chunks)} missing chunks")
    if invalid_results:
        gaps.append(f"digest_audit reports {len(invalid_results)} invalid chunks")

    if not report_dir or not report_dir.exists():
        status = "missing"
        gaps.append("Information-processor report directory not found.")
    elif not missing and not missing_chunks and not invalid_results:
        status = "ready"
    else:
        status = "partial"

    return {
        "status": status,
        "report_dir": path_state(report_dir),
        "artifacts": artifacts,
        "quality_flags": {
            "missing_required_artifacts": missing,
            "missing_digest_chunks": missing_chunks,
            "invalid_digest_results": invalid_results,
        },
        "gaps": gaps,
    }


def block_layer_for_future_cutoff(layer: dict[str, Any], layer_name: str) -> dict[str, Any]:
    """把本地已存在的下游产物标记为受未来财报阻断。

    为什么保留原 artifacts：审计仍需要说明本地文件确实存在，但状态必须降为 blocked，
    防止历史基准日研究把未来披露后的解析或分析结果误判为 ready。

    参数：
        layer: 已完成普通文件盘点的层状态。
        layer_name: 用于缺口说明的中文层名。
    返回值：
        保留原审计细节、但状态改为 blocked 的新字典。
    """
    blocked = dict(layer)
    gaps = list(layer.get("gaps", []))
    gaps.insert(0, f"The official report used by {layer_name} is later than the knowledge cutoff; local future artifacts cannot be reused.")
    blocked["status"] = "blocked"
    blocked["blocked_by"] = "collector.future_incompatible"
    blocked["gaps"] = gaps
    return blocked


def find_processor_report_dir(root: Path, target: dict[str, str], main_record: dict[str, Any] | None) -> Path | None:
    """定位信息处理员的单份报告目录。

    参数：
        root: 项目根目录。
        target: 标准目标信息。
        main_record: 正式财报记录。
    返回值：
        最佳报告目录；找不到返回 None。
    """
    processor_workspace = root / "info_processor_scripts" / "processor_workspace" / "parsed_reports"
    report_type = target.get("report_type", "annual")
    report_year = target.get("report_year", "")
    stock_code = target.get("stock_code", "")
    if main_record and main_record.get("local_relative_path"):
        stem = Path(str(main_record["local_relative_path"])).stem
        exact = processor_workspace / report_type / report_year / stock_code / stem
        if exact.exists():
            # 已选公告的目录即使尚未补齐，也不能被同财年摘要版或旧修订版的完整目录替换。
            return exact
        if stock_code and report_year:
            for candidate in (processor_workspace / report_type / report_year / stock_code).glob("*"):
                if processor_dir_matches_record(candidate, main_record):
                    return candidate
        return exact

    candidates: list[Path] = []
    if stock_code and report_year:
        candidates.extend((processor_workspace / report_type / report_year / stock_code).glob("*"))
    if target.get("report_stem"):
        candidates.extend(processor_workspace.glob(f"**/{target['report_stem']}"))
    return choose_best_dir(candidates, ["content.json", "llm_digest.json", "digest_audit.json"])


def processor_dir_matches_record(report_dir: Path, main_record: dict[str, Any]) -> bool:
    """核验处理目录是否确实来自当前选中的公告记录。"""
    content = load_json(report_dir / "content.json")
    metadata = content.get("document_metadata", {}) if isinstance(content, dict) else {}
    expected_announcement = str(main_record.get("announcement_id") or "")
    actual_announcement = str(metadata.get("announcement_id") or content.get("announcement_id") or "")
    if expected_announcement and actual_announcement:
        return expected_announcement == actual_announcement
    expected_stem = Path(str(main_record.get("local_relative_path") or "")).stem
    actual_stem = str(metadata.get("pdf_stem") or report_dir.name)
    return bool(expected_stem and expected_stem == actual_stem)


def verified_processor_pdf_hash(
    root: Path,
    processor_layer: dict[str, Any],
    main_record: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """从与当前公告匹配的 ``content.json`` 提取 PDF 指纹。

    功能：
        采集清单通常只记录公告身份，不一定保存下载文件的 SHA-256；信息处理层的
        ``content.json`` 才保存实际处理文件的哈希。本函数把两者连接起来，但只在
        处理目录、公告编号和源 PDF 路径都没有冲突时接受该哈希。

    参数：
        root: 项目根目录，用于解析相对路径。
        processor_layer: ``audit_processor_layer`` 返回的处理层状态。
        main_record: 当前审计选中的正式财报清单记录。

    返回值：
        ``(pdf_sha256, identity_gap)``。缺少哈希不是错误，返回空字符串和 ``None``；
        如果处理产物明确属于另一份财报，则返回空哈希和可展示的身份缺口。
    """
    if not main_record:
        return "", None
    artifacts = processor_layer.get("artifacts") if isinstance(processor_layer, dict) else {}
    content_info = artifacts.get("content_json") if isinstance(artifacts, dict) else {}
    raw_path = content_info.get("path") if isinstance(content_info, dict) else ""
    if not raw_path:
        return "", None
    content_path = Path(str(raw_path))
    if not content_path.is_absolute():
        content_path = root / content_path
    if not content_path.exists():
        return "", None

    content = load_json(content_path)
    if not isinstance(content, dict):
        return "", "The selected processor content.json is not a valid JSON object."

    # 先复用现有的公告编号/文件名匹配规则，避免从同一财年其他修订版目录借用哈希。
    if not processor_dir_matches_record(content_path.parent, main_record):
        return "", "The selected processor content does not match the selected filing identity."

    expected_pdf = resolve_record_pdf_path(
        root / "info_collector_scripts" / "collector_workspace",
        main_record,
    )
    source_pdf_path = content.get("source_pdf_path")
    if not source_pdf_path:
        metadata = content.get("document_metadata")
        if isinstance(metadata, dict):
            source_pdf_path = metadata.get("source_pdf_path")
    if source_pdf_path and expected_pdf:
        actual_path = Path(str(source_pdf_path))
        if not actual_path.is_absolute():
            actual_path = root / actual_path
        try:
            paths_match = actual_path.resolve() == expected_pdf.resolve()
        except OSError:
            paths_match = str(actual_path) == str(expected_pdf)
        if not paths_match:
            return "", "The selected processor content points to a different source PDF."

    pdf_sha256 = str(content.get("pdf_sha256") or "").strip()
    return pdf_sha256, None


def apply_processor_identity_gap(
    processor_layer: dict[str, Any],
    identity_gap: str | None,
) -> dict[str, Any]:
    """把处理产物身份冲突转换成不可复用的处理层状态。"""
    if not identity_gap:
        return processor_layer
    updated = dict(processor_layer)
    gaps = list(updated.get("gaps") or [])
    if identity_gap not in gaps:
        gaps.append(identity_gap)
    updated["gaps"] = gaps
    if str(updated.get("status") or "") == "ready":
        updated["status"] = "partial"
    return updated


def build_processor_artifacts(report_dir: Path | None) -> dict[str, dict[str, Any]]:
    """生成信息处理层关键产物路径状态。

    参数：
        report_dir: 信息处理员报告目录。
    返回值：
        产物键到路径状态的映射。
    """
    if not report_dir:
        return {key: path_state(None) for key in [*PROCESSOR_REQUIRED_KEYS, "llm_digest_md", "summary_comparison_md"]}
    return {
        "content_json": path_state(report_dir / "content.json"),
        "content_md": path_state(report_dir / "content.md"),
        "llm_digest_json": path_state(report_dir / "llm_digest.json"),
        "llm_digest_md": path_state(report_dir / "llm_digest.md"),
        "digest_audit_json": path_state(report_dir / "digest_audit.json"),
        "rag_chunks_jsonl": path_state(report_dir / "rag_index" / "rag_chunks.jsonl"),
        "summary_comparison_json": path_state(report_dir / "summary_comparison.json"),
        "summary_comparison_md": path_state(report_dir / "summary_comparison.md"),
    }


def find_financial_artifact_dirs(
    root: Path, target: dict[str, str], request: ResearchAuditRequest
) -> FinancialArtifacts:
    """定位财务证据草稿和正式财务分析目录。

    历史模式优先选择 ``<report_dir>/as_of/<as_of_date>``。只要精确 dated 目录存在，
    即使产物不完整也不回退到旧根目录，避免根目录中的未来分析覆盖历史快照。

    参数：
        root: 项目根目录。
        target: 标准目标信息。
        request: 标准化审计请求。
    返回值：
        FinancialArtifacts。
    """
    analyst_workspace = root / "financial_analyst_scripts" / "analyst_workspace"
    report_type = target.get("report_type", "annual")
    report_year = target.get("report_year", "")
    stock_code = target.get("stock_code", "")
    company_name = target.get("company_name", "")
    report_stem = target.get("report_stem", "")

    candidates: set[Path] = set()
    # 只有规范代码和财年同时存在时才允许构造精确目录。空组件会把路径折叠到
    # reports/<type>，进而把其他公司的年度目录误判成当前目标的部分财务产物。
    if report_year and stock_code:
        exact_root = analyst_workspace / "reports" / report_type / report_year / stock_code
        if exact_root.exists():
            candidates.update(path for path in exact_root.glob("*") if path.is_dir())
    if report_stem:
        candidates.update(path for path in analyst_workspace.glob(f"reports/**/{report_stem}"))
    if stock_code:
        candidates.update(path.parent for path in analyst_workspace.glob(f"reports/**/*{stock_code}*/analyst_report.json"))
        candidates.update(path.parent for path in analyst_workspace.glob(f"reports/**/*{stock_code}*/formal_financial_analysis.json"))
        candidates.add(analyst_workspace / stock_code / report_year)
    if company_name:
        candidates.update(path.parent for path in analyst_workspace.glob(f"reports/**/*{company_name}*/analyst_report.json"))
        candidates.update(path.parent for path in analyst_workspace.glob(f"reports/**/*{company_name}*/formal_financial_analysis.json"))

    existing_candidates = [path for path in candidates if path.exists()]
    evidence_dir = choose_best_dir(existing_candidates, FINANCIAL_DRAFT_REQUIRED_KEYS)

    dated_candidates: list[Path] = []
    if request.as_of_date:
        dated_candidates = [
            path / "as_of" / request.as_of_date
            for path in existing_candidates
            if (path / "as_of" / request.as_of_date).is_dir()
        ]
    formal_dir = choose_best_dir(dated_candidates, FORMAL_FINANCIAL_REQUIRED_KEYS)
    if not formal_dir:
        formal_dir = choose_best_dir(existing_candidates, FORMAL_FINANCIAL_REQUIRED_KEYS)
    if not formal_dir and evidence_dir and any((evidence_dir / key_to_filename(key)).exists() for key in FORMAL_FINANCIAL_REQUIRED_KEYS):
        formal_dir = evidence_dir
    return FinancialArtifacts(evidence_dir=evidence_dir, formal_dir=formal_dir)


def audit_filing_set_layer(filing_set_dir: Path, expected_fingerprint: str) -> dict[str, Any]:
    """审计公司级多期财报交接包，并核验其输入集合身份。"""
    filing_set_path = filing_set_dir / "filing_set.json"
    payload = load_json(filing_set_path)
    actual_fingerprint = str(payload.get("financial_input_fingerprint") or "") if isinstance(payload, dict) else ""
    if not filing_set_path.exists():
        status = "missing"
        gaps = ["filing_set.json is missing; build the multi-period financial evidence handoff."]
    elif expected_fingerprint and actual_fingerprint != expected_fingerprint:
        status = "incompatible"
        gaps = ["filing_set.json does not match the currently selected filing identities."]
    elif str((payload.get("quality") or {}).get("status") or "") == "partial":
        status = "partial"
        gaps = list((payload.get("quality") or {}).get("gaps") or [])
    else:
        status = "ready"
        gaps = []
    return {
        "status": status,
        "report_dir": path_state(filing_set_dir),
        "artifacts": {"filing_set_json": path_state(filing_set_path)},
        "analysis_metadata": {
            "financial_input_fingerprint": actual_fingerprint,
            "filing_count": int((payload.get("quality") or {}).get("filing_count") or 0) if isinstance(payload, dict) else 0,
        },
        "gaps": gaps,
    }


def audit_financial_draft_layer(evidence_dir: Path | None) -> dict[str, Any]:
    """审计财务证据草稿层。

    参数：
        evidence_dir: 财务证据草稿目录。
    返回值：
        财务证据草稿层状态。
    """
    artifacts = build_financial_draft_artifacts(evidence_dir)
    missing = [key for key in FINANCIAL_DRAFT_REQUIRED_KEYS if not artifacts[key]["exists"]]
    if not evidence_dir:
        status = "missing"
    elif missing:
        status = "partial"
    else:
        status = "ready"
    return {
        "status": status,
        "report_dir": path_state(evidence_dir),
        "artifacts": artifacts,
        "analysis_metadata": extract_analysis_metadata(artifacts["analyst_report_json"]["path"]),
        "gaps": [f"Missing {key}" for key in missing],
    }


def build_financial_draft_artifacts(evidence_dir: Path | None) -> dict[str, dict[str, Any]]:
    """生成财务证据草稿产物路径状态。

    参数：
        evidence_dir: 财务证据草稿目录。
    返回值：
        产物路径状态映射。
    """
    if not evidence_dir:
        return {key: path_state(None) for key in FINANCIAL_DRAFT_REQUIRED_KEYS}
    return {
        "analyst_report_json": path_state(evidence_dir / "analyst_report.json"),
        "analyst_report_md": path_state(evidence_dir / "analyst_report.md"),
        "evidence_check_json": path_state(evidence_dir / "evidence_check.json"),
        "analyst_audit_json": path_state(evidence_dir / "analyst_audit.json"),
    }


def audit_formal_financial_layer(
    formal_dir: Path | None,
    request: ResearchAuditRequest,
    *,
    financial_input_fingerprint: str = "",
) -> dict[str, Any]:
    """审计正式财务分析层，并判断 depth/focus 是否兼容。

    参数：
        formal_dir: 正式财务分析目录。
        request: 标准化请求。
    返回值：
        正式财务分析层状态。
    """
    artifacts = build_formal_financial_artifacts(formal_dir)
    missing = [key for key in FORMAL_FINANCIAL_REQUIRED_KEYS if not artifacts[key]["exists"]]
    # 结构化 JSON 是估值和审计的权威输入；Markdown 只是人类可读镜像。缺少镜像应保留
    # packaging gap，但不能把已经完成且截止合规的实质分析降为 partial 或触发重跑 Agent。
    missing_core = [key for key in missing if key == "formal_financial_analysis_json"]
    formal_payload = load_json(Path(artifacts["formal_financial_analysis_json"]["path"])) if artifacts["formal_financial_analysis_json"]["path"] else {}
    metadata = extract_analysis_metadata(artifacts["formal_financial_analysis_json"]["path"])
    compatibility = check_analysis_compatibility(metadata, request)
    cutoff_compatibility = check_formal_cutoff_compatibility(formal_payload, request)
    if not cutoff_compatibility["compatible"]:
        compatibility["compatible"] = False
        compatibility["reasons"].extend(cutoff_compatibility["reasons"])
    if financial_input_fingerprint and formal_payload:
        actual_fingerprint = str(
            formal_payload.get("financial_input_fingerprint")
            or (formal_payload.get("analysis_metadata") or {}).get("financial_input_fingerprint")
            or ""
        )
        if actual_fingerprint != financial_input_fingerprint:
            compatibility["compatible"] = False
            compatibility["reasons"].append(
                "The formal financial analysis was not generated from the currently selected filing set."
            )
    if not formal_dir:
        status = "missing"
    elif missing_core:
        status = "partial"
    elif not compatibility["compatible"]:
        status = "incompatible"
    else:
        status = "ready"
    gaps = [f"Missing {key}" for key in missing]
    gaps.extend(compatibility["reasons"])
    return {
        "status": status,
        "report_dir": path_state(formal_dir),
        "artifacts": artifacts,
        "analysis_metadata": metadata,
        "compatibility": compatibility,
        "cutoff_compatibility": cutoff_compatibility,
        "gaps": gaps,
    }


def build_formal_financial_artifacts(formal_dir: Path | None) -> dict[str, dict[str, Any]]:
    """生成正式财务分析产物路径状态。

    参数：
        formal_dir: 正式财务分析目录。
    返回值：
        产物路径状态映射。
    """
    if not formal_dir:
        return {key: path_state(None) for key in FORMAL_FINANCIAL_REQUIRED_KEYS}
    return {
        "formal_financial_analysis_json": path_state(formal_dir / "formal_financial_analysis.json"),
        "formal_financial_analysis_md": path_state(formal_dir / "formal_financial_analysis.md"),
    }


def audit_valuation_layer(
    root: Path,
    target: dict[str, str],
    request: ResearchAuditRequest,
    *,
    financial_input_fingerprint: str = "",
) -> dict[str, Any]:
    """审计估值层，并按 as_of_date 判断是否过期。

    参数：
        root: 项目根目录。
        target: 标准目标信息。
        request: 标准化请求。
    返回值：
        估值层状态。
    """
    stock_code = target.get("stock_code", "")
    if not stock_code:
        return {
            "status": "blocked",
            "report_dir": path_state(None),
            "artifacts": {key: path_state(None) for key in VALUATION_REQUIRED_KEYS},
            "latest_available_date": "",
            "gaps": ["Stock code is missing; valuation artifacts cannot be located."],
        }

    valuation_workspace = root / "valuation_analyst_scripts" / "valuation_workspace"
    candidates = find_valuation_candidates(valuation_workspace, stock_code)
    dated = classify_dated_candidates(candidates, request.as_of_date) if request.as_of_date else None
    exact = choose_latest_valuation(dated.exact) if dated else None
    before = choose_latest_valuation(dated.before) if dated else None
    latest = choose_latest_valuation(candidates)
    selected = (exact or before) if dated else latest
    report_dir = selected.parent if selected else None
    artifacts = build_valuation_artifacts(report_dir)
    missing = [key for key in VALUATION_REQUIRED_KEYS if not artifacts[key]["exists"]]
    valuation_audit = load_json(Path(artifacts["valuation_audit_json"]["path"])) if artifacts["valuation_audit_json"]["path"] else {}
    cutoff_compatibility = check_cutoff_audit(
        valuation_audit, request.as_of_date, "valuation_audit"
    ) if request.as_of_date and exact else {"compatible": True, "audit": {}, "reasons": []}
    if financial_input_fingerprint and exact:
        actual_fingerprint = str(valuation_audit.get("financial_input_fingerprint") or "")
        if actual_fingerprint != financial_input_fingerprint:
            cutoff_compatibility["compatible"] = False
            cutoff_compatibility["reasons"].append(
                "The valuation was not generated from the currently selected filing set."
            )

    if exact and not missing and cutoff_compatibility["compatible"]:
        status = "ready"
        gaps: list[str] = []
    elif exact and not missing:
        status = "incompatible"
        gaps = list(cutoff_compatibility["reasons"])
    elif exact:
        status = "partial"
        gaps = [f"Missing {key}" for key in missing]
        gaps.extend(cutoff_compatibility["reasons"])
    elif before:
        status = "stale"
        gaps = [f"The latest available valuation date is {before.parent.name}, earlier than as_of_date={request.as_of_date}."]
        gaps.extend(f"Missing {key}" for key in missing)
    elif dated and dated.future:
        status = "future_incompatible"
        gaps = [f"All valuation candidate directories are later than the knowledge cutoff {request.as_of_date} and cannot be used for historical as-of-date research."]
    elif latest and dated is None:
        # 未设置知识截止日时保持旧调用兼容：沿用原逻辑，把最新估值目录视为 ready。
        status = "ready"
        gaps = []
    else:
        status = "missing"
        gaps = ["Valuation report not found."]

    return {
        "status": status,
        "report_dir": path_state(report_dir),
        "artifacts": artifacts,
        "latest_available_date": selected.parent.name if selected else "",
        "latest_discovered_date": latest.parent.name if latest else "",
        "selected_candidate_date": selected.parent.name if selected else "",
        "requested_as_of_date": request.as_of_date,
        "candidate_date_audit": dated_candidate_audit(dated),
        "cutoff_compatibility": cutoff_compatibility,
        "gaps": gaps,
    }


def find_valuation_candidates(valuation_workspace: Path, stock_code: str) -> list[Path]:
    """查找某股票的估值报告候选文件。

    参数：
        valuation_workspace: 估值分析员工作区。
        stock_code: Stock code.
    返回值：
        valuation_report.json 候选路径列表。
    """
    candidates: list[Path] = []
    # 标准目录：valuation_workspace/reports/<stock_code>/<as_of_date>/valuation_report.json。
    candidates.extend((valuation_workspace / "reports" / stock_code).glob("*/valuation_report.json"))
    # 兼容早期目录：valuation_workspace/<stock_code>/<as_of_date>/valuation_report.json。
    candidates.extend((valuation_workspace / stock_code).glob("*/valuation_report.json"))
    # 旧数据目录结构可能不完全一致，因此再做一次保守兜底扫描，但限定股票代码目录避免全库扫描。
    candidates.extend(valuation_workspace.glob(f"**/{stock_code}/**/valuation_report.json"))
    return sorted(set(path for path in candidates if path.exists()))


def classify_dated_candidates(candidates: list[Path], cutoff_text: str) -> DatedCandidates:
    """按严格 ISO 目录日期把估值或市场候选分为 exact/before/future。

    参数：
        candidates: 报告 JSON 文件路径列表，父目录名应为观察日。
        cutoff_text: 已标准化的知识截止日。
    返回值：
        DatedCandidates 分类结果。
    """
    cutoff = parse_strict_iso_date(cutoff_text, "as_of_date")
    exact: list[Path] = []
    before: list[Path] = []
    future: list[Path] = []
    undated: list[Path] = []
    for candidate in candidates:
        try:
            candidate_date = parse_strict_iso_date(candidate.parent.name, "candidate_date")
        except ValueError:
            undated.append(candidate)
            continue
        if candidate_date == cutoff:
            exact.append(candidate)
        elif candidate_date < cutoff:
            before.append(candidate)
        else:
            future.append(candidate)
    return DatedCandidates(exact=exact, before=before, future=future, undated=undated)


def dated_candidate_audit(dated: DatedCandidates | None) -> dict[str, Any]:
    """把日期候选分类转换为紧凑、可序列化的审计统计。

    参数：
        dated: 日期候选分类；未设置截止日时为 None。
    返回值：
        各分类数量与日期列表。
    """
    if dated is None:
        return {"enabled": False, "exact": [], "before": [], "future": [], "undated": []}

    def dates(paths: list[Path]) -> list[str]:
        return sorted({path.parent.name for path in paths})

    return {
        "enabled": True,
        "exact": dates(dated.exact),
        "before": dates(dated.before),
        "future": dates(dated.future),
        "undated": dates(dated.undated),
        "counts": {
            "exact": len(dated.exact),
            "before": len(dated.before),
            "future": len(dated.future),
            "undated": len(dated.undated),
        },
    }


def choose_valuation_by_date(candidates: list[Path], as_of_date: str) -> Path | None:
    """选择指定估值日期的报告。

    参数：
        candidates: 估值报告候选路径。
        as_of_date: 估值观察日。
    返回值：
        匹配日期的估值报告路径；没有则返回 None。
    """
    matches = [path for path in candidates if path.parent.name == as_of_date]
    return choose_latest_valuation(matches)


def choose_latest_valuation(candidates: list[Path]) -> Path | None:
    """选择最新估值报告。

    参数：
        candidates: 估值报告候选路径。
    返回值：
        最新报告路径；没有则返回 None。
    """
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: (path.parent.name, path.stat().st_mtime), reverse=True)[0]


def build_valuation_artifacts(report_dir: Path | None) -> dict[str, dict[str, Any]]:
    """生成估值层产物路径状态。

    参数：
        report_dir: 估值报告目录。
    返回值：
        产物路径状态映射。
    """
    if not report_dir:
        return {key: path_state(None) for key in VALUATION_REQUIRED_KEYS}
    return {
        "valuation_report_json": path_state(report_dir / "valuation_report.json"),
        "valuation_report_md": path_state(report_dir / "valuation_report.md"),
        "valuation_evidence_table_json": path_state(report_dir / "valuation_evidence_table.json"),
        "valuation_audit_json": path_state(report_dir / "valuation_audit.json"),
    }


def market_context_package_ready(
    package: Any,
    sources: Any,
    collection_audit: Any,
) -> bool:
    """验证市场上下文包是否达到可复用的公开网页代理 Gate。

    参数：
        package: market_context_package.json 内容。
        sources: market_context_sources.json 内容。
        collection_audit: collection_audit.json 内容。
    返回值：
        结构、代理边界、质量 Gate 与审计状态全部一致时返回 True。
    """
    if not isinstance(package, dict) or not isinstance(sources, dict) or not isinstance(collection_audit, dict):
        return False
    quality_gate = package.get("quality_gate")
    usage_boundary = package.get("usage_boundary")
    source_rows = sources.get("sources")
    if package.get("status") != MARKET_CONTEXT_READY_STATUS:
        return False
    if not isinstance(quality_gate, dict) or quality_gate.get("can_support_market_expectation_proxy") is not True:
        return False
    if not isinstance(usage_boundary, dict) or usage_boundary.get("data_type") != "public_web_search_proxy":
        return False
    if quality_gate.get("max_confidence") not in {"low", "medium_low"}:
        return False
    if not isinstance(source_rows, list) or not source_rows:
        return False
    return collection_audit.get("status") == MARKET_CONTEXT_READY_STATUS


def check_market_cutoff_compatibility(
    package: Any, sources: Any, collection_audit: Any, cutoff_text: str
) -> dict[str, Any]:
    """校验严格同日市场上下文三件套的截止证明和模型可见来源边界。

    为什么同时核对两份来源表：``market_context_package.json`` 会直接进入模型上下文，
    ``market_context_sources.json`` 则是独立的来源登记。如果二者只校验其中一份，未来或
    无日期来源就可能通过另一份文件重新进入下游。因此严格同日复用必须保证两份表都只
    含 eligible 行、来源 ID 集合一致，并且所有 claim 都能回指到安全来源。

    参数：
        package: ``market_context_package.json`` 内容。
        sources: ``market_context_sources.json`` 内容。
        collection_audit: ``collection_audit.json`` 内容。
        cutoff_text: 请求中的严格 ISO 知识截止日。
    返回值：
        包含兼容性、英文缺口原因和三份截止审计元数据的字典。
    """
    if not cutoff_text:
        return {"compatible": True, "reasons": [], "audits": {}}
    documents = {
        "market_context_package": package,
        "market_context_sources": sources,
        "collection_audit": collection_audit,
    }
    reasons: list[str] = []
    audits: dict[str, Any] = {}
    for label, payload in documents.items():
        result = check_cutoff_audit(payload, cutoff_text, label, require_strict=True)
        audits[label] = result["audit"]
        reasons.extend(result["reasons"])
        if isinstance(result["audit"], dict):
            for count_key in ("future_fact_claim_count", "undated_fact_claim_count"):
                if count_key not in result["audit"]:
                    reasons.append(f"{label}.cutoff_audit is missing {count_key}, so claim compliance cannot be proven.")

    package_rows = package.get("source_table") if isinstance(package, dict) else None
    source_rows = sources.get("sources") if isinstance(sources, dict) else None
    package_source_ids, package_safe_count = validate_market_source_rows(
        package_rows, "market_context_package.source_table", reasons
    )
    registered_source_ids, registered_safe_count = validate_market_source_rows(
        source_rows, "market_context_sources.sources", reasons
    )

    if package_source_ids != registered_source_ids:
        package_only = sorted(str(source_id) for source_id in package_source_ids - registered_source_ids)
        sources_only = sorted(str(source_id) for source_id in registered_source_ids - package_source_ids)
        reasons.append(
            "The source-ID sets in market_context_package.source_table and market_context_sources.sources do not agree "
            f"(package_only={package_only}, sources_only={sources_only})."
        )

    # accepted_source_count 代表真正暴露给模型的安全来源数，而不是采集阶段发现的总数。
    # future_excluded_count、undated_discovery_count 等排除统计可以非零，但不能借此把被排除行
    # 重新放回任一模型可见来源表。
    for label, audit in audits.items():
        if not isinstance(audit, dict) or "accepted_source_count" not in audit:
            reasons.append(f"{label}.cutoff_audit is missing accepted_source_count.")
            continue
        accepted_source_count = audit.get("accepted_source_count")
        if accepted_source_count != package_safe_count or accepted_source_count != registered_safe_count:
            reasons.append(
                f"{label}.cutoff_audit.accepted_source_count={accepted_source_count!r} does not match the safe source-row counts "
                f"(package={package_safe_count}, sources={registered_safe_count})."
            )

    claims = package.get("claims", []) if isinstance(package, dict) else []
    if not isinstance(claims, list):
        reasons.append("market_context_package.claims must be a list for strict cutoff auditing.")
    else:
        safe_source_ids = package_source_ids & registered_source_ids
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                reasons.append(f"market_context_package.claims[{index}] is not an object.")
                continue
            source_id = claim.get("source_id")
            if source_id in (None, ""):
                reasons.append(f"market_context_package.claims[{index}] is missing source_id.")
            else:
                try:
                    source_is_safe = source_id in safe_source_ids
                except TypeError:
                    source_is_safe = False
                if not source_is_safe:
                    reasons.append(
                        f"market_context_package.claims[{index}] references source_id={source_id!r}, which is not an eligible source present in both source tables."
                    )
            claim_cutoff_status = claim.get("cutoff_status")
            if claim_cutoff_status not in (None, "eligible"):
                reasons.append(
                    f"market_context_package.claims[{index}] has cutoff_status={claim_cutoff_status!r}; strict exact-date claims must be eligible."
                )
    return {"compatible": not reasons, "reasons": reasons, "audits": audits}


def validate_market_source_rows(rows: Any, label: str, reasons: list[str]) -> tuple[set[Any], int]:
    """验证单份模型可见市场来源表只包含有 ID 的 eligible 行。

    为什么不根据 ``future_excluded_count`` 重建来源表：排除计数只是采集审计元数据，无法
    证明具体哪一行已从模型输入删除。兼容性判断必须直接检查最终文件中的每一行，避免
    “审计声称已排除、实际仍保留原文”的不一致包被复用。

    参数：
        rows: 来源行列表。
        label: 用于英文错误定位的字段路径。
        reasons: 共享缺口列表；本函数直接追加发现的问题。
    返回值：
        ``(source_id 集合, eligible 行数)``。集合仅供同次内存校验，不写入状态 JSON。
    """
    if not isinstance(rows, list):
        reasons.append(f"{label} must be a list for strict cutoff auditing.")
        return set(), 0

    source_ids: set[Any] = set()
    safe_row_count = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            reasons.append(f"{label}[{index}] is not an object.")
            continue
        cutoff_status = row.get("cutoff_status")
        if cutoff_status != "eligible":
            reasons.append(
                f"{label}[{index}] has cutoff_status={cutoff_status!r}; model-facing strict exact-date source tables may contain only eligible rows."
            )
            continue
        safe_row_count += 1
        source_id = row.get("source_id")
        if source_id in (None, ""):
            reasons.append(f"{label}[{index}] is eligible but missing source_id.")
            continue
        try:
            source_ids.add(source_id)
        except TypeError:
            reasons.append(f"{label}[{index}].source_id must be a scalar value.")
    return source_ids, safe_row_count


def audit_market_context_layer(root: Path, target: dict[str, str], request: ResearchAuditRequest) -> dict[str, Any]:
    """审计市场上下文层，并按 as_of_date 判断是否过期。

    参数：
        root: 项目根目录。
        target: 标准目标信息。
        request: 标准化请求。
    返回值：
        市场上下文层状态。
    """
    stock_code = target.get("stock_code", "")
    if not stock_code:
        return {
            "status": "blocked",
            "report_dir": path_state(None),
            "artifacts": {key: path_state(None) for key in MARKET_CONTEXT_REQUIRED_KEYS},
            "latest_available_date": "",
            "requested_as_of_date": request.as_of_date,
            "quality_gate": {},
            "gaps": ["Stock code is missing; the market-context package cannot be located."],
        }

    workspace = root / "market_context_collector_scripts" / "collector_workspace"
    candidates = find_market_context_candidates(workspace, stock_code)
    dated = classify_dated_candidates(candidates, request.as_of_date) if request.as_of_date else None
    exact = choose_latest_market_context(dated.exact) if dated else None
    before = choose_latest_market_context(dated.before) if dated else None
    latest = choose_latest_market_context(candidates)
    selected = (exact or before) if dated else latest
    report_dir = selected.parent if selected else None
    artifacts = build_market_context_artifacts(report_dir)
    missing = [key for key in MARKET_CONTEXT_REQUIRED_KEYS if not artifacts[key]["exists"]]
    package = load_json(Path(artifacts["market_context_package_json"]["path"])) if artifacts["market_context_package_json"]["path"] else {}
    sources = load_json(Path(artifacts["market_context_sources_json"]["path"])) if artifacts["market_context_sources_json"]["path"] else {}
    collection_audit = load_json(Path(artifacts["collection_audit_json"]["path"])) if artifacts["collection_audit_json"]["path"] else {}
    package_status = package.get("status", "") if isinstance(package, dict) else ""
    quality_gate = package.get("quality_gate", {}) if isinstance(package, dict) else {}
    package_ready = not missing and market_context_package_ready(package, sources, collection_audit)
    cutoff_compatibility = check_market_cutoff_compatibility(
        package, sources, collection_audit, request.as_of_date
    ) if request.as_of_date and exact else {"compatible": True, "reasons": [], "audits": {}}

    if exact and package_ready and cutoff_compatibility["compatible"]:
        status = "ready"
        gaps: list[str] = []
    elif exact and package_ready:
        status = "incompatible"
        gaps = list(cutoff_compatibility["reasons"])
    elif exact:
        status = "partial"
        gaps = [f"Missing {key}" for key in missing]
        gaps.extend(cutoff_compatibility["reasons"])
        if not package_ready:
            gaps.append(
                f"The market-context package status/gate does not satisfy ready_public_proxy (current: {package_status or 'invalid'}); "
                "it cannot be reused as a market-narrative proxy."
            )
    elif before:
        status = "stale"
        gaps = [f"The latest available market-context date is {before.parent.name}, earlier than as_of_date={request.as_of_date}."]
        gaps.extend(f"Missing {key}" for key in missing)
        if not package_ready:
            gaps.append(
                f"The historical market-context package status/gate does not satisfy ready_public_proxy (current: {package_status or 'invalid'})."
            )
    elif dated and dated.future:
        status = "future_incompatible"
        gaps = [f"All market-context candidate directories are later than the knowledge cutoff {request.as_of_date} and cannot be used for historical as-of-date research."]
    elif latest and dated is None:
        # 未设置知识截止日时保持既有 Gate：最新包只有完整且通过公开网页代理 Gate 才可 ready。
        if package_ready:
            status = "ready"
            gaps = []
        else:
            status = "partial"
            gaps = [f"Missing {key}" for key in missing]
            gaps.append(
                f"The market-context package status/gate does not satisfy ready_public_proxy (current: {package_status or 'invalid'}); "
                "it cannot be reused as a market-narrative proxy."
            )
    else:
        status = "missing"
        gaps = ["Market-context package not found."]

    return {
        "status": status,
        "report_dir": path_state(report_dir),
        "artifacts": artifacts,
        "latest_available_date": selected.parent.name if selected else "",
        "latest_discovered_date": latest.parent.name if latest else "",
        "selected_candidate_date": selected.parent.name if selected else "",
        "requested_as_of_date": request.as_of_date,
        "candidate_date_audit": dated_candidate_audit(dated),
        "package_status": package_status,
        "quality_gate": quality_gate,
        "cutoff_compatibility": cutoff_compatibility,
        "gaps": gaps,
    }


def find_market_context_candidates(workspace: Path, stock_code: str) -> list[Path]:
    """查找某股票的市场上下文包候选文件。

    参数：
        workspace: 市场上下文采集工作区。
        stock_code: Stock code.
    返回值：
        market_context_package.json 候选路径列表。
    """
    candidates: list[Path] = []
    candidates.extend((workspace / "packages" / stock_code).glob("*/market_context_package.json"))
    candidates.extend(workspace.glob(f"**/{stock_code}/**/market_context_package.json"))
    return sorted(set(path for path in candidates if path.exists()))


def choose_market_context_by_date(candidates: list[Path], as_of_date: str) -> Path | None:
    """选择指定观察日的市场上下文包。

    参数：
        candidates: 市场上下文包候选路径。
        as_of_date: 观察日。
    返回值：
        匹配观察日的包路径；没有则返回 None。
    """
    matches = [path for path in candidates if path.parent.name == as_of_date]
    return choose_latest_market_context(matches)


def choose_latest_market_context(candidates: list[Path]) -> Path | None:
    """选择最新市场上下文包。

    参数：
        candidates: 市场上下文包候选路径。
    返回值：
        最新包路径；没有则返回 None。
    """
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: (path.parent.name, path.stat().st_mtime), reverse=True)[0]


def build_market_context_artifacts(report_dir: Path | None) -> dict[str, dict[str, Any]]:
    """生成市场上下文层产物路径状态。

    参数：
        report_dir: 市场上下文包目录。
    返回值：
        产物路径状态映射。
    """
    if not report_dir:
        return {key: path_state(None) for key in MARKET_CONTEXT_REQUIRED_KEYS}
    return {
        "market_context_package_json": path_state(report_dir / "market_context_package.json"),
        "market_context_package_md": path_state(report_dir / "market_context_package.md"),
        "market_context_sources_json": path_state(report_dir / "market_context_sources.json"),
        "collection_audit_json": path_state(report_dir / "collection_audit.json"),
    }


def build_reusable_flags(layers: dict[str, dict[str, Any]]) -> dict[str, bool]:
    """根据层状态生成可复用布尔标记。

    参数：
        layers: 所有产物层状态。
    返回值：
        层名到可复用布尔值的映射。
    """
    return {
        "collector": layers["collector"]["status"] == "ready",
        "processor": layers["processor"]["status"] == "ready",
        "financial_evidence_draft": layers["financial_evidence_draft"]["status"] == "ready",
        "formal_financial_analysis": layers["formal_financial_analysis"]["status"] == "ready",
        "valuation": layers["valuation"]["status"] == "ready",
        "market_context": layers["market_context"]["status"] == "ready",
    }


def build_next_actions(layers: dict[str, dict[str, Any]], request: ResearchAuditRequest) -> list[dict[str, str]]:
    """根据状态层生成下一步动作列表。

    参数：
        layers: 所有产物层状态。
        request: 标准化请求。
    返回值：
        下一步动作列表。
    """
    actions: list[dict[str, str]] = []
    collector_status = layers["collector"]["status"]
    if collector_status == "ambiguous":
        actions.append(action("resolve_target", "main", "The target is ambiguous. Specify the stock code or report year before continuing."))
        return actions
    if collector_status == "future_incompatible":
        actions.append(
            action(
                "resolve_knowledge_cutoff",
                "main",
                "The selected report was published after the knowledge cutoff. Use an earlier fiscal year or adjust as_of_date; future artifacts cannot be generated for a historical cutoff.",
            )
        )
        return actions
    if request.force_refresh:
        return [
            action("collector_refresh", "information-collector", "force_refresh=true was explicitly requested; recheck or redownload the financial report."),
            action("processor_refresh", "information-processor", "force_refresh=true was explicitly requested; rebuild parsing, digest, RAG, and summary comparison."),
            action("financial_analysis_refresh", "financial-analyst", "force_refresh=true was explicitly requested; rebuild the financial analysis."),
            action("valuation_update", "valuation-analyst", "force_refresh=true was explicitly requested; rerun valuation."),
            action("market_context_refresh", "market-context-collector", "force_refresh=true was explicitly requested; recollect public-web market context."),
        ]
    if collector_status in {"missing", "partial"}:
        actions.append(action("collector_fetch", "information-collector", "The collection layer is missing an official annual-report PDF or manifest record."))
        return actions

    market_context_actions = build_market_context_next_actions(layers["market_context"])

    processor_status = layers["processor"]["status"]
    if processor_status in {"missing", "partial"}:
        actions.extend(build_processor_next_actions(layers["processor"]))
        actions.extend(market_context_actions)
        return actions

    draft_status = layers["financial_evidence_draft"]["status"]
    if draft_status in {"missing", "partial", "incompatible"}:
        reason = "The multi-period financial evidence handoff is missing, incomplete, or does not match the selected filing set."
        actions.append(action("financial_evidence_draft", "financial-analyst", reason))
        actions.extend(market_context_actions)
        return actions

    formal_status = layers["formal_financial_analysis"]["status"]
    if formal_status in {"missing", "partial", "incompatible"}:
        reason = "Formal financial analysis is missing or incomplete."
        if formal_status == "incompatible":
            reason = "The existing formal financial analysis is incompatible with the requested depth/focus. Reuse it as a base and supplement the analysis."
        actions.append(action("financial_analysis_update", "financial-analyst", reason))
        actions.extend(market_context_actions)
        return actions

    valuation_status = layers["valuation"]["status"]
    if valuation_status in {"missing", "partial", "stale", "incompatible", "blocked", "future_incompatible"}:
        reason_map = {
            "missing": "The valuation report is missing; generate valuation from the reused financial analysis.",
            "partial": "Valuation artifacts are incomplete; complete the valuation report, evidence table, or audit file.",
            "stale": "The valuation date is earlier than this as_of_date; update valuation and market data only.",
            "incompatible": "The same-day valuation lacks valid historical cutoff proof and must be rerun using the knowledge cutoff.",
            "blocked": "The valuation layer cannot locate the stock code; correct the target information first.",
            "future_incompatible": "All existing valuations are later than the knowledge cutoff and must be rerun for the historical as-of date.",
        }
        actions.append(action("valuation_update", "valuation-analyst", reason_map[valuation_status]))
    actions.extend(market_context_actions)
    return actions


def build_market_context_next_actions(market_context_layer: dict[str, Any]) -> list[dict[str, str]]:
    """为市场上下文层生成可与财务/估值并行的补齐动作。

    参数：
        market_context_layer: 市场上下文层状态。
    返回值：
        市场上下文下一步动作列表。
    """
    market_context_status = market_context_layer["status"]
    if market_context_status not in {"missing", "partial", "stale", "incompatible", "blocked", "future_incompatible"}:
        return []
    reason_map = {
        "missing": "The market-context package is missing; use Bocha Web Search to collect public market narratives and contrary signals.",
        "partial": "Market-context artifacts are incomplete or contain only a query plan; complete web results, source table, and quality gate.",
        "stale": "The market-context date is earlier than this as_of_date; refresh hotspots, company narratives, and contrary signals.",
        "incompatible": "The same-day market context lacks valid historical cutoff proof and must be recollected for as_of_date.",
        "blocked": "The market-context layer cannot locate the stock code; correct the target information first.",
        "future_incompatible": "All existing market-context packages are later than the knowledge cutoff and must be recollected for the historical as-of date.",
    }
    return [action("market_context_update", "market-context-collector", reason_map[market_context_status])]


def build_processor_next_actions(processor_layer: dict[str, Any]) -> list[dict[str, str]]:
    """为信息处理层生成精确到子产物的补齐动作。

    参数：
        processor_layer: 信息处理层状态。
    返回值：
        信息处理层下一步动作列表。
    """
    artifacts = processor_layer.get("artifacts", {})
    quality_flags = processor_layer.get("quality_flags", {})
    actions: list[dict[str, str]] = []
    if not artifacts.get("content_json", {}).get("exists"):
        actions.append(action("processor_parse_pdf", "information-processor", "content.json is missing; parse the PDF first."))
        return actions
    if not artifacts.get("llm_digest_json", {}).get("exists") or not artifacts.get("digest_audit_json", {}).get("exists"):
        actions.append(action("processor_digest", "information-processor", "llm_digest or digest_audit is missing; rebuild the digest."))
    elif quality_flags.get("missing_digest_chunks") or quality_flags.get("invalid_digest_results"):
        actions.append(action("processor_digest", "information-processor", "digest_audit reports missing or invalid chunks; repair the digest."))
    if not artifacts.get("rag_chunks_jsonl", {}).get("exists"):
        actions.append(action("processor_rag", "information-processor", "rag_index/rag_chunks.jsonl is missing; build only the RAG index."))
    if not artifacts.get("summary_comparison_json", {}).get("exists"):
        actions.append(action("processor_summary_compare", "information-processor", "summary_comparison.json is missing; run only the summary comparison."))
    return actions or [action("processor_inspect", "information-processor", "The processing layer is partial, but no standard missing item was identified; manual inspection is required.")]


def build_skipped_actions(
    layers: dict[str, dict[str, Any]], request: ResearchAuditRequest, next_actions: list[dict[str, str]]
) -> list[str]:
    """生成已经跳过的角色动作。

    参数：
        layers: 所有产物层状态。
        request: 标准化请求。
        next_actions: 下一步动作列表。
    返回值：
        跳过的角色名称列表。
    """
    if request.force_refresh:
        return []
    next_owners = {item["owner"] for item in next_actions}
    skipped: list[str] = []
    if layers["collector"]["status"] == "ready" and "information-collector" not in next_owners:
        skipped.append("information-collector")
    if layers["processor"]["status"] == "ready" and "information-processor" not in next_owners:
        skipped.append("information-processor")
    if (
        layers["financial_evidence_draft"]["status"] == "ready"
        and layers["formal_financial_analysis"]["status"] == "ready"
        and "financial-analyst" not in next_owners
    ):
        skipped.append("financial-analyst")
    if layers["valuation"]["status"] == "ready" and "valuation-analyst" not in next_owners:
        skipped.append("valuation-analyst")
    if layers["market_context"]["status"] == "ready" and "market-context-collector" not in next_owners:
        skipped.append("market-context-collector")
    return skipped


def build_summary(
    layers: dict[str, dict[str, Any]], reusable: dict[str, bool], skipped_actions: list[str], next_actions: list[dict[str, str]]
) -> dict[str, Any]:
    """构造给主会话快速阅读的摘要。

    参数：
        layers: 所有产物层状态。
        reusable: 可复用层标记。
        skipped_actions: 跳过动作。
        next_actions: 下一步动作。
    返回值：
        摘要字典。
    """
    return {
        "layer_statuses": {name: layer["status"] for name, layer in layers.items()},
        "reusable_layers": [name for name, enabled in reusable.items() if enabled],
        "skipped_actions": skipped_actions,
        "next_action_steps": [item["step"] for item in next_actions],
        "has_blocker": any(
            layer["status"] in {"ambiguous", "blocked", "future_incompatible"} for layer in layers.values()
        ),
    }


def action(step: str, owner: str, reason: str) -> dict[str, str]:
    """创建标准下一步动作对象。

    参数：
        step: 机器可读动作名。
        owner: 建议承担该动作的主会话或 custom agent。
        reason: 触发原因。
    返回值：
        动作字典。
    """
    return {"step": step, "owner": owner, "reason": reason}


CUTOFF_COMPLIANT_STATUSES = {"ok", "passed", "ready", "compliant", "completed"}


def check_cutoff_audit(
    payload: Any,
    cutoff_text: str,
    label: str,
    *,
    require_strict: bool = False,
) -> dict[str, Any]:
    """校验单个产物中的历史截止证明。

    参数：
        payload: 产物 JSON 内容。
        cutoff_text: 请求知识截止日。
        label: 供错误信息定位的产物名称。
        require_strict: 是否要求 ``strict_cutoff=true``。
    返回值：
        包含 compatible、audit 和 reasons 的字典。
    """
    if not cutoff_text:
        return {"compatible": True, "audit": {}, "reasons": []}
    reasons: list[str] = []
    audit = payload.get("cutoff_audit") if isinstance(payload, dict) else None
    # 兼容两种等价包装：分析主报告通常把证明放在 cutoff_audit 下；独立
    # valuation_audit.json 可以直接以审计字段作为根对象。只在根对象确实声明截止日期时
    # 接受后一种形式，避免把普通 {status: completed} 误当成完整历史证明。
    if not isinstance(audit, dict) and isinstance(payload, dict) and (
        payload.get("cutoff_date") or payload.get("as_of_date") or payload.get("knowledge_cutoff")
    ):
        audit = payload
    if not isinstance(audit, dict):
        return {
            "compatible": False,
            "audit": {},
            "reasons": [f"{label} is missing cutoff_audit, so exclusion of post-cutoff information cannot be proven."],
        }

    audit_date = str(
        audit.get("cutoff_date") or audit.get("as_of_date") or audit.get("knowledge_cutoff") or ""
    ).strip()
    if audit_date != cutoff_text:
        reasons.append(f"{label} cutoff_audit date is {audit_date or 'missing'}, inconsistent with as_of_date={cutoff_text}.")
    status = str(audit.get("status") or "").strip().lower()
    compliant = audit.get("cutoff_compliant") if "cutoff_compliant" in audit else audit.get("compliant")
    if status and status not in CUTOFF_COMPLIANT_STATUSES:
        reasons.append(f"{label} cutoff_audit.status={status} did not pass.")
    if compliant is False:
        reasons.append(f"{label} cutoff_audit explicitly marks the artifact as non-compliant.")
    elif compliant is not True and status not in CUTOFF_COMPLIANT_STATUSES:
        reasons.append(f"{label} cutoff_audit does not explicitly mark the artifact compliant.")
    if require_strict and audit.get("strict_cutoff") is not True:
        reasons.append(f"{label} cutoff_audit.strict_cutoff is not true.")
    if audit.get("future_fact_claim_count") not in (None, 0, "0"):
        reasons.append(f"{label} still uses future sources in factual claims.")
    if audit.get("undated_fact_claim_count") not in (None, 0, "0"):
        reasons.append(f"{label} undated_fact_claim_count must be 0.")
    return {"compatible": not reasons, "audit": audit, "reasons": reasons}


def extract_declared_as_of_date(payload: Any) -> str:
    """从常见元数据位置提取产物声明的 as_of_date。"""
    if not isinstance(payload, dict):
        return ""
    candidates = [payload.get("as_of_date")]
    for key in ("target", "analysis_metadata", "research_metadata", "metadata"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.extend([nested.get("as_of_date"), nested.get("knowledge_cutoff")])
    return next((str(value).strip() for value in candidates if str(value or "").strip()), "")


def collect_source_report_dates(payload: Any) -> tuple[list[date], list[str]]:
    """递归收集来源财报的 published_at，并区分无法解析的值。

    只有键路径包含 source/report/financial_statement 时才视为来源财报日期，避免把分析
    产物自身的发布时间误当成上游财报披露日。
    """
    parsed: list[date] = []
    invalid: list[str] = []

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered_path = (*path, str(key).lower())
                key_text = str(key).lower()
                if (key_text == "published_at" or key_text.endswith("_published_at")) and any(
                    token in "/".join(lowered_path) for token in ("source", "report", "financial_statement")
                ):
                    record_date = parse_record_published_date({"published_at": child})
                    if record_date is None:
                        invalid.append(str(child or ""))
                    else:
                        parsed.append(record_date)
                else:
                    visit(child, lowered_path)
        elif isinstance(value, list):
            for item in value:
                visit(item, path)

    visit(payload, ())
    return parsed, invalid


def check_formal_cutoff_compatibility(payload: Any, request: ResearchAuditRequest) -> dict[str, Any]:
    """校验正式财务分析的观察日、截止审计和来源财报披露日。"""
    if not request.as_of_date:
        return {"compatible": True, "reasons": [], "cutoff_audit": {}, "source_report_dates": []}
    result = check_cutoff_audit(payload, request.as_of_date, "formal_financial_analysis")
    reasons = list(result["reasons"])
    declared_date = extract_declared_as_of_date(payload)
    if declared_date != request.as_of_date:
        reasons.append(
            f"Formal financial analysis declares as_of_date={declared_date or 'missing'}, inconsistent with requested {request.as_of_date}."
        )
    source_dates, invalid_dates = collect_source_report_dates(payload)
    cutoff = parse_strict_iso_date(request.as_of_date, "as_of_date")
    if not source_dates:
        reasons.append("Formal financial analysis does not provide a verifiable source-report published_at.")
    if invalid_dates:
        reasons.append("Formal financial analysis contains an unparseable source-report published_at.")
    future_dates = sorted({item.isoformat() for item in source_dates if item > cutoff})
    if future_dates:
        reasons.append("Source-report publication dates later than the knowledge cutoff: " + ", ".join(future_dates))
    return {
        "compatible": not reasons,
        "reasons": reasons,
        "cutoff_audit": result["audit"],
        "declared_as_of_date": declared_date,
        "source_report_dates": sorted({item.isoformat() for item in source_dates}),
    }


def check_analysis_compatibility(metadata: dict[str, Any], request: ResearchAuditRequest) -> dict[str, Any]:
    """判断已有正式财务分析是否兼容本次 depth/focus。

    参数：
        metadata: 已有分析文件里的元数据。
        request: 标准化请求。
    返回值：
        兼容性结果。
    """
    existing_depth = normalize_depth(metadata.get("analysis_depth") or metadata.get("depth") or "standard")
    requested_depth = normalize_depth(request.depth)
    existing_focus = parse_focus(metadata.get("focus", ""))
    requested_focus = parse_focus(request.focus)
    reasons: list[str] = []
    if DEPTH_RANK[existing_depth] < DEPTH_RANK[requested_depth]:
        reasons.append(f"Existing financial-analysis depth is {existing_depth}, below requested {requested_depth}.")
    if requested_focus and not requested_focus.issubset(existing_focus):
        missing_focus = sorted(requested_focus - existing_focus)
        reasons.append("Existing financial analysis does not cover the requested focus: " + ", ".join(missing_focus))
    return {
        "compatible": not reasons,
        "existing_depth": existing_depth,
        "requested_depth": requested_depth,
        "existing_focus": sorted(existing_focus),
        "requested_focus": sorted(requested_focus),
        "reasons": reasons,
    }


def extract_analysis_metadata(path_value: str) -> dict[str, Any]:
    """从分析产物中提取 depth/focus 等复用元数据。

    参数：
        path_value: JSON 文件路径字符串。
    返回值：
        元数据字典；文件不存在或解析失败时返回空字典。
    """
    if not path_value:
        return {}
    payload = load_json(Path(path_value))
    if not isinstance(payload, dict):
        return {}
    for key in ["analysis_metadata", "research_metadata", "metadata"]:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def normalize_depth(value: Any) -> str:
    """标准化分析深度标签。

    参数：
        value: 任意深度表示。
    返回值：
        quick、standard 或 deep。
    """
    text = str(value or "standard").strip().lower()
    return text if text in DEPTH_RANK else "standard"


def parse_focus(value: Any) -> set[str]:
    """把 focus 字符串或列表解析成小写集合。

    参数：
        value: 字符串、列表或其他值。
    返回值：
        focus 标签集合。
    """
    if isinstance(value, list):
        raw_items = value
    else:
        text = str(value or "")
        raw_items = text.replace("，", ",").replace(";", ",").replace("；", ",").split(",")
    return {str(item).strip().lower() for item in raw_items if str(item).strip()}


def choose_best_dir(candidates: list[Path] | set[Path], required_keys: list[str]) -> Path | None:
    """从候选目录中选择最完整、最新的目录。

    参数：
        candidates: 候选目录集合。
        required_keys: 用于评分的产物键。
    返回值：
        最佳目录；没有有效目录时返回 None。
    """
    existing_dirs = [path for path in set(candidates) if path and path.exists() and path.is_dir()]
    if not existing_dirs:
        return None

    def score(path: Path) -> tuple[int, int, float, str]:
        existing_count = sum(1 for key in required_keys if (path / key_to_filename(key)).exists())
        known_path_score = 1 if "unknown_year" not in str(path).replace("\\", "/") else 0
        newest_mtime = max((child.stat().st_mtime for child in path.glob("*") if child.is_file()), default=path.stat().st_mtime)
        return existing_count, known_path_score, newest_mtime, str(path)

    return sorted(existing_dirs, key=score, reverse=True)[0]


def key_to_filename(key: str) -> str:
    """把产物键转换成标准文件名。

    参数：
        key: 产物键。
    返回值：
        文件名。
    """
    mapping = {
        "analyst_report_json": "analyst_report.json",
        "analyst_report_md": "analyst_report.md",
        "evidence_check_json": "evidence_check.json",
        "analyst_audit_json": "analyst_audit.json",
        "formal_financial_analysis_json": "formal_financial_analysis.json",
        "formal_financial_analysis_md": "formal_financial_analysis.md",
    }
    return mapping.get(key, key)


def path_state(path: Path | str | None) -> dict[str, Any]:
    """生成统一路径状态对象。

    参数：
        path: 路径或 None。
    返回值：
        包含 path 和 exists 的字典。
    """
    if not path:
        return {"path": "", "exists": False}
    resolved = Path(path)
    return {"path": str(resolved), "exists": resolved.exists()}


def trim_record(record: dict[str, Any] | None) -> dict[str, Any]:
    """裁剪 manifest 记录，只保留调度需要的字段。

    参数：
        record: manifest 记录。
    返回值：
        裁剪后的记录。
    """
    if not record:
        return {}
    keys = [
        "stock_code",
        "company_name",
        "report_year",
        "report_type",
        "title",
        "published_at",
        "announcement_id",
        "local_relative_path",
        "title_classification",
        "record_kind",
    ]
    return {key: record.get(key, "") for key in keys}


def load_json(path: Path) -> Any:
    """安全读取 JSON 文件。

    参数：
        path: JSON 文件路径。
    返回值：
        JSON 内容；不存在或解析失败时返回空字典。
    """
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_json_list(path: Path) -> list[dict[str, Any]]:
    """安全读取 JSON 列表文件。

    参数：
        path: JSON 文件路径。
    返回值：
        字典列表；不存在或格式不符时返回空列表。
    """
    payload = load_json(path)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def write_json(path: Path, payload: Any) -> None:
    """原子写入 JSON 文件，并自动创建父目录。

    为什么使用同目录临时文件再替换：状态观察器、主协调会话和人工 audit 可能在
    相邻时刻读取同一文件；直接 ``write_text`` 会先截断目标文件，读者可能看到半份
    JSON。原子替换保证任一时刻只能看到旧版本或完整新版本。

    参数：
        path: 输出路径。
        payload: 可序列化对象。
    返回值：
        无。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        # replace 成功后临时文件已不存在；失败时尽力清理，但绝不删除旧目标文件。
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_state_path_component(value: Any, fallback: str) -> str:
    """把外部目标值规整为单个安全目录名。

    为什么不能直接交给 ``Path / value``：绝对路径会丢弃此前的根目录，``..`` 和
    路径分隔符也能越出 company_state。这里保留中文、字母、数字及少量安全符号，
    其他字符统一替换为下划线。

    参数：
        value: 原始股票代码、目标名称或财年。
        fallback: 规整后为空时的兜底目录名。
    返回值：
        不含路径分隔符且不是 ``.``/``..`` 的安全组件。
    """
    text = str(value or "").strip()
    safe = re.sub(r"[^0-9A-Za-z一-鿿._-]+", "_", text)
    safe = safe.strip(" ._")
    if not safe or safe in {".", ".."}:
        return fallback
    return safe[:120]


def default_state_output_path(root: Path, state: dict[str, Any]) -> Path:
    """生成受 company_state 根约束的默认 research_state 输出路径。

    参数：
        root: 项目根目录。
        state: 审计状态。
    返回值：
        默认输出路径。
    """
    target = state.get("target", {})
    request = state.get("request", {})
    stock_code = _safe_state_path_component(
        target.get("stock_code") or request.get("target"), "unknown_target"
    )
    filing_policy = str(state.get("filing_policy") or request.get("filing_policy") or "single_filing")
    if filing_policy == "recent_history":
        state_scope = _safe_state_path_component(
            state.get("knowledge_cutoff") or request.get("as_of_date"), "current"
        )
    else:
        state_scope = _safe_state_path_component(
            target.get("report_year") or request.get("report_year"), "unknown_year"
        )
    base = (
        root
        / "research_orchestrator_scripts"
        / "orchestrator_workspace"
        / "company_state"
    ).resolve()
    output = (base / stock_code / state_scope / "research_state.json").resolve()
    if not output.is_relative_to(base):
        # 双重防线：即使未来放宽字符集，也不能突破正式状态工作区。
        raise ValueError(f"research_state output path is outside the allowed root: {output}")
    return output


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    参数：
        无。
    返回值：
        ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="Audit reusable company-research artifacts and output a continuation plan.")
    parser.add_argument("--target", default="", help="Company name or stock code; a six-digit number is treated as a stock code.")
    parser.add_argument("--stock-code", default="", help="Stock code.")
    parser.add_argument("--company-name", default="", help="Company name.")
    parser.add_argument("--report-year", "--fiscal-year", dest="report_year", default="", help="Fiscal year of an explicitly pinned filing, e.g. 2025.")
    parser.add_argument("--report-type", default="", help="Report type for an explicitly pinned filing. Omit it to use recent-history mode.")
    parser.add_argument("--filing-policy", choices=["recent_history", "single_filing"], default="", help="Filing selection policy. The default is recent_history unless both report type and year are pinned.")
    parser.add_argument("--annual-lookback", type=int, default=2, help="Number of eligible annual reports to retain in recent-history mode; default: 2.")
    parser.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard", help="Research depth.")
    parser.add_argument("--focus", default="", help="Research focus; separate multiple topics with commas.")
    parser.add_argument("--as-of-date", default="", help="Valuation observation date, e.g. 2026-07-08.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root; detected automatically by default.")
    parser.add_argument("--force-refresh", action="store_true", help="Force refresh all layers; disabled by default.")
    parser.add_argument("--write-state", action="store_true", help="Write research_state.json.")
    parser.add_argument("--output", default="", help="Explicit research_state.json output path.")
    return parser


def main() -> None:
    """命令行主入口。

    参数：
        无。
    返回值：
        无。
    """
    configure_stdout_encoding()
    args = build_parser().parse_args()
    request = ResearchAuditRequest(
        target=args.target,
        stock_code=args.stock_code,
        company_name=args.company_name,
        report_year=args.report_year,
        report_type=args.report_type,
        filing_policy=args.filing_policy,
        annual_lookback=args.annual_lookback,
        depth=args.depth,
        focus=args.focus,
        as_of_date=args.as_of_date,
        force_refresh=args.force_refresh,
        write_state=args.write_state,
        output=args.output,
    )
    root = Path(args.project_root).resolve()
    state = audit_company_research_state(root, request)
    output_path = Path(args.output).resolve() if args.output else None
    if args.write_state or output_path:
        write_json(output_path or default_state_output_path(root, state), state)
    print(json.dumps(state, ensure_ascii=False, indent=2))


def configure_stdout_encoding() -> None:
    """配置 Windows 终端 UTF-8 输出，避免中文乱码。

    参数：
        无。
    返回值：
        无。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    main()
