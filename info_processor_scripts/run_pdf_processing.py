"""
信息处理员命令行入口。

该脚本负责把信息收集员下载好的财报 PDF 批量送入解析器，输出 LLM/代码可读的 JSON、Markdown、TXT 和图片摘要；表格 CSV 仅在显式开启时导出。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pdf_financial_report_processor import (
    DEFAULT_PROCESSOR_WORKSPACE,
    FinancialReportPdfProcessor,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COLLECTOR_WORKSPACE = PROJECT_ROOT / "info_collector_scripts" / "collector_workspace"
DEFAULT_COLLECTOR_MANIFEST = DEFAULT_COLLECTOR_WORKSPACE / "manifests" / "cninfo_all_reports.json"


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    参数：
        无。

    返回值：
        配置完成的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(
        description="解析财报 PDF，输出结构化 JSON、Markdown、TXT 和图片摘要；表格 CSV 仅在显式开启时导出。"
    )
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_PROCESSOR_WORKSPACE),
        help="信息处理员工作区，默认落到 info_processor_scripts/processor_workspace。",
    )
    parser.add_argument(
        "--collector-workspace",
        default=str(DEFAULT_COLLECTOR_WORKSPACE),
        help="信息收集员工作区，用于把 manifest 中的 local_relative_path 解析为真实 PDF 路径。",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_COLLECTOR_MANIFEST),
        help="信息收集员总 JSON 清单路径；不传 --pdf 时默认从该清单读取本地 PDF。",
    )
    parser.add_argument(
        "--pdf",
        action="append",
        default=[],
        help="直接指定要解析的 PDF 路径；可重复传入。传入后不会自动扫描 manifest。",
    )
    parser.add_argument("--stock-code", default="", help="按证券代码过滤 manifest 记录。")
    parser.add_argument("--company-name", default="", help="按公司名称包含关系过滤 manifest 记录。")
    parser.add_argument("--report-type", default="", help="按财报类型过滤，例如 annual、semiannual、q1、q3。")
    parser.add_argument("--report-year", default="", help="按财报所属年度过滤，例如 2025。")
    parser.add_argument("--announcement-id", default="", help="按公告编号过滤。")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多处理多少份 PDF；0 表示不限制。适合先做小样本验证。",
    )
    parser.add_argument(
        "--include-missing-local",
        action="store_true",
        help="默认跳过本地 PDF 不存在的 manifest 记录；开启后会把缺失记录打印为跳过原因。",
    )
    parser.add_argument(
        "--no-save-images",
        action="store_true",
        help="只生成图片摘要，不导出被保留的图片文件。",
    )
    parser.add_argument(
        "--export-table-csv",
        action="store_true",
        help="额外导出 tables/*.csv；默认关闭，因为 content.json 和 content.md 已包含表格。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="如果目标 PDF 已有 content.json，是否重新解析。",
    )
    return parser


def main() -> None:
    """
    命令行主入口。

    参数：
        无。

    返回值：
        无。
    """
    configure_stdout_encoding()
    args = build_parser().parse_args()
    processor = FinancialReportPdfProcessor(
        workspace=args.workspace,
        save_images=not args.no_save_images,
        export_table_csv=args.export_table_csv,
    )

    targets = build_targets(args)
    processed_count = 0
    skipped_count = 0

    for target in targets:
        pdf_path = target["pdf_path"]
        source_record = target.get("source_record")
        if not pdf_path.exists():
            skipped_count += 1
            print(f"跳过：本地 PDF 不存在：{pdf_path}")
            continue

        report = processor.process_pdf(
            pdf_path,
            source_record=source_record,
            overwrite=args.overwrite,
        )
        processed_count += 1
        print(
            "已处理："
            f"{pdf_path} | 页数={report.page_count} | "
            f"表格={sum(len(page.tables) for page in report.pages)} | "
            f"保留图片={sum(1 for page in report.pages for image in page.images if image.decision == 'keep')} | "
            f"输出={report.outputs.get('markdown', '')}"
        )

    print(f"处理完成：成功 {processed_count} 份，跳过 {skipped_count} 份。")
    print(f"处理员工作区：{Path(args.workspace).resolve()}")


def configure_stdout_encoding() -> None:
    """
    将标准输出切换为 UTF-8，避免 Windows Bash 捕获中文帮助文本时出现乱码。

    参数：
        无。

    返回值：
        无。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_targets(args: argparse.Namespace) -> list[dict[str, Any]]:
    """
    构建待处理 PDF 列表。

    参数：
        args: 命令行参数。

    返回值：
        每个元素包含 pdf_path 和可选 source_record。
    """
    if args.pdf:
        return apply_limit(
            [{"pdf_path": Path(pdf_path).resolve(), "source_record": None} for pdf_path in args.pdf],
            args.limit,
        )

    manifest_path = Path(args.manifest).resolve()
    collector_workspace = Path(args.collector_workspace).resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"信息收集员 manifest 不存在: {manifest_path}")

    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    targets: list[dict[str, Any]] = []
    for record in records:
        if not record_matches_filters(record, args):
            continue
        local_relative_path = record.get("local_relative_path", "")
        if not local_relative_path:
            continue
        pdf_path = collector_workspace / local_relative_path
        if pdf_path.exists() or args.include_missing_local:
            targets.append({"pdf_path": pdf_path.resolve(), "source_record": record})
    return apply_limit(targets, args.limit)


def record_matches_filters(record: dict[str, Any], args: argparse.Namespace) -> bool:
    """
    判断 manifest 记录是否满足命令行过滤条件。

    参数：
        record: 信息收集员 manifest 中的一条记录。
        args: 命令行参数。

    返回值：
        满足条件返回 True，否则返回 False。
    """
    filters = {
        "stock_code": args.stock_code,
        "report_type": args.report_type,
        "report_year": args.report_year,
        "announcement_id": args.announcement_id,
    }
    for field_name, expected_value in filters.items():
        if expected_value and str(record.get(field_name, "")) != expected_value:
            return False
    if args.company_name and args.company_name not in str(record.get("company_name", "")):
        return False
    return True


def apply_limit(targets: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """
    应用最大处理数量限制。

    参数：
        targets: 待处理列表。
        limit: 最大数量，0 表示不限制。

    返回值：
        截断后的列表。
    """
    if limit <= 0:
        return targets
    return targets[:limit]


if __name__ == "__main__":
    main()
