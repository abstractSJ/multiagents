"""
财务分析员证据草稿命令行入口。

该脚本负责把信息处理员生成的单份年报分析包送入规则化证据草稿模块，
输出 analyst_report.json、analyst_report.md、evidence_check.json 和 analyst_audit.json。
这些输出只是后续 LLM 财务分析 Agent 的候选事实、证据核验和开放问题，
不能替代正式的多轮 Agent 研究结论。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from financial_report_analyzer import DEFAULT_ANALYST_WORKSPACE, FinancialReportAnalyzer


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROCESSOR_WORKSPACE = PROJECT_ROOT / "info_processor_scripts" / "processor_workspace"


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    参数：
        无。
    返回值：
        配置完成的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="基于信息处理员 digest 与 RAG 证据生成财务分析证据草稿，供 LLM Agent 复核。")
    parser.add_argument("--report-dir", required=True, help="信息处理员输出的单份报告目录。")
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_ANALYST_WORKSPACE),
        help="财务分析员工作区，默认落到 financial_analyst_scripts/analyst_workspace。",
    )
    parser.add_argument("--output-dir", default="", help="证据草稿输出目录；不传时按 report_type/report_year/stock_code/pdf_stem 自动生成。")
    parser.add_argument(
        "--analysis-depth",
        choices=["quick", "standard", "deep"],
        default="standard",
        help="分析深度标签；第一版主要影响输出审计记录，默认 standard。",
    )
    parser.add_argument(
        "--allow-incomplete-digest",
        action="store_true",
        help="允许 digest 不完整时输出低置信初步报告。",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="可选重点分析方向，例如 cashflow、receivable、growth、governance。",
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
    analyzer = FinancialReportAnalyzer(workspace=args.workspace)
    result = analyzer.analyze_report_dir(
        args.report_dir,
        output_dir=args.output_dir or None,
        analysis_depth=args.analysis_depth,
        allow_incomplete_digest=args.allow_incomplete_digest,
        focus=args.focus,
    )
    print("财务分析证据草稿生成完成：")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def configure_stdout_encoding() -> None:
    """
    将标准输出切换为 UTF-8，避免 Windows Bash 捕获中文文本时乱码。

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
