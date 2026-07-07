"""公司研究状态审计器的单元测试。

这些测试使用临时工作区构造最小产物树，避免依赖真实投研数据。
测试重点不是验证财务结论，而是验证“已有产物不重跑、只补缺口”的调度语义。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_orchestrator_scripts.audit_company_research_state import (
    ResearchAuditRequest,
    audit_company_research_state,
)


class CompanyResearchStateAuditTest(unittest.TestCase):
    """验证公司研究状态审计器对复用、缺口、时效和兼容性的判断。"""

    def setUp(self) -> None:
        """为每个测试创建独立临时项目根目录，避免测试之间共享状态。"""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        """清理临时目录，确保测试不会污染真实工作区。"""
        self.temp_dir.cleanup()

    def test_reuses_all_ready_layers_when_current_valuation_exists(self) -> None:
        """当所有层都完整且估值日期匹配时，应跳过全链路且不给新增动作。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(
                stock_code="600519",
                report_year="2025",
                report_type="annual",
                depth="quick",
                as_of_date="2026-07-08",
            ),
        )

        self.assertEqual(state["layers"]["collector"]["status"], "ready")
        self.assertEqual(state["layers"]["processor"]["status"], "ready")
        self.assertEqual(state["layers"]["financial_evidence_draft"]["status"], "ready")
        self.assertEqual(state["layers"]["formal_financial_analysis"]["status"], "ready")
        self.assertEqual(state["layers"]["valuation"]["status"], "ready")
        self.assertTrue(state["reusable"]["processor"])
        self.assertIn("information-processor", state["skipped_actions"])
        self.assertIn("financial-analyst", state["skipped_actions"])
        self.assertIn("valuation-analyst", state["skipped_actions"])
        self.assertEqual(state["next_actions"], [])

    def test_only_requests_rag_when_processor_layer_lacks_rag_chunks(self) -> None:
        """处理层只缺 RAG 时，应只补 RAG，不要求重跑 PDF 解析或 digest。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08", omit_processor_files={"rag_chunks"})

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(
                stock_code="600519",
                report_year="2025",
                report_type="annual",
                depth="standard",
                as_of_date="2026-07-08",
            ),
        )

        action_steps = [action["step"] for action in state["next_actions"]]
        self.assertEqual(state["layers"]["processor"]["status"], "partial")
        self.assertIn("processor_rag", action_steps)
        self.assertNotIn("processor_parse_pdf", action_steps)
        self.assertNotIn("processor_digest", action_steps)
        self.assertIn("information-collector", state["skipped_actions"])

    def test_marks_old_valuation_as_stale_without_rerunning_upstream_layers(self) -> None:
        """估值日期过旧时，只要求更新估值层，上游财报和财务分析继续复用。"""
        build_company_workspace(self.project_root, valuation_date="2026-05-27")

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(
                stock_code="600519",
                report_year="2025",
                report_type="annual",
                depth="standard",
                as_of_date="2026-07-08",
            ),
        )

        action_steps = [action["step"] for action in state["next_actions"]]
        self.assertEqual(state["layers"]["valuation"]["status"], "stale")
        self.assertIn("valuation_update", action_steps)
        self.assertIn("information-processor", state["skipped_actions"])
        self.assertIn("financial-analyst", state["skipped_actions"])

    def test_marks_formal_financial_analysis_incompatible_for_higher_depth_or_new_focus(self) -> None:
        """已有正式财务分析深度或 focus 不匹配时，应补财务分析但不重跑上游证据。"""
        build_company_workspace(
            self.project_root,
            valuation_date="2026-07-08",
            formal_depth="quick",
            formal_focus="",
        )

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(
                stock_code="600519",
                report_year="2025",
                report_type="annual",
                depth="deep",
                focus="cashflow",
                as_of_date="2026-07-08",
            ),
        )

        action_steps = [action["step"] for action in state["next_actions"]]
        self.assertEqual(state["layers"]["formal_financial_analysis"]["status"], "incompatible")
        self.assertIn("financial_analysis_update", action_steps)
        self.assertIn("information-processor", state["skipped_actions"])
        self.assertNotIn("processor_parse_pdf", action_steps)


def build_company_workspace(
    project_root: Path,
    *,
    valuation_date: str,
    omit_processor_files: set[str] | None = None,
    formal_depth: str = "standard",
    formal_focus: str = "",
) -> None:
    """构造一套最小公司研究产物树。

    参数：
        project_root: 临时项目根目录。
        valuation_date: 估值报告目录日期，用于测试 ready 或 stale。
        omit_processor_files: 需要故意缺失的信息处理层文件集合。
        formal_depth: 正式财务分析记录的深度标签。
        formal_focus: 正式财务分析记录的 focus 标签。
    返回值：
        无。该函数直接写入临时目录。
    """
    omitted = omit_processor_files or set()
    stock_code = "600519"
    company_name = "贵州茅台"
    report_year = "2025"
    report_type = "annual"
    report_stem = "600519-贵州茅台-2025年年报"

    collector_workspace = project_root / "info_collector_scripts" / "collector_workspace"
    manifest_path = collector_workspace / "manifests" / "cninfo_all_reports.json"
    pdf_relative_path = f"reports/{report_type}/{report_year}/{stock_code}/{report_stem}.pdf"
    summary_relative_path = f"reports/{report_type}/{report_year}/{stock_code}/{report_stem}-摘要.pdf"
    touch(collector_workspace / pdf_relative_path)
    touch(collector_workspace / summary_relative_path)
    write_json(
        manifest_path,
        [
            {
                "stock_code": stock_code,
                "company_name": company_name,
                "report_type": report_type,
                "report_year": report_year,
                "title": "贵州茅台2025年年度报告",
                "local_relative_path": pdf_relative_path,
                "title_classification": "annual_full",
                "record_kind": "report",
                "published_at": "2026-04-17",
            },
            {
                "stock_code": stock_code,
                "company_name": company_name,
                "report_type": report_type,
                "report_year": report_year,
                "title": "贵州茅台2025年年度报告摘要",
                "local_relative_path": summary_relative_path,
                "title_classification": "annual_summary",
                "record_kind": "summary",
                "published_at": "2026-04-17",
            },
        ],
    )

    processor_dir = (
        project_root
        / "info_processor_scripts"
        / "processor_workspace"
        / "parsed_reports"
        / report_type
        / report_year
        / stock_code
        / report_stem
    )
    write_json(
        processor_dir / "content.json",
        {
            "pdf_sha256": "sha256-for-test",
            "document_metadata": {
                "stock_code": stock_code,
                "company_name": company_name,
                "report_type": report_type,
                "report_year": report_year,
                "pdf_stem": report_stem,
            },
            "pages": [],
        },
    )
    touch(processor_dir / "content.md")
    write_json(processor_dir / "llm_digest.json", {"document_metadata": {"stock_code": stock_code}})
    touch(processor_dir / "llm_digest.md")
    write_json(processor_dir / "digest_audit.json", {"missing_chunks": [], "invalid_results": []})
    if "rag_chunks" not in omitted:
        touch(processor_dir / "rag_index" / "rag_chunks.jsonl")
    if "summary_comparison" not in omitted:
        write_json(processor_dir / "summary_comparison.json", {"coverage": {}})
        touch(processor_dir / "summary_comparison.md")

    financial_dir = (
        project_root
        / "financial_analyst_scripts"
        / "analyst_workspace"
        / "reports"
        / report_type
        / report_year
        / stock_code
        / report_stem
    )
    write_json(
        financial_dir / "analyst_report.json",
        {"analysis_metadata": {"analysis_depth": "standard", "focus": ""}},
    )
    touch(financial_dir / "analyst_report.md")
    write_json(financial_dir / "evidence_check.json", {"checked_total": 0})
    write_json(financial_dir / "analyst_audit.json", {"analysis_complete": True})
    write_json(
        financial_dir / "formal_financial_analysis.json",
        {"analysis_metadata": {"analysis_depth": formal_depth, "focus": formal_focus}},
    )
    touch(financial_dir / "formal_financial_analysis.md")

    valuation_dir = project_root / "valuation_analyst_scripts" / "valuation_workspace" / "reports" / stock_code / valuation_date
    write_json(
        valuation_dir / "valuation_report.json",
        {"target": {"stock_code": stock_code, "valuation_date": valuation_date}, "status": "completed"},
    )
    touch(valuation_dir / "valuation_report.md")
    write_json(valuation_dir / "valuation_evidence_table.json", {"rows": []})
    write_json(valuation_dir / "valuation_audit.json", {"status": "completed"})


def write_json(path: Path, payload: object) -> None:
    """写入 JSON 文件，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def touch(path: Path, content: str = "test") -> None:
    """写入一个最小文本文件，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
