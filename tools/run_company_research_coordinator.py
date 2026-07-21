# -*- coding: utf-8 -*-

"""用 Python 主会话跑原项目公司研究流程（脚本 + 新 agent 运行时）。

替代“Claude 主会话 /rec 自选 agent”：

    python tools/run_company_research_coordinator.py ^
        --stock-code 601138 ^
        --as-of-date 2026-05-01 ^
        --claude-bin "C:/Users/1/.local/bin/claude.exe" ^
        --workspace "d:/desk/multiagents/tmp_company_coord_601138" ^
        --keep

常用：
    --scripts-only     只跑 audit + 确定性脚本，不调 LLM agent
    --llm-only         假设脚本层已就绪，只跑 pending 的财务/估值 agent
    --force-refresh    传给 audit / plan
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research_console.company_research_coordinator import run_company_research  # noqa: E402

__test__ = False


def parse_args() -> argparse.Namespace:
    """解析命令行。"""

    parser = argparse.ArgumentParser(description="Python-owned company research coordinator.")
    parser.add_argument("--stock-code", required=True, help="A-share stock code.")
    parser.add_argument("--company-name", default="", help="Optional company name.")
    parser.add_argument("--as-of-date", required=True, help="Hard knowledge cutoff YYYY-MM-DD.")
    parser.add_argument("--target", default="", help="Optional free-text target.")
    parser.add_argument("--depth", default="standard")
    parser.add_argument("--focus", default="")
    parser.add_argument("--filing-policy", default="", help="recent_history or single_filing.")
    parser.add_argument("--report-year", default="")
    parser.add_argument("--report-type", default="")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--no-market-context", action="store_true")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800, help="Per LLM agent timeout seconds.")
    parser.add_argument("--budget", type=float, default=5.0, help="Per LLM agent max USD budget.")
    parser.add_argument(
        "--tool-mode",
        choices=("auto", "request", "permission"),
        default="permission",
    )
    parser.add_argument("--scripts-only", action="store_true", help="Skip LLM agents.")
    parser.add_argument("--llm-only", action="store_true", help="Skip deterministic scripts.")
    return parser.parse_args()


def main() -> int:
    """入口。"""

    args = parse_args()
    temporary = args.workspace is None
    workspace = (
        args.workspace.resolve()
        if args.workspace
        else Path(tempfile.mkdtemp(prefix=f"company_coord_{args.stock_code}_"))
    )
    workspace.mkdir(parents=True, exist_ok=True)

    params = {
        "stock_code": args.stock_code,
        "company_name": args.company_name,
        "target": args.target or args.stock_code,
        "as_of_date": args.as_of_date,
        "depth": args.depth,
        "focus": args.focus,
        "force_refresh": args.force_refresh,
        "run_market_context": not args.no_market_context,
    }
    if args.filing_policy:
        params["filing_policy"] = args.filing_policy
    if args.report_year:
        params["report_year"] = args.report_year
    if args.report_type:
        params["report_type"] = args.report_type

    tool_restriction = "request" if args.tool_mode in {"auto", "request"} else "permission"
    auto_fallback = args.tool_mode == "auto"

    def progress(msg: str) -> None:
        print(f"[coordinator] {msg}", flush=True)

    try:
        result = run_company_research(
            params,
            workspace=workspace,
            claude_bin=args.claude_bin,
            tool_restriction=tool_restriction,
            llm_timeout_seconds=args.timeout,
            max_budget_usd=args.budget,
            run_scripts=not args.llm_only,
            run_llm_agents=not args.scripts_only,
            auto_fallback_tool_mode=auto_fallback,
            progress=progress,
        )
        print("=" * 72)
        print("Python Company Research Coordinator")
        print("=" * 72)
        print(f"Result:     {'PASS' if result.passed else 'FAIL'} ({result.final_status})")
        print(f"Stock:      {result.stock_code}")
        print(f"As-of:      {result.as_of_date}")
        print(f"Workspace:  {result.workspace}")
        print(f"State:      {result.research_state_path}")
        print(f"Report:     {result.workspace / 'company_research_report.json'}")
        print()
        print("Steps:")
        for step in result.steps:
            print(
                f"  [{step.status:10}] {step.step_id:28} owner={step.owner} kind={step.kind}"
                + (f" agent={step.agent_id}" if step.agent_id else "")
            )
            if step.tool_names:
                print(f"             tools={step.tool_names}")
            if step.detail and step.status in {"failed", "degraded"}:
                print(f"             detail={step.detail[:200]}")
        if result.errors:
            print("Errors:")
            for err in result.errors:
                print(f"  - {err}")
        print("=" * 72)
        return 0 if result.passed else 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if temporary and not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
