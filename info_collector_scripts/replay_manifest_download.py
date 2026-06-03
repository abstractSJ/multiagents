"""
基于历史清单重放财报下载。

该脚本用于在以下场景中恢复信息收集员的执行结果：
1. 已有 JSON 清单，但本地 PDF 丢失，需要按清单重新下载；
2. 需要把历史采集结果迁移回统一工作区；
3. 需要验证“记录的来源路径 + 本地相对路径”是否足以完整复现实验结果。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cninfo_financial_report_collector import (
    CninfoFinancialReportCollector,
    ReportRecord,
)

DEFAULT_WORKSPACE = Path(__file__).resolve().parent / "collector_workspace"


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    返回值：
        配置完成的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(
        description="根据历史 JSON 清单重放巨潮资讯财报下载，默认仍落回统一工作区。"
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="JSON 清单文件路径，可以是当前总清单，也可以是旧批次清单。",
    )
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help="目标工作区目录，默认是统一的 collector_workspace。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="若本地文件已存在，是否覆盖。",
    )
    return parser


def load_records(manifest_path: str | Path) -> list[ReportRecord]:
    """
    从 JSON 清单加载财报记录。

    参数：
        manifest_path: JSON 清单路径。

    返回值：
        ReportRecord 列表。
    """
    collector = CninfoFinancialReportCollector(workspace=DEFAULT_WORKSPACE)
    return collector._load_manifest_records(manifest_path)


def main() -> None:
    """
    命令行主入口。

    返回值：
        无。
    """
    args = build_parser().parse_args()
    records = load_records(args.manifest)
    collector = CninfoFinancialReportCollector(workspace=args.workspace)

    success_count = 0
    for record in records:
        collector.download_report(record, overwrite=args.overwrite)
        if record.download_status in {"downloaded", "existing"}:
            success_count += 1

    print(f"重放完成，共处理 {len(records)} 条记录，成功 {success_count} 条。")
    print(f"目标工作区: {Path(args.workspace).resolve()}")


if __name__ == "__main__":
    main()
