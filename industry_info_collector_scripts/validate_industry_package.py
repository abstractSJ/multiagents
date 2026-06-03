"""行业研究输入包校验脚本。

该脚本校验信息收集员2生成的 JSON 是否满足行业研究员的最低输入要求，
并把校验结果写回输入包同目录下的 validation_result.json。

本版本同时兼容：
- 原有公司导向输入包；
- schema 1.2 的事件研究 / 纯行业输入包；
- `theme_event_study` 的最低成熟度 Gate。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


REQUIRED_TOP_LEVEL = [
    "schema_version",
    "collector_name",
    "task_id",
    "information_package",
    "source_ref_index",
    "limitations",
]


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="校验行业研究输入包")
    parser.add_argument("--package", required=True, help="industry_input_package.json 路径")
    parser.add_argument("--require-competitors", action="store_true", help="把同行候选少于 3 个视为错误")
    parser.add_argument("--deliverable-type", help="显式指定交付类型；未传时自动从包内推断")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    """读取 JSON 文件。"""

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def now_iso() -> str:
    """返回北京时间 ISO 时间。"""

    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def infer_deliverable_type(package: dict[str, Any], cli_value: str | None) -> str:
    """推断交付类型。

    Args:
        package: 输入包 JSON。
        cli_value: CLI 显式传入值。

    Returns:
        交付类型字符串。
    """

    if cli_value:
        return cli_value
    target = package.get("target", {})
    research_scope = package.get("research_scope", {})
    return package.get("deliverable_type") or target.get("deliverable_type") or research_scope.get("deliverable_type") or "investment_research"


def is_industry_first_package(package: dict[str, Any], deliverable_type: str) -> bool:
    """判断是否为纯行业优先输入包。"""

    company = package.get("company", {})
    target = package.get("target", {})
    return company.get("role") == "not_applicable_industry_first_package" or bool(target.get("industry_name") and deliverable_type == "theme_event_study")


def validate_theme_event_study(package: dict[str, Any], errors: list[str], warnings: list[str]) -> dict[str, Any]:
    """校验事件研究模式的最低门槛。"""

    event_study = package.get("event_study")
    if not isinstance(event_study, dict):
        errors.append("theme_event_study 缺少顶层 event_study 覆盖层")
        return {
            "theme_event_study_minimum_passed": False,
            "event_name": None,
            "event_type": None,
            "event_timeline_count": 0,
            "transmission_chain_count": 0,
            "falsification_indicator_count": 0,
            "observed_impact_count": 0,
            "event_gap_count": 0,
        }

    metadata = event_study.get("event_metadata") or {}
    baseline_and_counterfactual = event_study.get("baseline_and_counterfactual")
    event_timeline = event_study.get("event_timeline") or []
    transmission_chain = event_study.get("transmission_chain") or []
    pricing_mechanism = event_study.get("pricing_mechanism")
    observed_vs_expected = event_study.get("observed_vs_expected_impacts") or {}
    observed_impacts = observed_vs_expected.get("observed_impacts") or []
    falsification_indicators = event_study.get("falsification_indicators") or []
    event_specific_gaps = event_study.get("event_specific_gaps") or []

    if not metadata.get("event_name"):
        errors.append("theme_event_study 缺少 event_metadata.event_name")
    if not metadata.get("event_type"):
        errors.append("theme_event_study 缺少 event_metadata.event_type")
    if not metadata.get("affected_industry"):
        errors.append("theme_event_study 缺少 event_metadata.affected_industry")
    if not event_timeline:
        errors.append("theme_event_study 缺少关键事件时间线")
    if not transmission_chain:
        errors.append("theme_event_study 缺少事件传导链")
    if not baseline_and_counterfactual:
        errors.append("theme_event_study 缺少事件前基线/反事实")
    if not observed_vs_expected:
        errors.append("theme_event_study 缺少 observed_vs_expected_impacts")
    if not falsification_indicators:
        errors.append("theme_event_study 缺少证伪指标")

    impact_variables = set()
    for item in observed_impacts:
        variable_name = str(item.get("variable_name") or "").lower()
        if variable_name:
            impact_variables.add(variable_name)
    if any(keyword in impact_variables for keyword in {"price", "pricing", "profit", "margin", "cost", "asp", "fee"}) and not pricing_mechanism:
        errors.append("theme_event_study 重点变量涉及价格/利润，但缺少 pricing_mechanism")

    if not observed_impacts:
        warnings.append("theme_event_study 尚无事件后的可观察落地变量；当前更适合 partial / needs_more_evidence。")
    elif not any(item.get("current_value") not in {None, "", "unknown"} for item in observed_impacts):
        warnings.append("theme_event_study 虽然列出了观测变量，但当前值均为 unknown；仍不能视为已传导。")

    for index, item in enumerate(event_timeline, start=1):
        if not item.get("source_refs"):
            warnings.append(f"theme_event_study 事件时间线第 {index} 条缺少 source_refs。")
    for index, item in enumerate(observed_impacts, start=1):
        if not item.get("source_refs"):
            warnings.append(f"theme_event_study 观测影响第 {index} 条缺少 source_refs。")

    return {
        "theme_event_study_minimum_passed": not any(message.startswith("theme_event_study") for message in errors),
        "event_name": metadata.get("event_name"),
        "event_type": metadata.get("event_type"),
        "event_timeline_count": len(event_timeline),
        "transmission_chain_count": len(transmission_chain),
        "falsification_indicator_count": len(falsification_indicators),
        "observed_impact_count": len(observed_impacts),
        "event_gap_count": len(event_specific_gaps),
    }


def validate(package_path: Path, require_competitors: bool = False, deliverable_type_override: str | None = None) -> dict[str, Any]:
    """执行最低可用性校验。

    Args:
        package_path: 行业输入包路径。
        require_competitors: 是否强制要求至少 3 个同行候选。
        deliverable_type_override: 显式交付类型。

    Returns:
        校验结果字典。
    """

    errors: list[str] = []
    warnings: list[str] = []
    package = load_json(package_path)
    deliverable_type = infer_deliverable_type(package, deliverable_type_override)
    industry_first = is_industry_first_package(package, deliverable_type)

    for key in REQUIRED_TOP_LEVEL:
        if key not in package:
            errors.append(f"缺少顶层字段：{key}")

    company = package.get("company", {})
    target = package.get("target", {})
    if industry_first:
        if not target.get("industry_name"):
            errors.append("纯行业输入包缺少 target.industry_name")
    else:
        if not company.get("ticker"):
            errors.append("company.ticker 为空")
        if not company.get("name"):
            errors.append("company.name 为空")

    info = package.get("information_package", {})
    classification = info.get("industry_classification", {})
    if info and not info.get("company_profile") and not industry_first:
        errors.append("information_package.company_profile 为空")
    if not classification.get("primary_industry") and not target.get("industry_name"):
        errors.append("industry_classification.primary_industry 为空")
    if classification.get("primary_industry") == "未知行业" and not target.get("industry_name"):
        warnings.append("行业分类仍为未知行业，需要 seed、CLI 或人工资料补充。")

    competitors = info.get("competitors", [])
    if len(competitors) < 3:
        message = "同行候选少于 3 个；同行年报和同行财务分析可由原信息收集员和财务分析员补齐。"
        if require_competitors:
            errors.append(message)
        else:
            warnings.append(message)

    source_refs = package.get("source_ref_index", [])
    if not source_refs:
        errors.append("source_ref_index 为空")
    if not package.get("limitations"):
        errors.append("limitations 为空")

    evidence_path = package_path.with_name("evidence_table.json")
    evidence_refs = set()
    if evidence_path.exists():
        evidence = load_json(evidence_path)
        evidence_refs = {item.get("ref_id") for item in evidence.get("items", [])}
        source_ref_ids = {item.get("ref_id") for item in source_refs}
        missing_evidence = sorted(ref for ref in source_ref_ids if ref not in evidence_refs)
        if missing_evidence:
            warnings.append(f"部分 source_ref_index 没有对应 evidence item：{', '.join(missing_evidence)}")
    else:
        errors.append("缺少 evidence_table.json")

    industry_data = info.get("industry_data", {})
    company_event_count = len(info.get("company_events", []))
    policy_record_count = len(info.get("policy_and_regulation", []))
    public_stat_count = len(industry_data.get("public_stats", []))
    industry_signal_count = len(industry_data.get("industry_signals", []))
    market_data = info.get("market_data", {})
    market_status = market_data.get("collection_status")
    if market_status != "available_from_local_file":
        warnings.append("未提供可用行情估值快照，行业研究员不能据此判断估值高低。")
    else:
        price_snapshot = market_data.get("price_snapshot") or {}
        valuation_snapshot = market_data.get("valuation_snapshot") or {}
        if not any(
            value not in {None, ""}
            for value in [
                price_snapshot.get("price"),
                valuation_snapshot.get("market_cap"),
                valuation_snapshot.get("pe_ttm"),
                valuation_snapshot.get("pb"),
                valuation_snapshot.get("ps_ttm"),
                valuation_snapshot.get("ev_ebitda"),
                valuation_snapshot.get("dividend_yield"),
            ]
        ):
            warnings.append("行情估值文件已接入，但核心估值字段为空；这只能验证接口格式，不能支持估值判断。")
    if company_event_count == 0 and policy_record_count == 0 and public_stat_count == 0 and industry_signal_count == 0:
        warnings.append("未提供公司事件、政策监管、行业统计或行业信号本地适配器数据，当前主要依赖 seed 和财报链路。")

    theme_event_study_summary: dict[str, Any] | None = None
    if deliverable_type == "theme_event_study":
        theme_event_study_summary = validate_theme_event_study(package, errors, warnings)

    result = {
        "schema_version": "1.2",
        "validated_at": now_iso(),
        "package_path": str(package_path),
        "deliverable_type": deliverable_type,
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "company": company,
            "target_industry": target.get("industry_name") or classification.get("primary_industry"),
            "primary_industry": classification.get("primary_industry"),
            "secondary_industry": classification.get("secondary_industry"),
            "competitor_count": len(competitors),
            "company_event_count": company_event_count,
            "policy_record_count": policy_record_count,
            "public_stat_count": public_stat_count,
            "industry_signal_count": industry_signal_count,
            "market_data_status": market_status,
            "source_ref_count": len(source_refs),
            "evidence_item_count": len(evidence_refs),
            "limitation_count": len(package.get("limitations", [])),
        },
    }
    if theme_event_study_summary is not None:
        result["summary"].update(theme_event_study_summary)
        if not result["passed"]:
            result["recommended_status"] = "partial"
            result["recommended_actionability"] = "needs_more_evidence"
        elif not theme_event_study_summary.get("observed_impact_count"):
            result["recommended_status"] = "partial"
            result["recommended_actionability"] = "watchlist_only"

    output_path = package_path.with_name("validation_result.json")
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    """运行校验并打印结果。"""

    args = parse_args()
    result = validate(
        Path(args.package),
        require_competitors=args.require_competitors,
        deliverable_type_override=args.deliverable_type,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
