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

from filing_set_builder import write_filing_set
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
    parser = argparse.ArgumentParser(description="Generate either a single-report evidence draft or a multi-period filing-set handoff for LLM financial analysis.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--report-dir", help="Single-report directory produced by the information processor.")
    source_group.add_argument("--research-state", help="Schema 2.0 research_state.json used to build a multi-period filing-set handoff.")
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_ANALYST_WORKSPACE),
        help="Financial analyst workspace; defaults to financial_analyst_scripts/analyst_workspace.",
    )
    parser.add_argument("--output-dir", default="", help="Evidence-draft output directory; generated automatically from report_type/report_year/stock_code/pdf_stem when omitted.")
    parser.add_argument(
        "--analysis-depth",
        choices=["quick", "standard", "deep"],
        default="standard",
        help="Analysis-depth label; currently affects output audit metadata. Default: standard.",
    )
    parser.add_argument(
        "--allow-incomplete-digest",
        action="store_true",
        help="Allow a low-confidence preliminary report when the digest is incomplete.",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="Optional analysis focus, e.g. cashflow, receivable, growth, or governance.",
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
    if args.research_state:
        state_path = Path(args.research_state).resolve()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        result = write_filing_set(
            state,
            research_state_path=str(state_path),
            workspace=args.workspace,
            output=args.output_dir or None,
        )
        print("Financial filing-set handoff generated:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    analyzer = FinancialReportAnalyzer(workspace=args.workspace)
    result = analyzer.analyze_report_dir(
        args.report_dir,
        output_dir=args.output_dir or None,
        analysis_depth=args.analysis_depth,
        allow_incomplete_digest=args.allow_incomplete_digest,
        focus=args.focus,
    )
    print("Financial analysis evidence draft generated:")
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
