"""公司级多期财报交接包测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from financial_analyst_scripts.filing_set_builder import build_filing_set_payload, write_filing_set


class FilingSetBuilderTest(unittest.TestCase):
    """验证累计期间语义、逐份引用前缀和指纹透传。"""

    def test_builds_period_safe_handoff(self) -> None:
        """交接包必须区分 12M 年报和 3M 累计一季报，且保留 filing_id 引用前缀。"""
        state = {
            "knowledge_cutoff": "2026-06-15",
            "filing_policy": "recent_history",
            "financial_input_fingerprint": "fingerprint-1",
            "request": {"annual_lookback": 2},
            "target": {"stock_code": "600519", "company_name": "贵州茅台"},
            "filings": [
                filing("600519:annual:2025:a1", "annual", "2025", "/annual"),
                filing("600519:q1:2026:q1", "q1", "2026", "/q1"),
            ],
        }

        payload = build_filing_set_payload(state, research_state_path="/state.json")

        self.assertEqual(payload["financial_input_fingerprint"], "fingerprint-1")
        self.assertEqual(payload["quality"]["status"], "ready")
        annual, q1 = payload["source_filings"]
        self.assertEqual(annual["period_semantics"]["months"], 12)
        self.assertEqual(q1["period_semantics"]["months"], 3)
        self.assertEqual(q1["period_semantics"]["flow_basis"], "year_to_date_cumulative")
        self.assertEqual(q1["source_ref_prefix"], "600519:q1:2026:q1")
        self.assertIn("only when", payload["period_rules"]["derivation_gate"].lower())

    def test_writes_company_date_scoped_file(self) -> None:
        """默认路径应按股票和知识截止日隔离，避免不同观察日覆盖。"""
        state = {
            "knowledge_cutoff": "2026-06-15",
            "filing_policy": "recent_history",
            "financial_input_fingerprint": "fingerprint-1",
            "request": {"annual_lookback": 2},
            "target": {"stock_code": "600519", "company_name": "贵州茅台"},
            "filings": [filing("600519:annual:2025:a1", "annual", "2025", "/annual")],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            result = write_filing_set(state, workspace=temp_dir)
            path = Path(result["filing_set_json"])
            self.assertTrue(path.exists())
            self.assertEqual(path.parts[-4:], ("filing_sets", "600519", "2026-06-15", "filing_set.json"))


def filing(filing_id: str, report_type: str, report_year: str, report_dir: str) -> dict:
    """构造最小 ready 财报状态。"""
    return {
        "filing_id": filing_id,
        "role": "test",
        "report_type": report_type,
        "report_year": report_year,
        "identity": {
            "report_type": report_type,
            "report_year": report_year,
            "announcement_id": filing_id.rsplit(":", 1)[-1],
        },
        "selected_record": {"published_at": "2026-04-29"},
        "summary_comparison": "required" if report_type == "annual" else "not_applicable",
        "processor": {
            "status": "ready",
            "report_dir": {"path": report_dir, "exists": True},
            "artifacts": {
                "content_json": {"path": f"{report_dir}/content.json", "exists": True},
                "llm_digest_json": {"path": f"{report_dir}/llm_digest.json", "exists": True},
                "digest_audit_json": {"path": f"{report_dir}/digest_audit.json", "exists": True},
                "rag_chunks_jsonl": {"path": f"{report_dir}/rag_index/rag_chunks.jsonl", "exists": True},
                "summary_comparison_json": {"path": f"{report_dir}/summary_comparison.json", "exists": report_type == "annual"},
            },
        },
    }


if __name__ == "__main__":
    unittest.main()
