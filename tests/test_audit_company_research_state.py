"""公司研究状态审计器的单元测试。

这些测试使用临时工作区构造最小产物树，避免依赖真实投研数据。
测试重点不是验证财务结论，而是验证“已有产物不重跑、只补缺口”的调度语义。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from financial_analyst_scripts.filing_set_builder import write_filing_set
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

    def test_direct_script_entry_point_resolves_project_package(self) -> None:
        """直接执行审计脚本时也必须能解析项目包，避免控制台在首个 audit 步骤崩溃。"""
        project_root = Path(__file__).resolve().parent.parent
        script_path = project_root / "research_orchestrator_scripts" / "audit_company_research_state.py"

        # 使用项目外的临时目录作为 cwd，确保测试验证的是脚本自身的路径引导，
        # 而不是测试进程或仓库当前目录偶然提供的模块搜索路径。
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stdout + result.stderr)
        self.assertIn("Audit reusable company-research artifacts", result.stdout)

    def test_unresolved_identity_does_not_scan_unrelated_financial_years(self) -> None:
        """缺代码和财年时不得把 reports/annual 下的其他年度目录误判为当前公司产物。"""
        unrelated_year = (
            self.project_root
            / "financial_analyst_scripts"
            / "analyst_workspace"
            / "reports"
            / "annual"
            / "2025"
        )
        unrelated_year.mkdir(parents=True, exist_ok=True)
        (unrelated_year / "unrelated.json").write_text("{}", encoding="utf-8")

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(target="bank of chine", company_name="bank of chine", report_type="annual"),
        )

        self.assertEqual(state["target"]["stock_code"], "")
        self.assertEqual(state["target"]["report_year"], "")
        self.assertEqual(state["layers"]["financial_evidence_draft"]["status"], "missing")
        self.assertEqual(state["layers"]["formal_financial_analysis"]["status"], "missing")

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

    def test_newer_missing_revision_is_selected_and_requested_for_download(self) -> None:
        """较新修订版不能被旧本地 PDF 遮蔽；应选中新版并把采集层标为 partial。"""
        build_company_workspace(self.project_root, valuation_date="2026-06-30", market_context_date="2026-06-30")
        collector_workspace = self.project_root / "info_collector_scripts" / "collector_workspace"
        manifest_path = collector_workspace / "manifests" / "cninfo_all_reports.json"
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
        records.append(
            {
                "stock_code": "600519",
                "company_name": "贵州茅台",
                "report_type": "annual",
                "report_year": "2025",
                "title": "贵州茅台2025年年度报告修订版",
                "local_relative_path": "reports/annual/2025/600519/revised-missing.pdf",
                "title_classification": "annual_full",
                "record_kind": "report",
                "announcement_id": "revised-missing",
                "published_at": "2026-06-15",
            }
        )
        write_json(manifest_path, records)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-06-30"),
        )

        self.assertEqual(state["layers"]["collector"]["status"], "partial")
        self.assertEqual(state["layers"]["collector"]["selected_record"]["announcement_id"], "revised-missing")
        self.assertEqual(state["next_actions"][0]["step"], "collector_fetch")

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

    def test_formal_json_without_markdown_remains_ready_with_packaging_gap(self) -> None:
        """JSON 已完整且截止合规时，缺 Markdown 镜像不得触发实质分析重跑。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        formal_md = next(
            self.project_root.glob("financial_analyst_scripts/**/as_of/2026-07-08/formal_financial_analysis.md")
        )
        formal_md.unlink()
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        formal = state["layers"]["formal_financial_analysis"]
        self.assertEqual(formal["status"], "ready")
        self.assertTrue(any("formal_financial_analysis_md" in gap for gap in formal["gaps"]))
        self.assertTrue(state["reusable"]["formal_financial_analysis"])

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
        self.assertTrue(any("later than the knowledge cutoff" in reason for reason in formal["gaps"]))

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

    def test_direct_root_valuation_cutoff_audit_is_accepted(self) -> None:
        """独立 valuation_audit.json 可直接承载完整截止字段，不强制再套一层同名对象。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        valuation_audit = next(self.project_root.glob("valuation_analyst_scripts/**/2026-07-08/valuation_audit.json"))
        payload = json.loads(valuation_audit.read_text(encoding="utf-8"))["cutoff_audit"]
        write_json(valuation_audit, payload)
        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )
        self.assertEqual(state["layers"]["valuation"]["status"], "ready")
        self.assertTrue(state["reusable"]["valuation"])

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

    def test_exact_market_context_rejects_future_rows_even_when_exclusion_metadata_is_valid(self) -> None:
        """严格同日包若仍向模型暴露 future 行，即使排除计数非零且事实 claim 为零也必须不兼容。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        package = json.loads((market_dir / "market_context_package.json").read_text(encoding="utf-8"))
        sources = json.loads((market_dir / "market_context_sources.json").read_text(encoding="utf-8"))
        future_row = {"source_id": "SRC-FUTURE", "cutoff_status": "future"}
        package["source_table"].append(future_row)
        sources["sources"].append(future_row)
        write_json(market_dir / "market_context_package.json", package)
        write_json(market_dir / "market_context_sources.json", sources)
        update_market_cutoff_metadata(market_dir, future_excluded_count=1)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        market = state["layers"]["market_context"]
        self.assertEqual(market["status"], "incompatible")
        self.assertTrue(any("only eligible rows" in reason for reason in market["gaps"]))

    def test_exact_market_context_rejects_undated_rows_even_when_discovery_metadata_is_valid(self) -> None:
        """严格同日包若仍保留 undated 行，合法的无日期发现统计不能替代对模型来源表的实际过滤。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        package = json.loads((market_dir / "market_context_package.json").read_text(encoding="utf-8"))
        sources = json.loads((market_dir / "market_context_sources.json").read_text(encoding="utf-8"))
        undated_row = {"source_id": "SRC-UNDATED", "cutoff_status": "undated"}
        package["source_table"].append(undated_row)
        sources["sources"].append(undated_row)
        write_json(market_dir / "market_context_package.json", package)
        write_json(market_dir / "market_context_sources.json", sources)
        update_market_cutoff_metadata(market_dir, undated_discovery_count=1, undated_excluded_count=1)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        market = state["layers"]["market_context"]
        self.assertEqual(market["status"], "incompatible")
        self.assertTrue(any("only eligible rows" in reason for reason in market["gaps"]))

    def test_exact_market_context_rejects_source_id_set_mismatch(self) -> None:
        """两份模型可见来源表的 ID 集合不一致时，不得依赖任一单表把 claim 判为安全。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        sources = json.loads((market_dir / "market_context_sources.json").read_text(encoding="utf-8"))
        sources["sources"][0]["source_id"] = "SRC-OTHER"
        write_json(market_dir / "market_context_sources.json", sources)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        market = state["layers"]["market_context"]
        self.assertEqual(market["status"], "incompatible")
        self.assertTrue(any("source-ID sets" in reason for reason in market["gaps"]))

    def test_exact_market_context_rejects_claim_without_source_id(self) -> None:
        """严格同日包中的每条 claim 都必须显式回指两份来源表共同登记的 eligible 来源。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        package = json.loads((market_dir / "market_context_package.json").read_text(encoding="utf-8"))
        package["claims"] = [{"cutoff_status": "eligible"}]
        write_json(market_dir / "market_context_package.json", package)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        market = state["layers"]["market_context"]
        self.assertEqual(market["status"], "incompatible")
        self.assertTrue(any("missing source_id" in reason for reason in market["gaps"]))

    def test_exact_market_context_rejects_accepted_source_count_mismatch(self) -> None:
        """截止审计声明的接受来源数必须等于两份模型来源表中的实际安全行数，不能只靠状态字段过关。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        update_market_cutoff_metadata(market_dir, accepted_source_count=2)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        market = state["layers"]["market_context"]
        self.assertEqual(market["status"], "incompatible")
        self.assertTrue(any("does not match the safe source-row counts" in reason for reason in market["gaps"]))

    def test_exact_market_context_accepts_clean_model_rows_with_nonzero_exclusion_metadata(self) -> None:
        """被排除来源只留在审计计数中、两份模型来源表均干净时，非零排除统计仍应允许同日复用。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        update_market_cutoff_metadata(
            market_dir,
            future_excluded_count=2,
            future_source_count=2,
            undated_discovery_count=1,
            undated_excluded_count=1,
        )

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        market = state["layers"]["market_context"]
        self.assertEqual(market["status"], "ready")
        self.assertTrue(state["reusable"]["market_context"])
        for audit in market["cutoff_compatibility"]["audits"].values():
            self.assertEqual(audit["accepted_source_count"], 1)
            self.assertEqual(audit["future_excluded_count"], 2)
            self.assertEqual(audit["undated_discovery_count"], 1)

    def test_market_context_without_as_of_date_preserves_legacy_source_row_behavior(self) -> None:
        """未设置 as_of_date 时继续沿用旧 Gate，不把严格同日来源行规则扩散到实时兼容路径。"""
        build_company_workspace(self.project_root, valuation_date="2026-07-08")
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-07-08"))
        package = json.loads((market_dir / "market_context_package.json").read_text(encoding="utf-8"))
        sources = json.loads((market_dir / "market_context_sources.json").read_text(encoding="utf-8"))
        future_row = {"source_id": "SRC-FUTURE", "cutoff_status": "future"}
        package["source_table"].append(future_row)
        sources["sources"].append(future_row)
        write_json(market_dir / "market_context_package.json", package)
        write_json(market_dir / "market_context_sources.json", sources)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025"),
        )

        self.assertEqual(state["layers"]["market_context"]["status"], "ready")

    def test_non_exact_market_context_preserves_stale_legacy_behavior(self) -> None:
        """只有早于 as_of_date 的候选仍按 stale 处理，不对非精确日期包追加严格同日结构校验。"""
        build_company_workspace(
            self.project_root,
            valuation_date="2026-07-08",
            market_context_date="2026-05-27",
        )
        market_dir = next(self.project_root.glob("market_context_collector_scripts/**/600519/2026-05-27"))
        package = json.loads((market_dir / "market_context_package.json").read_text(encoding="utf-8"))
        sources = json.loads((market_dir / "market_context_sources.json").read_text(encoding="utf-8"))
        undated_row = {"source_id": "SRC-UNDATED", "cutoff_status": "undated"}
        package["source_table"].append(undated_row)
        sources["sources"].append(undated_row)
        write_json(market_dir / "market_context_package.json", package)
        write_json(market_dir / "market_context_sources.json", sources)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", report_year="2025", as_of_date="2026-07-08"),
        )

        self.assertEqual(state["layers"]["market_context"]["status"], "stale")

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

    def test_recent_history_selects_two_annuals_and_all_open_interims(self) -> None:
        """近期历史模式应同时纳入两份年报、上一年全部中报和当年已披露一季报。"""
        build_company_workspace(self.project_root, valuation_date="2026-06-15", market_context_date="2026-06-15")
        add_filing_workspace(self.project_root, "annual", "2024", "2025-04-18")
        for report_type, published_at in (
            ("q1", "2025-04-25"),
            ("semiannual", "2025-08-28"),
            ("q3", "2025-10-29"),
        ):
            add_filing_workspace(self.project_root, report_type, "2025", published_at)
        add_filing_workspace(self.project_root, "q1", "2026", "2026-04-29")

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", filing_policy="recent_history", as_of_date="2026-06-15"),
        )

        selected = {(item["report_type"], item["report_year"]) for item in state["filings"]}
        self.assertEqual(
            selected,
            {
                ("annual", "2024"),
                ("annual", "2025"),
                ("q1", "2025"),
                ("semiannual", "2025"),
                ("q3", "2025"),
                ("q1", "2026"),
            },
        )
        self.assertEqual(state["layers"]["collector"]["status"], "ready")
        self.assertEqual(state["layers"]["processor"]["status"], "ready")
        q1_entry = next(item for item in state["filings"] if item["report_type"] == "q1" and item["report_year"] == "2026")
        self.assertEqual(q1_entry["summary_comparison"], "not_applicable")
        self.assertEqual(q1_entry["processor"]["status"], "ready")
        self.assertTrue(state["financial_input_fingerprint"])
        self.assertTrue(all(item["identity"].get("pdf_sha256") for item in state["filings"]))

    def test_single_filing_hydrates_processor_pdf_hash(self) -> None:
        """单份财报模式也必须复用已解析 PDF 的真实哈希。"""
        build_company_workspace(self.project_root, valuation_date="2026-06-15", market_context_date="2026-06-15")

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(
                stock_code="600519",
                filing_policy="single_filing",
                report_type="annual",
                report_year="2025",
                as_of_date="2026-06-15",
            ),
        )

        self.assertEqual(len(state["filings"]), 1)
        self.assertEqual(state["filings"][0]["identity"]["pdf_sha256"], "sha256-for-test")

    def test_mismatched_processor_content_does_not_donate_pdf_hash(self) -> None:
        """处理目录指向另一份公告时，审计不得借用其 PDF 哈希。"""
        build_company_workspace(self.project_root, valuation_date="2026-06-15", market_context_date="2026-06-15")
        content_path = (
            self.project_root
            / "info_processor_scripts"
            / "processor_workspace"
            / "parsed_reports"
            / "annual"
            / "2025"
            / "600519"
            / "600519-贵州茅台-2025年年报"
            / "content.json"
        )
        content = json.loads(content_path.read_text(encoding="utf-8"))
        content["pdf_sha256"] = "unrelated-hash"
        content["document_metadata"]["pdf_stem"] = "other-filing"
        write_json(content_path, content)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(
                stock_code="600519",
                filing_policy="single_filing",
                report_type="annual",
                report_year="2025",
                as_of_date="2026-06-15",
            ),
        )

        self.assertEqual(state["filings"][0]["identity"]["pdf_sha256"], "")
        self.assertEqual(state["layers"]["processor"]["status"], "partial")
        self.assertIn("does not match", " ".join(state["layers"]["processor"]["gaps"]))

    def test_recent_history_requests_only_missing_required_filing_first(self) -> None:
        """已有期间必须复用；缺当年一季报时，第一批动作只应精确请求该期间。"""
        build_company_workspace(self.project_root, valuation_date="2026-06-15", market_context_date="2026-06-15")
        add_filing_workspace(self.project_root, "annual", "2024", "2025-04-18")
        for report_type, published_at in (
            ("q1", "2025-04-25"),
            ("semiannual", "2025-08-28"),
            ("q3", "2025-10-29"),
        ):
            add_filing_workspace(self.project_root, report_type, "2025", published_at)

        state = audit_company_research_state(
            self.project_root,
            ResearchAuditRequest(stock_code="600519", filing_policy="recent_history", as_of_date="2026-06-15"),
        )

        self.assertEqual(len(state["next_actions"]), 1)
        action_item = state["next_actions"][0]
        self.assertEqual(action_item["step"], "collector_fetch")
        self.assertEqual(action_item["report_type"], "q1")
        self.assertEqual(action_item["report_year"], "2026")

    def test_new_filing_identity_invalidates_formal_analysis_and_valuation(self) -> None:
        """新增修订版财报改变输入指纹后，同日正式分析和估值都不得继续复用。"""
        build_company_workspace(self.project_root, valuation_date="2026-06-15", market_context_date="2026-06-15")
        add_filing_workspace(self.project_root, "annual", "2024", "2025-04-18")
        for report_type, year, published_at in (
            ("q1", "2025", "2025-04-25"),
            ("semiannual", "2025", "2025-08-28"),
            ("q3", "2025", "2025-10-29"),
            ("q1", "2026", "2026-04-29"),
        ):
            add_filing_workspace(self.project_root, report_type, year, published_at)
        request = ResearchAuditRequest(stock_code="600519", filing_policy="recent_history", as_of_date="2026-06-15")
        state = audit_company_research_state(self.project_root, request)
        write_filing_set(
            state,
            workspace=self.project_root / "financial_analyst_scripts" / "analyst_workspace",
        )
        filing_set_dir = (
            self.project_root
            / "financial_analyst_scripts"
            / "analyst_workspace"
            / "filing_sets"
            / "600519"
            / "2026-06-15"
        )
        write_json(
            filing_set_dir / "formal_financial_analysis.json",
            {
                "financial_input_fingerprint": state["financial_input_fingerprint"],
                "analysis_metadata": {
                    "analysis_depth": "standard",
                    "focus": "",
                    "as_of_date": "2026-06-15",
                    "financial_input_fingerprint": state["financial_input_fingerprint"],
                },
                "cutoff_audit": {"cutoff_date": "2026-06-15", "status": "compliant", "cutoff_compliant": True},
                "source_filings": [item["identity"] for item in state["filings"]],
            },
        )
        touch(filing_set_dir / "formal_financial_analysis.md")
        valuation_audit = next(self.project_root.glob("valuation_analyst_scripts/**/2026-06-15/valuation_audit.json"))
        valuation_payload = json.loads(valuation_audit.read_text(encoding="utf-8"))
        valuation_payload["financial_input_fingerprint"] = state["financial_input_fingerprint"]
        write_json(valuation_audit, valuation_payload)

        ready_state = audit_company_research_state(self.project_root, request)
        self.assertEqual(ready_state["layers"]["financial_evidence_draft"]["status"], "ready")
        self.assertEqual(ready_state["layers"]["formal_financial_analysis"]["status"], "ready")
        self.assertEqual(ready_state["layers"]["valuation"]["status"], "ready")

        revise_filing_workspace(self.project_root, "q1", "2026", "2026-05-20")
        revised_state = audit_company_research_state(self.project_root, request)
        self.assertNotEqual(revised_state["financial_input_fingerprint"], state["financial_input_fingerprint"])
        self.assertEqual(revised_state["layers"]["financial_evidence_draft"]["status"], "incompatible")
        self.assertEqual(revised_state["layers"]["formal_financial_analysis"]["status"], "incompatible")
        self.assertEqual(revised_state["layers"]["valuation"]["status"], "incompatible")


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
                "accepted_source_count": 1,
                "future_excluded_count": 0,
                "undated_discovery_count": 0,
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
        "accepted_source_count": 1,
        "future_excluded_count": 0,
        "undated_discovery_count": 0,
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


def add_filing_workspace(project_root: Path, report_type: str, report_year: str, published_at: str) -> None:
    """向临时工作区追加一份正式财报及其最小可复用处理包。"""
    stock_code = "600519"
    company_name = "贵州茅台"
    report_stem = f"{stock_code}-{company_name}-{report_year}-{report_type}"
    collector_workspace = project_root / "info_collector_scripts" / "collector_workspace"
    manifest_path = collector_workspace / "manifests" / "cninfo_all_reports.json"
    records = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    relative_path = f"reports/{report_type}/{report_year}/{stock_code}/{report_stem}.pdf"
    touch(collector_workspace / relative_path)
    announcement_id = f"{report_type}-{report_year}"
    records.append(
        {
            "stock_code": stock_code,
            "company_name": company_name,
            "report_type": report_type,
            "report_year": report_year,
            "title": f"{company_name} {report_year} {report_type}",
            "local_relative_path": relative_path,
            "title_classification": f"{report_type}_full",
            "record_kind": "report",
            "announcement_id": announcement_id,
            "published_at": published_at,
        }
    )
    write_json(manifest_path, records)

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
            "pdf_sha256": f"sha-{announcement_id}",
            "document_metadata": {
                "stock_code": stock_code,
                "company_name": company_name,
                "report_type": report_type,
                "report_year": report_year,
                "pdf_stem": report_stem,
                "announcement_id": announcement_id,
            },
            "pages": [],
        },
    )
    touch(processor_dir / "content.md")
    write_json(
        processor_dir / "llm_digest.json",
        {
            "complete": True,
            "document_metadata": {
                "stock_code": stock_code,
                "report_type": report_type,
                "report_year": report_year,
                "pdf_stem": report_stem,
                "announcement_id": announcement_id,
            },
        },
    )
    write_json(processor_dir / "digest_audit.json", {"complete": True, "missing_chunks": [], "invalid_results": []})
    touch(processor_dir / "rag_index" / "rag_chunks.jsonl", "")


def revise_filing_workspace(project_root: Path, report_type: str, report_year: str, published_at: str) -> None:
    """追加一份本地存在的修订版，确保审计器选择新公告并改变输入指纹。"""
    stock_code = "600519"
    company_name = "贵州茅台"
    collector_workspace = project_root / "info_collector_scripts" / "collector_workspace"
    manifest_path = collector_workspace / "manifests" / "cninfo_all_reports.json"
    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    announcement_id = f"{report_type}-{report_year}-revised"
    stem = f"{stock_code}-{company_name}-{report_year}-{report_type}-revised"
    relative_path = f"reports/{report_type}/{report_year}/{stock_code}/{stem}.pdf"
    touch(collector_workspace / relative_path)
    records.append(
        {
            "stock_code": stock_code,
            "company_name": company_name,
            "report_type": report_type,
            "report_year": report_year,
            "title": f"{company_name} {report_year} {report_type} revised",
            "local_relative_path": relative_path,
            "title_classification": f"{report_type}_full",
            "record_kind": "report",
            "announcement_id": announcement_id,
            "published_at": published_at,
        }
    )
    write_json(manifest_path, records)
    processor_dir = (
        project_root
        / "info_processor_scripts"
        / "processor_workspace"
        / "parsed_reports"
        / report_type
        / report_year
        / stock_code
        / stem
    )
    write_json(
        processor_dir / "content.json",
        {
            "document_metadata": {
                "stock_code": stock_code,
                "report_type": report_type,
                "report_year": report_year,
                "pdf_stem": stem,
                "announcement_id": announcement_id,
            }
        },
    )
    touch(processor_dir / "content.md")
    write_json(processor_dir / "llm_digest.json", {"complete": True, "document_metadata": {"announcement_id": announcement_id}})
    write_json(processor_dir / "digest_audit.json", {"complete": True, "missing_chunks": [], "invalid_results": []})
    touch(processor_dir / "rag_index" / "rag_chunks.jsonl", "")


def update_market_cutoff_metadata(market_dir: Path, **updates: object) -> None:
    """同步更新市场三件套中的截止审计元数据。

    为什么测试必须同步三份文件：审计器要求 package、sources 与 collection audit 对同一
    截止策略给出一致证明。测试若只修改其中一份，会把“来源行泄漏”和“审计元数据不一致”
    两类问题混在一起，无法准确验证本次兼容性规则。

    参数：
        market_dir: 单个观察日的市场上下文包目录。
        updates: 需要覆盖到三份 ``cutoff_audit`` 的字段。
    返回值：
        无。函数直接覆写临时测试产物。
    """
    for filename in ("market_context_package.json", "market_context_sources.json", "collection_audit.json"):
        path = market_dir / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cutoff_audit"].update(updates)
        write_json(path, payload)


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
