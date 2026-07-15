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

    parser = argparse.ArgumentParser(description="生成行业研究员输入包")
    parser.add_argument("--target", help="行业、板块或主题名；纯行业模式下建议显式传入")
    parser.add_argument("--stock-code", help="股票代码，例如 600519；公司验证模式下使用")
    parser.add_argument("--company-name", help="公司名称，例如 贵州茅台；公司验证模式下使用")
    parser.add_argument("--fiscal-year", help="财报年度，例如 2025；公司验证模式下使用")
    parser.add_argument("--as-of-date", required=True, help="信息包生成日期，格式 YYYY-MM-DD")
    parser.add_argument("--industry-name", help="显式指定主要行业，seed 缺失时可使用")
    parser.add_argument("--secondary-industry", help="显式指定细分行业")
    parser.add_argument("--classification-system", help="显式行业分类系统名称，默认 user_cli_override")
    parser.add_argument("--deliverable-type", default="investment_research", help="交付类型，例如 investment_research、theme_event_study")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="信息收集员2工作区")
    parser.add_argument("--financial-analysis-report", help="财务分析员 analyst_report.json 路径")
    parser.add_argument("--processor-content-json", help="信息处理员 content.json 路径")
    parser.add_argument("--financial-manifest", default=str(DEFAULT_FINANCIAL_MANIFEST), help="原财报收集员总清单路径")
    parser.add_argument("--seed-data", default=str(DEFAULT_SEED_DATA), help="行业 seed 数据路径")
    parser.add_argument("--company-events-file", help="本地公司事件 JSON/CSV 文件")
    parser.add_argument("--policy-regulation-file", help="本地政策监管 JSON/CSV 文件")
    parser.add_argument("--industry-public-stats-file", help="本地行业公开统计 JSON/CSV 文件")
    parser.add_argument("--industry-signals-file", help="本地行业信号 JSON/CSV 文件")
    parser.add_argument("--market-valuation-file", help="本地行情估值 JSON/CSV 文件，用于接入用户稳定来源")
    parser.add_argument("--event-timeline-file", help="本地事件时间线 JSON 文件")
    parser.add_argument("--event-impacts-file", help="本地事件影响观测 JSON 文件")
    parser.add_argument("--event-name", help="事件名称，例如 战争、集采、出口管制、资本开支主线")
    parser.add_argument("--event-type", help="事件类型，例如 war_geopolitics、policy_regulation、capex_cycle")
    parser.add_argument("--event-description", help="事件描述")
    parser.add_argument("--event-start-date", help="事件开始日期，格式 YYYY-MM-DD")
    parser.add_argument("--event-end-date", help="事件结束日期或 ongoing")
    parser.add_argument("--event-status", help="事件状态，例如 announced、implemented、ongoing")
    parser.add_argument("--event-window", help="事件观察窗口，例如 未来3个月、2026Q2-Q4")
    parser.add_argument("--baseline-period", help="事件前基线窗口，例如 2025Q1-2025Q4")
    parser.add_argument("--impact-variables", help="重点影响变量，逗号分隔，例如 price,supply,logistics")
    parser.add_argument("--pricing-variable", help="重点价格变量，例如 氦气价格/进口单价/中标价/ASP")
    parser.add_argument("--affected-segments", help="受影响细分环节，逗号分隔")
    parser.add_argument("--geography-scope", help="事件影响的区域范围，例如 全球/中国进口链")
    parser.add_argument("--counterfactual-assumption", help="无事件时的基线假设")
    parser.add_argument("--offline", action="store_true", default=True, help="离线模式，只使用本地和 seed 数据")
    args = parser.parse_args()

    has_company_context = bool(args.stock_code and args.company_name and args.fiscal_year)
    has_partial_company_context = bool(args.stock_code or args.company_name or args.fiscal_year) and not has_company_context
    has_industry_target = bool(args.target or args.industry_name)

    if has_partial_company_context:
        parser.error("公司验证模式需要同时提供 --stock-code、--company-name、--fiscal-year。")
    if not has_company_context and not has_industry_target:
        parser.error("纯行业模式至少需要 --target 或 --industry-name。")

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
