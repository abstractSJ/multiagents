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
        target: 用户传入的公司名或股票代码；如果是 6 位数字，会被视作股票代码。
        stock_code: 明确的股票代码。
        company_name: 明确的公司简称或全称。
        report_year: 财报所属年度，例如 2025；为空时从已有记录中选择最新年度。
        report_type: 财报类型，默认 annual。
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
    report_type: str = "annual"
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

    参数：
        project_root: 项目根目录。
        request: 审计请求。
    返回值：
        可直接 JSON 序列化的研究状态字典。
    """
    root = Path(project_root).resolve()
    normalized_request = normalize_request(request)

    collector_layer, selection = audit_collector_layer(root, normalized_request)
    target = selection.target
    processor_layer = audit_processor_layer(root, target, selection.main_record)
    financial_dirs = find_financial_artifact_dirs(root, target, normalized_request)
    financial_draft_layer = audit_financial_draft_layer(financial_dirs.evidence_dir)
    formal_financial_layer = audit_formal_financial_layer(financial_dirs.formal_dir, normalized_request)

    if collector_layer["status"] == "future_incompatible":
        # 历史基准日下，未来披露财报即使已经下载、解析或分析，也不能倒灌进当时可知状态。
        processor_layer = block_layer_for_future_cutoff(processor_layer, "信息处理层")
        financial_draft_layer = block_layer_for_future_cutoff(financial_draft_layer, "财务证据草稿层")
        formal_financial_layer = block_layer_for_future_cutoff(formal_financial_layer, "正式财务分析层")

    valuation_layer = audit_valuation_layer(root, target, normalized_request)
    market_context_layer = audit_market_context_layer(root, target, normalized_request)

    layers = {
        "collector": collector_layer,
        "processor": processor_layer,
        "financial_evidence_draft": financial_draft_layer,
        "formal_financial_analysis": formal_financial_layer,
        "valuation": valuation_layer,
        "market_context": market_context_layer,
    }
    reusable = build_reusable_flags(layers)
    next_actions = build_next_actions(layers, normalized_request)
    skipped_actions = build_skipped_actions(layers, normalized_request, next_actions)

    state = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_cutoff": normalized_request.as_of_date,
        "request": request_to_dict(normalized_request),
        "target": target,
        "layers": layers,
        "reusable": reusable,
        "skipped_actions": skipped_actions,
        "next_actions": next_actions,
        "summary": build_summary(layers, reusable, skipped_actions, next_actions),
    }
    return state


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
        raise ValueError(f"{field_name} 必须是严格 ISO 日期 YYYY-MM-DD，当前值：{text or '<empty>'}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} 不是有效日期：{text}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field_name} 必须是严格 ISO 日期 YYYY-MM-DD，当前值：{text}")
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
    return ResearchAuditRequest(
        target=target,
        stock_code=stock_code,
        company_name=company_name,
        report_year=str(request.report_year or "").strip(),
        report_type=str(request.report_type or "annual").strip() or "annual",
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
        gaps.append("目标命中多个股票代码或报告年度，需要先明确 stock_code/report_year。")
    elif not manifest_path.exists():
        status = "missing"
        gaps.append("财报 manifest 不存在，需要先运行 information-collector。")
    elif not selection.main_record and future_main_records and not undated_main_records:
        status = "future_incompatible"
        gaps.append(
            f"命中的正式财报均在知识截止日 {request.as_of_date} 之后披露，不能用于历史基准日研究。"
        )
    elif not selection.main_record:
        status = "missing"
        gaps.append("manifest 中没有命中知识截止日前已披露的正式财报记录。")
        if undated_main_records:
            gaps.append("存在缺少或无法解析 published_at 的正式财报记录，历史审计中不能选用。")
    elif not main_pdf_path or not main_pdf_path.exists():
        status = "partial"
        gaps.append("正式财报 PDF 记录存在但本地文件缺失，需要补下载。")

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

    def score(record: dict[str, Any]) -> tuple[int, str]:
        # 优先选择本地文件已存在、分类明确为 annual_full、披露日期较新的记录。
        pdf_path = resolve_record_pdf_path(collector_workspace, record)
        exists_score = 10 if pdf_path and pdf_path.exists() else 0
        class_score = 5 if str(record.get("title_classification", "")) in {"annual_full", "report_full"} else 0
        date_score = str(record.get("published_at", ""))
        return exists_score + class_score, date_score

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


def audit_processor_layer(root: Path, target: dict[str, str], main_record: dict[str, Any] | None) -> dict[str, Any]:
    """审计信息处理层，检查解析、digest、RAG 和摘要比对产物。

    参数：
        root: 项目根目录。
        target: 标准目标信息。
        main_record: 正式财报记录。
    返回值：
        信息处理层状态。
    """
    report_dir = find_processor_report_dir(root, target, main_record)
    artifacts = build_processor_artifacts(report_dir)
    missing = [key for key in PROCESSOR_REQUIRED_KEYS if not artifacts[key]["exists"]]
    gaps = [f"缺少 {key}" for key in missing]
    digest_audit = load_json(Path(artifacts["digest_audit_json"]["path"])) if artifacts["digest_audit_json"]["path"] else {}
    missing_chunks = digest_audit.get("missing_chunks", []) if isinstance(digest_audit, dict) else []
    invalid_results = digest_audit.get("invalid_results", []) if isinstance(digest_audit, dict) else []
    if missing_chunks:
        gaps.append(f"digest_audit 标记缺失 chunk：{len(missing_chunks)} 个")
    if invalid_results:
        gaps.append(f"digest_audit 标记无效 chunk：{len(invalid_results)} 个")

    if not report_dir:
        status = "missing"
        gaps.append("未找到信息处理员报告目录。")
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
    gaps.insert(0, f"{layer_name}依赖的正式财报晚于知识截止日，禁止复用本地未来产物。")
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
    candidates: list[Path] = []
    if main_record and main_record.get("local_relative_path"):
        stem = Path(str(main_record["local_relative_path"])).stem
        candidates.append(processor_workspace / report_type / report_year / stock_code / stem)
    if stock_code and report_year:
        candidates.extend((processor_workspace / report_type / report_year / stock_code).glob("*"))
    if target.get("report_stem"):
        candidates.extend(processor_workspace.glob(f"**/{target['report_stem']}"))
    return choose_best_dir(candidates, ["content.json", "llm_digest.json", "digest_audit.json"])


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
        "gaps": [f"缺少 {key}" for key in missing],
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


def audit_formal_financial_layer(formal_dir: Path | None, request: ResearchAuditRequest) -> dict[str, Any]:
    """审计正式财务分析层，并判断 depth/focus 是否兼容。

    参数：
        formal_dir: 正式财务分析目录。
        request: 标准化请求。
    返回值：
        正式财务分析层状态。
    """
    artifacts = build_formal_financial_artifacts(formal_dir)
    missing = [key for key in FORMAL_FINANCIAL_REQUIRED_KEYS if not artifacts[key]["exists"]]
    formal_payload = load_json(Path(artifacts["formal_financial_analysis_json"]["path"])) if artifacts["formal_financial_analysis_json"]["path"] else {}
    metadata = extract_analysis_metadata(artifacts["formal_financial_analysis_json"]["path"])
    compatibility = check_analysis_compatibility(metadata, request)
    cutoff_compatibility = check_formal_cutoff_compatibility(formal_payload, request)
    if not cutoff_compatibility["compatible"]:
        compatibility["compatible"] = False
        compatibility["reasons"].extend(cutoff_compatibility["reasons"])
    if not formal_dir:
        status = "missing"
    elif missing:
        status = "partial"
    elif not compatibility["compatible"]:
        status = "incompatible"
    else:
        status = "ready"
    gaps = [f"缺少 {key}" for key in missing]
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


def audit_valuation_layer(root: Path, target: dict[str, str], request: ResearchAuditRequest) -> dict[str, Any]:
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
            "gaps": ["缺少股票代码，无法定位估值产物。"],
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

    if exact and not missing and cutoff_compatibility["compatible"]:
        status = "ready"
        gaps: list[str] = []
    elif exact and not missing:
        status = "incompatible"
        gaps = list(cutoff_compatibility["reasons"])
    elif exact:
        status = "partial"
        gaps = [f"缺少 {key}" for key in missing]
        gaps.extend(cutoff_compatibility["reasons"])
    elif before:
        status = "stale"
        gaps = [f"最近可用估值日期为 {before.parent.name}，早于本次 as_of_date={request.as_of_date}。"]
        gaps.extend(f"缺少 {key}" for key in missing)
    elif dated and dated.future:
        status = "future_incompatible"
        gaps = [f"估值候选目录均晚于知识截止日 {request.as_of_date}，不能用于历史基准日研究。"]
    elif latest and dated is None:
        # 未设置知识截止日时保持旧调用兼容：沿用原逻辑，把最新估值目录视为 ready。
        status = "ready"
        gaps = []
    else:
        status = "missing"
        gaps = ["未找到估值报告。"]

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
        stock_code: 股票代码。
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
    """校验市场上下文三件套的截止证明及 claim 来源使用边界。"""
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
                    reasons.append(f"{label}.cutoff_audit 缺少 {count_key}，无法证明 claim 合规。")

    source_rows = sources.get("sources", []) if isinstance(sources, dict) else []
    source_status = {
        row.get("source_id"): row.get("cutoff_status")
        for row in source_rows
        if isinstance(row, dict) and row.get("source_id")
    }
    claims = package.get("claims", []) if isinstance(package, dict) else []
    future_claims = [
        claim
        for claim in claims
        if isinstance(claim, dict)
        and (
            claim.get("cutoff_status") == "future"
            or source_status.get(claim.get("source_id")) == "future"
        )
    ]
    if future_claims:
        reasons.append("市场上下文存在使用 future 来源生成的 claim。")
    return {"compatible": not reasons, "reasons": reasons, "audits": audits}


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
            "gaps": ["缺少股票代码，无法定位市场上下文包。"],
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
        gaps = [f"缺少 {key}" for key in missing]
        gaps.extend(cutoff_compatibility["reasons"])
        if not package_ready:
            gaps.append(
                f"市场上下文包状态/Gate 不满足 ready_public_proxy（当前 {package_status or 'invalid'}），"
                "不能作为可复用市场叙事代理。"
            )
    elif before:
        status = "stale"
        gaps = [f"最近可用市场上下文日期为 {before.parent.name}，早于本次 as_of_date={request.as_of_date}。"]
        gaps.extend(f"缺少 {key}" for key in missing)
        if not package_ready:
            gaps.append(
                f"该历史市场上下文包状态/Gate 不满足 ready_public_proxy（当前 {package_status or 'invalid'}）。"
            )
    elif dated and dated.future:
        status = "future_incompatible"
        gaps = [f"市场上下文候选目录均晚于知识截止日 {request.as_of_date}，不能用于历史基准日研究。"]
    elif latest and dated is None:
        # 未设置知识截止日时保持既有 Gate：最新包只有完整且通过公开网页代理 Gate 才可 ready。
        if package_ready:
            status = "ready"
            gaps = []
        else:
            status = "partial"
            gaps = [f"缺少 {key}" for key in missing]
            gaps.append(
                f"市场上下文包状态/Gate 不满足 ready_public_proxy（当前 {package_status or 'invalid'}），"
                "不能作为可复用市场叙事代理。"
            )
    else:
        status = "missing"
        gaps = ["未找到市场上下文包。"]

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
        stock_code: 股票代码。
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
        actions.append(action("resolve_target", "main", "目标不唯一，需要明确股票代码或财报年度后再继续。"))
        return actions
    if collector_status == "future_incompatible":
        actions.append(
            action(
                "resolve_knowledge_cutoff",
                "main",
                "所选财报在知识截止日之后才披露；必须改用更早财年或调整 as_of_date，不能补跑未来产物。",
            )
        )
        return actions
    if request.force_refresh:
        return [
            action("collector_refresh", "information-collector", "用户显式要求 force_refresh=true，需要重新检查或下载财报。"),
            action("processor_refresh", "information-processor", "用户显式要求 force_refresh=true，需要重建解析、digest、RAG 和摘要比对。"),
            action("financial_analysis_refresh", "financial-analyst", "用户显式要求 force_refresh=true，需要重建财务分析。"),
            action("valuation_update", "valuation-analyst", "用户显式要求 force_refresh=true，需要重新估值。"),
            action("market_context_refresh", "market-context-collector", "用户显式要求 force_refresh=true，需要重新采集网页市场上下文。"),
        ]
    if collector_status in {"missing", "partial"}:
        actions.append(action("collector_fetch", "information-collector", "财报采集层缺少正式年报 PDF 或 manifest 记录。"))
        return actions

    market_context_actions = build_market_context_next_actions(layers["market_context"])

    processor_status = layers["processor"]["status"]
    if processor_status in {"missing", "partial"}:
        actions.extend(build_processor_next_actions(layers["processor"]))
        actions.extend(market_context_actions)
        return actions

    draft_status = layers["financial_evidence_draft"]["status"]
    if draft_status in {"missing", "partial"}:
        actions.append(action("financial_evidence_draft", "financial-analyst", "财务证据草稿缺失或不完整，需要运行规则化证据草稿。"))
        actions.extend(market_context_actions)
        return actions

    formal_status = layers["formal_financial_analysis"]["status"]
    if formal_status in {"missing", "partial", "incompatible"}:
        reason = "正式财务分析缺失或不完整。"
        if formal_status == "incompatible":
            reason = "已有正式财务分析与本次 depth/focus 不兼容，应复用旧产物作为底稿并补充分析。"
        actions.append(action("financial_analysis_update", "financial-analyst", reason))
        actions.extend(market_context_actions)
        return actions

    valuation_status = layers["valuation"]["status"]
    if valuation_status in {"missing", "partial", "stale", "incompatible", "blocked", "future_incompatible"}:
        reason_map = {
            "missing": "估值报告缺失，需要基于已复用财务分析生成估值。",
            "partial": "估值产物不完整，需要补齐估值报告、证据表或审计文件。",
            "stale": "估值日期早于本次 as_of_date，只需更新估值和市场数据。",
            "incompatible": "同日估值缺少有效历史截止证明，必须按知识截止日重新估值。",
            "blocked": "估值层无法定位股票代码，需要先修正目标信息。",
            "future_incompatible": "现有估值均晚于知识截止日，必须按历史基准日重新估值。",
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
        "missing": "市场上下文包缺失，需要使用 Bocha Web Search 采集公开市场叙事和反方信号。",
        "partial": "市场上下文产物不完整或仅有查询计划，需要补齐网页搜索结果、来源表和质量 Gate。",
        "stale": "市场上下文日期早于本次 as_of_date，需要刷新热点、公司叙事和反方信号。",
        "incompatible": "同日市场上下文缺少有效历史截止证明，需要按 as_of_date 重新采集。",
        "blocked": "市场上下文层无法定位股票代码，需要先修正目标信息。",
        "future_incompatible": "现有市场上下文均晚于知识截止日，必须按历史基准日重新采集。",
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
        actions.append(action("processor_parse_pdf", "information-processor", "缺少 content.json，需要先解析 PDF。"))
        return actions
    if not artifacts.get("llm_digest_json", {}).get("exists") or not artifacts.get("digest_audit_json", {}).get("exists"):
        actions.append(action("processor_digest", "information-processor", "缺少 llm_digest 或 digest_audit，需要补 digest。"))
    elif quality_flags.get("missing_digest_chunks") or quality_flags.get("invalid_digest_results"):
        actions.append(action("processor_digest", "information-processor", "digest_audit 标记 chunk 缺失或无效，需要修复 digest。"))
    if not artifacts.get("rag_chunks_jsonl", {}).get("exists"):
        actions.append(action("processor_rag", "information-processor", "缺少 rag_index/rag_chunks.jsonl，只需补 RAG 索引。"))
    if not artifacts.get("summary_comparison_json", {}).get("exists"):
        actions.append(action("processor_summary_compare", "information-processor", "缺少 summary_comparison.json，只需补摘要比对。"))
    return actions or [action("processor_inspect", "information-processor", "处理层为 partial，但未识别出标准缺失项，需要人工检查。")]


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
    if not isinstance(audit, dict):
        return {
            "compatible": False,
            "audit": {},
            "reasons": [f"{label} 缺少 cutoff_audit，不能证明未使用知识截止日后的信息。"],
        }

    audit_date = str(
        audit.get("cutoff_date") or audit.get("as_of_date") or audit.get("knowledge_cutoff") or ""
    ).strip()
    if audit_date != cutoff_text:
        reasons.append(f"{label} cutoff_audit 日期为 {audit_date or 'missing'}，与 as_of_date={cutoff_text} 不一致。")
    status = str(audit.get("status") or "").strip().lower()
    compliant = audit.get("cutoff_compliant") if "cutoff_compliant" in audit else audit.get("compliant")
    if status and status not in CUTOFF_COMPLIANT_STATUSES:
        reasons.append(f"{label} cutoff_audit.status={status} 未通过。")
    if compliant is False:
        reasons.append(f"{label} cutoff_audit 明确标记为不 compliant。")
    elif compliant is not True and status not in CUTOFF_COMPLIANT_STATUSES:
        reasons.append(f"{label} cutoff_audit 未明确标记 compliant。")
    if require_strict and audit.get("strict_cutoff") is not True:
        reasons.append(f"{label} cutoff_audit.strict_cutoff 不是 true。")
    if audit.get("future_fact_claim_count") not in (None, 0, "0"):
        reasons.append(f"{label} 仍有 future 来源被事实 claim 使用。")
    if audit.get("undated_fact_claim_count") not in (None, 0, "0"):
        reasons.append(f"{label} undated_fact_claim_count 必须为 0。")
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
            f"正式财务分析声明的 as_of_date 为 {declared_date or 'missing'}，与请求 {request.as_of_date} 不一致。"
        )
    source_dates, invalid_dates = collect_source_report_dates(payload)
    cutoff = parse_strict_iso_date(request.as_of_date, "as_of_date")
    if not source_dates:
        reasons.append("正式财务分析未提供可核验的来源财报 published_at。")
    if invalid_dates:
        reasons.append("正式财务分析包含无法解析的来源财报 published_at。")
    future_dates = sorted({item.isoformat() for item in source_dates if item > cutoff})
    if future_dates:
        reasons.append("来源财报披露日晚于知识截止日：" + ", ".join(future_dates))
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
        reasons.append(f"已有财务分析深度为 {existing_depth}，低于本次 {requested_depth}。")
    if requested_focus and not requested_focus.issubset(existing_focus):
        missing_focus = sorted(requested_focus - existing_focus)
        reasons.append("已有财务分析未覆盖本次 focus：" + ", ".join(missing_focus))
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
    report_year = _safe_state_path_component(
        target.get("report_year") or request.get("report_year"), "unknown_year"
    )
    base = (
        root
        / "research_orchestrator_scripts"
        / "orchestrator_workspace"
        / "company_state"
    ).resolve()
    output = (base / stock_code / report_year / "research_state.json").resolve()
    if not output.is_relative_to(base):
        # 双重防线：即使未来放宽字符集，也不能突破正式状态工作区。
        raise ValueError(f"research_state 输出路径越界: {output}")
    return output


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    参数：
        无。
    返回值：
        ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="审计单家公司研究产物复用状态，并输出续跑计划。")
    parser.add_argument("--target", default="", help="公司名或股票代码；6 位数字会自动视作股票代码。")
    parser.add_argument("--stock-code", default="", help="股票代码。")
    parser.add_argument("--company-name", default="", help="公司名称。")
    parser.add_argument("--report-year", "--fiscal-year", dest="report_year", default="", help="财报所属年度，例如 2025。")
    parser.add_argument("--report-type", default="annual", help="财报类型，默认 annual。")
    parser.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard", help="本次研究深度。")
    parser.add_argument("--focus", default="", help="本次研究重点，多个重点用逗号分隔。")
    parser.add_argument("--as-of-date", default="", help="估值观察日，例如 2026-07-08。")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="项目根目录，默认自动识别。")
    parser.add_argument("--force-refresh", action="store_true", help="强制刷新所有层；默认关闭。")
    parser.add_argument("--write-state", action="store_true", help="写入 research_state.json。")
    parser.add_argument("--output", default="", help="显式指定 research_state.json 输出路径。")
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
