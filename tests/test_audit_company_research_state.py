"""公司研究状态审计器的单元测试。

这些测试使用临时工作区构造最小产物树，避免依赖真实投研数据。
测试重点不是验证财务结论，而是验证“已有产物不重跑、只补缺口”的调度语义。
"""

from __future__ import annotations

import json
import shutil
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
        self.assertEqual(state["layers"]["valuation"]["candidate_date_audit"]["exact"], ["2026-07-08"])
        self.assertEqual(state["layers"]["market_context"]["status"], "ready")
        self.assertEqual(state["layers"]["market_context"]["candidate_date_audit"]["exact"], ["2026-07-08"])
        self.assertTrue(state["reusable"]["processor"])
        self.assertTrue(state["reusable"]["market_context"])
        self.assertIn("information-processor", state["skipped_actions"])
        self.assertIn("financial-analyst", state["skipped_actions"])
        self.assertIn("valuation-analyst", state["skipped_actions"])
        self.assertIn("market-context-collector", state["skipped_actions"])
        self.assertEqual(state["knowledge_cutoff"], "2026-07-08")
        self.assertEqual(state["next_actions"], [])

    def test_rejects_non_strict_or_invalid_as_of_date(self) -> None:
        """知识截止日必须是有效的十位 ISO 日期，避免宽松解析改变证据边界。"""
        for invalid in ("20260708", "2026-7-8", "2026-02-30", "2026-07-08T00:00:00"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                audit_company_research_state(
                    self.project_root,
                    ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date=invalid),
                )

    def test_future_only_report_blocks_local_processor_and_financial_artifacts(self) -> None:
        """只有截止日后财报时，本地未来解析和财务文件也不得被误判为 ready。"""
        build_company_workspace(self.project_root, valuation_date="2026-03-31", market_context_date="2026-03-31")

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-03-31"),
        )

        self.assertEqual(state["layers"]["collector"]["status"], "future_incompatible")
        self.assertEqual(state["layers"]["collector"]["date_audit"]["future_main_count"], 1)
        self.assertEqual(state["layers"]["collector"]["date_audit"]["eligible_count"], 0)
        for layer_name in ("processor", "financial_evidence_draft", "formal_financial_analysis"):
            self.assertEqual(state["layers"][layer_name]["status"], "blocked", layer_name)
            self.assertFalse(state["reusable"][layer_name], layer_name)
        self.assertEqual([item["step"] for item in state["next_actions"]], ["resolve_knowledge_cutoff"])
        self.assertTrue(state["summary"]["has_blocker"])

    def test_same_fiscal_year_selects_latest_version_not_after_cutoff(self) -> None:
        """同财年存在多版年报时，只能在截止日前版本中选择披露日期最新的一版。"""
        build_company_workspace(self.project_root, valuation_date="2026-06-30", market_context_date="2026-06-30")
        collector_workspace = self.project_root / "info_collector_scripts" / "collector_workspace"
        manifest_path = collector_workspace / "manifests" / "cninfo_all_reports.json"
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
        for published_at, suffix in (("2026-06-15", "修订版"), ("2026-07-10", "最终版")):
            relative_path = f"reports/annual/2025/600519/600519-贵州茅台-2025年年报-{suffix}.pdf"
            touch(collector_workspace / relative_path)
            records.append(
                {
                    "stock_code": "600519",
                    "company_name": "贵州茅台",
                    "report_type": "annual",
                    "report_year": "2025",
                    "title": f"贵州茅台2025年年度报告{suffix}",
                    "local_relative_path": relative_path,
                    "title_classification": "annual_full",
                    "record_kind": "report",
                    "published_at": published_at,
                }
            )
        records.append(
            {
                "stock_code": "600519",
                "company_name": "贵州茅台",
                "report_type": "annual",
                "report_year": "2025",
                "title": "贵州茅台2025年年度报告日期缺失版",
                "local_relative_path": "reports/annual/2025/600519/undated.pdf",
                "title_classification": "annual_full",
                "record_kind": "report",
                "published_at": "",
            }
        )
        write_json(manifest_path, records)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-06-30"),
        )

        collector = state["layers"]["collector"]
        self.assertEqual(collector["status"], "ready")
        self.assertEqual(collector["selected_record"]["published_at"], "2026-06-15")
        self.assertEqual(collector["date_audit"]["future_count"], 1)
        self.assertEqual(collector["date_audit"]["undated_count"], 1)
        self.assertEqual(collector["date_audit"]["eligible_count"], 3)

    def test_partial_market_proxy_is_not_promoted_to_ready(self) -> None:
        """来源质量 Gate 未通过时，四个文件存在也只能是 partial 且不可复用。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = (
            self.project_root
            / "market_context_collector_scripts"
            / "collector_workspace"
            / "packages"
            / "600519"
            / "2026-07-08"
        )
        write_json(
            market_dir / "market_context_package.json",
            {
                "status": "partial_with_public_sources",
                "usage_boundary": {"data_type": "public_web_search_proxy"},
                "quality_gate": {
                    "can_support_market_expectation_proxy": False,
                    "max_confidence": "low",
                },
            },
        )
        write_json(market_dir / "collection_audit.json", {"status": "partial_with_public_sources"})

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        self.assertEqual(state["layers"]["market_context"]["status"], "partial")
        self.assertFalse(state["reusable"]["market_context"])
        self.assertTrue(any(action["step"] == "market_context_update" for action in state["next_actions"]))

    def test_invalid_market_package_json_is_not_ready(self) -> None:
        """损坏 JSON 不能因其余文件名存在而被判为 ready。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        package = (
            self.project_root
            / "market_context_collector_scripts"
            / "collector_workspace"
            / "packages"
            / "600519"
            / "2026-07-08"
            / "market_context_package.json"
        )
        package.write_text("{broken", encoding="utf-8")
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        self.assertEqual(state["layers"]["market_context"]["status"], "partial")
        self.assertFalse(state["reusable"]["market_context"])

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
        self.assertEqual(state["layers"]["valuation"]["candidate_date_audit"]["before"], ["2026-05-27"])
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

    def test_historical_formal_analysis_prefers_exact_dated_directory(self) -> None:
        """历史模式应优先复用 as_of 精确目录，而不是同报告根目录的旧文件。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        formal = state["layers"]["formal_financial_analysis"]
        self.assertEqual(formal["status"], "ready")
        self.assertTrue(formal["report_dir"]["path"].replace("\\", "/").endswith("/as_of/2026-07-08"))

    def test_legacy_root_formal_without_cutoff_audit_is_incompatible(self) -> None:
        """历史模式找不到 dated 快照时，旧根目录缺截止证明不得 ready。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        financial_root = (
            self.project_root
            / "financial_analyst_scripts"
            / "analyst_workspace"
            / "reports"
            / "annual"
            / "2025"
            / "600519"
            / "600519-贵州茅台-2025年年报"
        )
        shutil.rmtree(financial_root / "as_of")
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        formal = state["layers"]["formal_financial_analysis"]
        self.assertEqual(formal["status"], "incompatible")
        self.assertTrue(any("cutoff_audit" in reason for reason in formal["gaps"]))

    def test_formal_analysis_rejects_future_source_report(self) -> None:
        """dated 正式财务分析若引用截止日后披露的财报，应判为 incompatible。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        formal_json = next(
            self.project_root.glob("financial_analyst_scripts/**/as_of/2026-07-08/formal_financial_analysis.json")
        )
        payload = json.loads(formal_json.read_text(encoding="utf-8"))
        payload["source_report"]["published_at"] = "2026-07-09"
        write_json(formal_json, payload)
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        formal = state["layers"]["formal_financial_analysis"]
        self.assertEqual(formal["status"], "incompatible")
        self.assertTrue(any("晚于知识截止日" in reason for reason in formal["gaps"]))

    def test_exact_valuation_without_cutoff_proof_is_incompatible(self) -> None:
        """历史同日估值缺少 valuation_audit.cutoff_audit 时不得 ready。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        valuation_audit = next(self.project_root.glob("valuation_analyst_scripts/**/2026-07-08/valuation_audit.json"))
        write_json(valuation_audit, {"status": "completed"})
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        self.assertEqual(state["layers"]["valuation"]["status"], "incompatible")
        self.assertFalse(state["reusable"]["valuation"])

    def test_legacy_exact_market_context_without_cutoff_proof_is_incompatible(self) -> None:
        """旧同日市场包即使四件套和质量 Gate 完整，缺截止证明也不得 ready。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        for filename in ("market_context_package.json", "market_context_sources.json", "collection_audit.json"):
            path = market_dir / filename
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("cutoff_audit", None)
            write_json(path, payload)
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        self.assertEqual(state["layers"]["market_context"]["status"], "incompatible")
        self.assertFalse(state["reusable"]["market_context"])

    def test_exact_market_context_requires_compliant_cutoff_audits(self) -> None:
        """市场三件套若使用 future claim 或 undated 事实，历史同日包不得 ready。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        package = json.loads((market_dir / "market_context_package.json").read_text(encoding="utf-8"))
        sources = json.loads((market_dir / "market_context_sources.json").read_text(encoding="utf-8"))
        package["claims"] = [{"source_id": "SRC-FUTURE", "cutoff_status": "future"}]
        package["cutoff_audit"]["future_fact_claim_count"] = 1
        package["cutoff_audit"]["undated_fact_claim_count"] = 1
        sources["sources"].append({"source_id": "SRC-FUTURE", "cutoff_status": "future"})
        write_json(market_dir / "market_context_package.json", package)
        write_json(market_dir / "market_context_sources.json", sources)
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        market = state["layers"]["market_context"]
        self.assertEqual(market["status"], "incompatible")
        self.assertTrue(any("future" in reason or "undated" in reason for reason in market["gaps"]))

    def test_marks_market_context_as_stale_without_rerunning_financial_layers(self) -> None:
        """市场上下文过旧时，应只补网页市场叙事，不重跑财报和财务分析。"""
        build_company_workspace(
            self.project_root,
            valuation_date="2026-07-08",
            market_context_date="2026-05-27",
        )

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
        self.assertEqual(state["layers"]["market_context"]["status"], "stale")
        self.assertEqual(
            state["layers"]["market_context"]["candidate_date_audit"]["before"], ["2026-05-27"]
        )
        self.assertIn("market_context_update", action_steps)
        self.assertIn("information-processor", state["skipped_actions"])
        self.assertIn("financial-analyst", state["skipped_actions"])
        self.assertIn("valuation-analyst", state["skipped_actions"])

    def test_future_only_valuation_and_market_directories_are_incompatible(self) -> None:
        """估值和市场目录只有截止日后候选时，应标记未来不兼容而不是 stale/ready。"""
        build_company_workspace(
            self.project_root,
            valuation_date="2026-07-10",
            market_context_date="2026-07-10",
        )

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        for layer_name in ("valuation", "market_context"):
            layer = state["layers"][layer_name]
            self.assertEqual(layer["status"], "future_incompatible", layer_name)
            self.assertEqual(layer["latest_available_date"], "", layer_name)
            self.assertEqual(layer["latest_discovered_date"], "2026-07-10", layer_name)
            self.assertEqual(layer["selected_candidate_date"], "", layer_name)
            self.assertEqual(layer["candidate_date_audit"]["future"], ["2026-07-10"], layer_name)
            self.assertFalse(state["reusable"][layer_name], layer_name)
        action_steps = [item["step"] for item in state["next_actions"]]
        self.assertIn("valuation_update", action_steps)
        self.assertIn("market_context_update", action_steps)


def build_company_workspace(
    project_root: Path,
    *,
    valuation_date: str,
    market_context_date: str = "2026-07-08",
    omit_processor_files: set[str] | None = None,
    formal_depth: str = "standard",
    formal_focus: str = "",
    formal_as_of_date: str = "2026-07-08",
) -> None:
    """构造一套最小公司研究产物树。

    参数：
        project_root: 临时项目根目录。
        valuation_date: 估值报告目录日期，用于测试 ready 或 stale。
        market_context_date: 市场上下文包目录日期，用于测试 ready 或 stale。
        omit_processor_files: 需要故意缺失的信息处理层文件集合。
        formal_depth: 正式财务分析记录的深度标签。
        formal_focus: 正式财务分析记录的 focus 标签。
        formal_as_of_date: dated 正式财务分析目录的知识截止日。
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
    dated_formal_dir = financial_dir / "as_of" / formal_as_of_date
    write_json(
        dated_formal_dir / "formal_financial_analysis.json",
        {
            "analysis_metadata": {
                "analysis_depth": formal_depth,
                "focus": formal_focus,
                "as_of_date": formal_as_of_date,
            },
            "cutoff_audit": {
                "cutoff_date": formal_as_of_date,
                "status": "compliant",
                "cutoff_compliant": True,
            },
            "source_report": {"published_at": "2026-04-17"},
        },
    )
    touch(dated_formal_dir / "formal_financial_analysis.md")

    valuation_dir = project_root / "valuation_analyst_scripts" / "valuation_workspace" / "reports" / stock_code / valuation_date
    write_json(
        valuation_dir / "valuation_report.json",
        {"target": {"stock_code": stock_code, "valuation_date": valuation_date}, "status": "completed"},
    )
    touch(valuation_dir / "valuation_report.md")
    write_json(valuation_dir / "valuation_evidence_table.json", {"rows": []})
    write_json(
        valuation_dir / "valuation_audit.json",
        {
            "status": "completed",
            "cutoff_audit": {
                "cutoff_date": valuation_date,
                "status": "compliant",
                "cutoff_compliant": True,
            },
        },
    )

    market_context_dir = (
        project_root
        / "market_context_collector_scripts"
        / "collector_workspace"
        / "packages"
        / stock_code
        / market_context_date
    )
    write_json(
        market_context_dir / "market_context_package.json",
        {
            "target": {"stock_code": stock_code, "company_name": company_name, "as_of_date": market_context_date},
            "status": "ready_public_proxy",
            "cutoff_audit": {
                "strict_cutoff": True,
                "cutoff_date": market_context_date,
                "status": "compliant",
                "cutoff_compliant": True,
                "future_fact_claim_count": 0,
                "undated_fact_claim_count": 0,
            },
            "source_table": [{"source_id": "SRC-001", "cutoff_status": "eligible"}],
            "claims": [{"source_id": "SRC-001", "cutoff_status": "eligible"}],
            "usage_boundary": {"data_type": "public_web_search_proxy"},
            "quality_gate": {
                "market_expectation_status": "proxy_only",
                "max_confidence": "medium_low",
                "can_support_market_expectation_proxy": True,
            },
        },
    )
    touch(market_context_dir / "market_context_package.md")
    market_cutoff_audit = {
        "strict_cutoff": True,
        "cutoff_date": market_context_date,
        "status": "compliant",
        "cutoff_compliant": True,
        "future_fact_claim_count": 0,
        "undated_fact_claim_count": 0,
    }
    write_json(
        market_context_dir / "market_context_sources.json",
        {
            "cutoff_audit": market_cutoff_audit,
            "sources": [{"source_id": "SRC-001", "cutoff_status": "eligible"}],
        },
    )
    write_json(
        market_context_dir / "collection_audit.json",
        {"status": "ready_public_proxy", "cutoff_audit": market_cutoff_audit},
    )


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
