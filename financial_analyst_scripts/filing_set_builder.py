"""构建公司级多期财报交接包。

交接包不尝试把未经核对的累计中报数字相加或相减。它只冻结财报身份、处理产物路径、
期间语义和可执行的派生规则，供正式 financial-analyst Agent 在保留逐份引用的前提下综合。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = PROJECT_ROOT / "financial_analyst_scripts" / "analyst_workspace"
PERIOD_SEMANTICS = {
    "annual": {"months": 12, "flow_basis": "full_year", "audited_default": True},
    "q1": {"months": 3, "flow_basis": "year_to_date_cumulative", "audited_default": False},
    "semiannual": {"months": 6, "flow_basis": "year_to_date_cumulative", "audited_default": False},
    "q3": {"months": 9, "flow_basis": "year_to_date_cumulative", "audited_default": False},
}


def build_filing_set_payload(research_state: dict[str, Any], *, research_state_path: str = "") -> dict[str, Any]:
    """把 schema 2.0 research_state 转成财务分析员可直接读取的紧凑交接包。"""
    target = research_state.get("target", {}) if isinstance(research_state.get("target"), dict) else {}
    request = research_state.get("request", {}) if isinstance(research_state.get("request"), dict) else {}
    filings = research_state.get("filings") if isinstance(research_state.get("filings"), list) else []
    source_filings: list[dict[str, Any]] = []
    gaps: list[str] = []

    for filing in filings:
        processor = filing.get("processor", {}) if isinstance(filing.get("processor"), dict) else {}
        artifacts = processor.get("artifacts", {}) if isinstance(processor.get("artifacts"), dict) else {}
        identity = filing.get("identity", {}) if isinstance(filing.get("identity"), dict) else {}
        report_type = str(filing.get("report_type") or identity.get("report_type") or "")
        report_year = str(filing.get("report_year") or identity.get("report_year") or "")
        semantics = dict(PERIOD_SEMANTICS.get(report_type, {"months": 0, "flow_basis": "unknown", "audited_default": False}))

        def artifact_path(key: str) -> str:
            value = artifacts.get(key, {})
            return str(value.get("path") or "") if isinstance(value, dict) else ""

        report_dir_info = processor.get("report_dir", {})
        report_dir = str(report_dir_info.get("path") or "") if isinstance(report_dir_info, dict) else ""
        item = {
            "filing_id": str(filing.get("filing_id") or ""),
            "role": str(filing.get("role") or ""),
            "report_type": report_type,
            "report_year": report_year,
            "published_at": str((filing.get("selected_record") or {}).get("published_at") or identity.get("published_at") or ""),
            "announcement_id": str(identity.get("announcement_id") or ""),
            "local_relative_path": str(identity.get("local_relative_path") or ""),
            "pdf_sha256": str(identity.get("pdf_sha256") or ""),
            "source_ref_prefix": str(filing.get("filing_id") or f"{report_type}:{report_year}"),
            "period_semantics": semantics,
            "summary_comparison": str(filing.get("summary_comparison") or "not_applicable"),
            "processor_status": str(processor.get("status") or "missing"),
            "paths": {
                "report_dir": report_dir,
                "content_json": artifact_path("content_json"),
                "llm_digest_json": artifact_path("llm_digest_json"),
                "digest_audit_json": artifact_path("digest_audit_json"),
                "rag_chunks_jsonl": artifact_path("rag_chunks_jsonl"),
                "summary_comparison_json": artifact_path("summary_comparison_json"),
            },
        }
        source_filings.append(item)
        if item["processor_status"] != "ready":
            gaps.append(f"{report_type} {report_year} processor status is {item['processor_status']}.")

    source_filings.sort(key=lambda item: (int(item["report_year"] or 0), int(item["period_semantics"]["months"]), item["filing_id"]))
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_role": "multi_period_financial_evidence_handoff",
        "stock_code": str(target.get("stock_code") or ""),
        "company_name": str(target.get("company_name") or ""),
        "as_of_date": str(research_state.get("knowledge_cutoff") or request.get("as_of_date") or ""),
        "filing_policy": str(research_state.get("filing_policy") or request.get("filing_policy") or "single_filing"),
        "annual_lookback": int(request.get("annual_lookback") or 2),
        "research_state_path": research_state_path,
        "financial_input_fingerprint": str(research_state.get("financial_input_fingerprint") or ""),
        "source_filings": source_filings,
        "period_rules": {
            "balance_sheet": "Use each filing's stated point-in-time balance. Do not subtract balance-sheet periods.",
            "flow_statements": "Q1, semiannual, and Q3 income/cash-flow values are normally year-to-date cumulative unless the filing explicitly states otherwise.",
            "standalone_derivation": {
                "q2": "semiannual cumulative minus q1 cumulative",
                "q3": "q3 cumulative minus semiannual cumulative",
                "q4": "annual full-year minus q3 cumulative",
            },
            "derivation_gate": "Derive a standalone quarter only when metric definition, consolidation scope, currency, unit, accounting policy, and restatement basis match. Otherwise retain cumulative values and disclose the gap.",
            "cross_filing_citations": "Every claim and number must include filing_id/source_ref_prefix in addition to page or chunk identifiers.",
        },
        "quality": {
            "status": "ready" if source_filings and not gaps else "partial",
            "filing_count": len(source_filings),
            "processor_ready_count": sum(1 for item in source_filings if item["processor_status"] == "ready"),
            "gaps": gaps,
        },
    }


def default_output_path(payload: dict[str, Any], workspace: str | Path = DEFAULT_WORKSPACE) -> Path:
    """按股票代码和知识截止日生成公司级交接包路径。"""
    stock_code = str(payload.get("stock_code") or "unknown_code")
    as_of_date = str(payload.get("as_of_date") or "current")
    return Path(workspace).resolve() / "filing_sets" / stock_code / as_of_date / "filing_set.json"


def write_filing_set(
    research_state: dict[str, Any],
    *,
    research_state_path: str = "",
    workspace: str | Path = DEFAULT_WORKSPACE,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """构建并写入 filing_set.json，返回路径和核心状态。"""
    payload = build_filing_set_payload(research_state, research_state_path=research_state_path)
    output_path = Path(output).resolve() if output else default_output_path(payload, workspace)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "filing_set_json": str(output_path),
        "status": payload["quality"]["status"],
        "filing_count": payload["quality"]["filing_count"],
        "financial_input_fingerprint": payload["financial_input_fingerprint"],
    }


def build_parser() -> argparse.ArgumentParser:
    """构建独立命令行入口，供 console 和人工续跑共同使用。"""
    parser = argparse.ArgumentParser(description="Build a cutoff-safe multi-period filing-set handoff for financial analysis.")
    parser.add_argument("--research-state", required=True, help="Path to schema 2.0 research_state.json.")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Financial analyst workspace.")
    parser.add_argument("--output", default="", help="Optional explicit filing_set.json output path.")
    return parser


def main() -> None:
    """命令行主入口。"""
    args = build_parser().parse_args()
    state_path = Path(args.research_state).resolve()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    result = write_filing_set(
        payload,
        research_state_path=str(state_path),
        workspace=args.workspace,
        output=args.output or None,
    )
    print("Financial filing-set handoff generated:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
