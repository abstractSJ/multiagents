"""行业信息收集员2命令行入口。

该脚本生成行业研究员可消费的通用行业输入包。第一版默认离线运行，
优先复用本地财务分析员报告、信息处理员 content.json、原财报 manifest、seed 数据和用户提供的本地适配器文件。

本版本在保留原公司导向调用方式的同时，增加了事件研究和纯行业模式：
- 仍兼容 `--stock-code --company-name --fiscal-year` 的旧调用；
- 允许 `theme_event_study` 在没有锚点公司的情况下，直接围绕行业和事件生成输入包；
- 对“指定事件 + 指定行业”的研究，统一走现有行业链路，而不是另开新链路。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    # 作为包导入时使用相对路径，保证测试与其他 Python 模块可直接复用本入口。
    from .industry_info_collector import (
        DEFAULT_FINANCIAL_MANIFEST,
        DEFAULT_SEED_DATA,
        DEFAULT_WORKSPACE,
        ClassificationOverride,
        CollectionPaths,
        EventStudyRequest,
        IndustryInfoCollector,
        default_financial_analysis_path,
        default_processor_content_path,
    )
except ImportError:
    # 直接执行脚本时没有包上下文，回退到同目录绝对导入以保持原 CLI 用法兼容。
    from industry_info_collector import (
        DEFAULT_FINANCIAL_MANIFEST,
        DEFAULT_SEED_DATA,
        DEFAULT_WORKSPACE,
        ClassificationOverride,
        CollectionPaths,
        EventStudyRequest,
        IndustryInfoCollector,
        default_financial_analysis_path,
        default_processor_content_path,
    )


def optional_path(value: str | None) -> Path | None:
    """把可选字符串路径转为 Path。"""

    return Path(value) if value else None


def parse_comma_list(value: str | None) -> list[str]:
    """把逗号分隔字符串解析为列表。

    Args:
        value: 逗号分隔的参数字符串。

    Returns:
        去掉空白和空值后的字符串列表。
    """

    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="Generate an industry-researcher input package")
    parser.add_argument("--target", help="Industry, sector, or theme name; recommended explicitly in industry-only mode")
    parser.add_argument("--stock-code", help="Stock code, e.g. 600519; used in company-validation mode")
    parser.add_argument("--company-name", help="Company name, e.g. Kweichow Moutai; used in company-validation mode")
    parser.add_argument("--fiscal-year", help="Fiscal year, e.g. 2025; used in company-validation mode")
    parser.add_argument("--as-of-date", required=True, help="Package as-of date in YYYY-MM-DD format")
    parser.add_argument("--industry-name", help="Explicit primary industry; can be used when seed data is unavailable")
    parser.add_argument("--secondary-industry", help="Explicit secondary industry")
    parser.add_argument("--classification-system", help="Explicit classification-system name; default: user_cli_override")
    parser.add_argument("--deliverable-type", default="investment_research", help="Deliverable type, e.g. investment_research or theme_event_study")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Industry information collector workspace")
    parser.add_argument("--financial-analysis-report", help="Path to the financial analyst analyst_report.json")
    parser.add_argument("--processor-content-json", help="Path to the information processor content.json")
    parser.add_argument("--financial-manifest", default=str(DEFAULT_FINANCIAL_MANIFEST), help="Path to the financial-report collector master manifest")
    parser.add_argument("--seed-data", default=str(DEFAULT_SEED_DATA), help="Path to industry seed data")
    parser.add_argument("--company-events-file", help="Local company-events JSON/CSV file")
    parser.add_argument("--policy-regulation-file", help="Local policy-regulation JSON/CSV file")
    parser.add_argument("--industry-public-stats-file", help="Local public industry-statistics JSON/CSV file")
    parser.add_argument("--industry-signals-file", help="Local industry-signals JSON/CSV file")
    parser.add_argument("--market-valuation-file", help="Local market-valuation JSON/CSV file for a stable user-provided source")
    parser.add_argument("--event-timeline-file", help="Local event-timeline JSON file")
    parser.add_argument("--event-impacts-file", help="Local event-impact observations JSON file")
    parser.add_argument("--event-name", help="Event name, e.g. war, centralized procurement, export controls, or a capex cycle")
    parser.add_argument("--event-type", help="Event type, e.g. war_geopolitics, policy_regulation, or capex_cycle")
    parser.add_argument("--event-description", help="Event description")
    parser.add_argument("--event-start-date", help="Event start date in YYYY-MM-DD format")
    parser.add_argument("--event-end-date", help="Event end date or ongoing")
    parser.add_argument("--event-status", help="Event status, e.g. announced, implemented, or ongoing")
    parser.add_argument("--event-window", help="Event observation window, e.g. next 3 months or 2026Q2-Q4")
    parser.add_argument("--baseline-period", help="Pre-event baseline window, e.g. 2025Q1-2025Q4")
    parser.add_argument("--impact-variables", help="Key impact variables, comma-separated, e.g. price,supply,logistics")
    parser.add_argument("--pricing-variable", help="Key pricing variable, e.g. helium price, import unit price, tender price, or ASP")
    parser.add_argument("--affected-segments", help="Affected segments, comma-separated")
    parser.add_argument("--geography-scope", help="Geographic scope, e.g. global or China import chain")
    parser.add_argument("--counterfactual-assumption", help="No-event counterfactual baseline assumption")
    parser.add_argument("--offline", action="store_true", default=True, help="Offline mode using only local and seed data")
    args = parser.parse_args()

    has_company_context = bool(args.stock_code and args.company_name and args.fiscal_year)
    has_partial_company_context = bool(args.stock_code or args.company_name or args.fiscal_year) and not has_company_context
    has_industry_target = bool(args.target or args.industry_name)

    if has_partial_company_context:
        parser.error("Company-validation mode requires --stock-code, --company-name, and --fiscal-year together.")
    if not has_company_context and not has_industry_target:
        parser.error("Industry-only mode requires at least --target or --industry-name.")

    return args


def main() -> None:
    """执行行业输入包生成并打印机器可读摘要。"""

    args = parse_args()
    has_company_context = bool(args.stock_code and args.company_name and args.fiscal_year)

    financial_analysis_report = (
        Path(args.financial_analysis_report)
        if args.financial_analysis_report
        else default_financial_analysis_path(args.stock_code, args.company_name, args.fiscal_year)
        if has_company_context
        else None
    )
    processor_content_json = (
        Path(args.processor_content_json)
        if args.processor_content_json
        else default_processor_content_path(args.stock_code, args.company_name, args.fiscal_year)
        if has_company_context
        else None
    )

    paths = CollectionPaths(
        financial_analysis_report=financial_analysis_report,
        processor_content_json=processor_content_json,
        financial_manifest=Path(args.financial_manifest),
        seed_data=Path(args.seed_data),
        company_events_file=optional_path(args.company_events_file),
        policy_regulation_file=optional_path(args.policy_regulation_file),
        industry_public_stats_file=optional_path(args.industry_public_stats_file),
        industry_signals_file=optional_path(args.industry_signals_file),
        market_valuation_file=optional_path(args.market_valuation_file),
        event_timeline_file=optional_path(args.event_timeline_file),
        event_impacts_file=optional_path(args.event_impacts_file),
    )
    override = ClassificationOverride(
        industry_name=args.industry_name,
        secondary_industry=args.secondary_industry,
        classification_system=args.classification_system,
    )

    event_study_request = None
    if args.deliverable_type == "theme_event_study" or args.event_name or args.event_type:
        event_study_request = EventStudyRequest(
            event_name=args.event_name,
            event_type=args.event_type,
            event_description=args.event_description,
            event_start_date=args.event_start_date,
            event_end_date=args.event_end_date,
            event_status=args.event_status,
            event_window=args.event_window,
            baseline_period=args.baseline_period,
            impact_variables=parse_comma_list(args.impact_variables),
            pricing_variable=args.pricing_variable,
            affected_segments=parse_comma_list(args.affected_segments),
            geography_scope=args.geography_scope,
            counterfactual_assumption=args.counterfactual_assumption,
        )

    collector = IndustryInfoCollector(workspace=Path(args.workspace))
    result = collector.collect(
        stock_code=args.stock_code,
        company_name=args.company_name,
        fiscal_year=args.fiscal_year,
        as_of_date=args.as_of_date,
        paths=paths,
        offline=args.offline,
        classification_override=override,
        target=args.target,
        deliverable_type=args.deliverable_type,
        event_study_request=event_study_request,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
