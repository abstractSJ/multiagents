"""
信息收集员命令行入口。

该脚本面向后续 agent 或人工运维直接调用，负责把“查找财报、更新总清单、可选下载 PDF”串成一条完整执行链路。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cninfo_financial_report_collector import (
    CninfoFinancialReportCollector,
    REPORT_TYPE_CONFIG,
)

DEFAULT_WORKSPACE = Path(__file__).resolve().parent / "collector_workspace"


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    返回值：
        配置完成的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(
        description="从巨潮资讯检索 A 股定期财报，更新总清单，并可选地下载 PDF 到统一工作区。"
    )
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help="统一工作区目录，默认落到脚本目录下的 collector_workspace。",
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="查询开始日期，格式 YYYY-MM-DD。注意：这里过滤的是披露日期，不是财报所属年度。",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="查询结束日期，格式 YYYY-MM-DD。注意：这里过滤的是披露日期，不是财报所属年度。",
    )
    parser.add_argument(
        "--report-types",
        nargs="+",
        default=["annual"],
        choices=[*REPORT_TYPE_CONFIG.keys(), "all"],
        help="财报类型，可选 annual、semiannual、q1、q3、all。",
    )
    parser.add_argument(
        "--keyword",
        default="",
        help="搜索关键字，可传证券代码或公司名称；为空时抓取全市场。",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=30,
        help="单页抓取条数，默认 30。",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="最多抓取页数；不传则抓完整个结果集。",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.2,
        help="翻页间隔秒数，用于降低请求过于密集带来的不稳定性。",
    )
    parser.add_argument(
        "--split-windows",
        action="store_true",
        help="全市场抓取时按月/日拆分披露窗口，降低源站高页码重复导致的漏采风险。",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="是否同步下载 PDF。不开启时只更新 2 份总清单，不会真正落盘 PDF。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="下载时若文件已存在，是否覆盖。",
    )
    return parser


def main() -> None:
    """
    命令行主入口。

    返回值：
        无。
    """
    args = build_parser().parse_args()
    collector = CninfoFinancialReportCollector(workspace=args.workspace)
    records, json_manifest_path, csv_manifest_path = collector.collect(
        report_types=args.report_types,
        start_date=args.start_date,
        end_date=args.end_date,
        keyword=args.keyword,
        page_size=args.page_size,
        max_pages=args.max_pages,
        sleep_seconds=args.sleep_seconds,
        download=args.download,
        overwrite=args.overwrite,
        split_windows=args.split_windows,
    )

    print(f"采集完成，本次共获取 {len(records)} 条财报记录。")
    print(f"总 JSON 清单: {json_manifest_path}")
    print(f"总 CSV 清单: {csv_manifest_path}")

    downloaded_count = sum(
        1 for record in records if record.download_status in {"downloaded", "existing"}
    )
    if args.download:
        print(f"本次已处理 PDF {downloaded_count} 份。")
    else:
        print("当前为清单模式：只更新总清单，不下载 PDF。")

    if args.split_windows:
        repeated_windows = [
            audit for audit in collector.last_collection_audits if audit.get("warning")
        ]
        print(f"拆窗审计窗口数: {len(collector.last_collection_audits)}")
        print(f"存在覆盖风险的窗口数: {len(repeated_windows)}")
        if repeated_windows:
            print("首个覆盖风险窗口：")
            first_warning = repeated_windows[0]
            print(f"- 财报类型: {first_warning.get('report_type', '')}")
            print(f"- 窗口: {first_warning.get('window_start', '')} ~ {first_warning.get('window_end', '')}")
            print(f"- 风险: {first_warning.get('warning', '')}")

    if records:
        sample_record = records[0]
        print("首条样例：")
        print(f"- 证券代码: {sample_record.stock_code}")
        print(f"- 公司名称: {sample_record.company_name}")
        print(f"- 财报类型: {sample_record.report_type_label}")
        print(f"- 财报年度: {sample_record.report_year}")
        print(f"- 披露日期: {sample_record.published_at}")
        print(f"- 本地路径: {Path(args.workspace).resolve() / sample_record.local_relative_path}")


if __name__ == "__main__":
    main()
