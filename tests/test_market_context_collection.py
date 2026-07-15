"""市场上下文采集 v1 的单元测试。

测试只覆盖查询规划、Bocha 响应归一化和 dry-run 产物写入，不真实调用外部 Web Search，
从而避免把 API Key、网络波动或额度消耗引入自动化验证。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_context_collector_scripts.bocha_web_search_client import build_search_payload, normalize_bocha_response
from market_context_collector_scripts.market_context_query_planner import MarketContextRequest, build_query_plan
from market_context_collector_scripts.run_market_context_collection import collect_market_context, query_cache_path


class MarketContextCollectionTest(unittest.TestCase):
    """验证市场上下文采集 v1 在无真实网络调用下的核心行为。"""

    def setUp(self) -> None:
        """创建临时项目根目录，确保测试产物不会污染真实工作区。"""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        """清理临时目录。"""
        self.temp_dir.cleanup()

    def test_query_plan_covers_core_buckets_for_standard_depth(self) -> None:
        """standard 深度应覆盖市场热点、公司叙事、行业、主题、同行和反方查询。"""
        request = MarketContextRequest(
            target="贵州茅台",
            stock_code="600519",
            company_name="贵州茅台",
            industry="白酒",
            as_of_date="2026-07-08",
            depth="standard",
            focus="分红,批价",
        )

        plan = build_query_plan(request)
        buckets = {item["bucket"] for item in plan}

        self.assertLessEqual(len(plan), 32)
        self.assertIn("market_hotspots", buckets)
        self.assertIn("target_narrative", buckets)
        self.assertIn("sector_context", buckets)
        self.assertIn("theme_mapping", buckets)
        self.assertIn("peer_context", buckets)
        self.assertIn("negative_signals", buckets)
        self.assertTrue(any("贵州茅台" in item["query"] for item in plan))
        self.assertTrue(any("风险" in item["query"] or "利空" in item["query"] for item in plan))

    def test_normalizes_bocha_webpages_response(self) -> None:
        """Bocha 常见 data.webPages.value 响应应被归一化为统一搜索结果。"""
        payload = {
            "data": {
                "webPages": {
                    "value": [
                        {
                            "name": "贵州茅台投资者关系活动记录",
                            "url": "https://www.cninfo.com.cn/new/disclosure/detail",
                            "summary": "公司回应分红、渠道和批价问题。",
                            "datePublished": "2026-07-01",
                            "siteName": "巨潮资讯",
                        }
                    ]
                }
            }
        }

        results = normalize_bocha_response(payload)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "贵州茅台投资者关系活动记录")
        self.assertEqual(results[0].url, "https://www.cninfo.com.cn/new/disclosure/detail")
        self.assertIn("分红", results[0].snippet)
        self.assertEqual(results[0].published_at, "2026-07-01")

    def test_quick_query_plan_keeps_negative_signals(self) -> None:
        """quick 深度也必须保留反方查询，避免只采到热点和正向叙事。"""
        request = MarketContextRequest(
            target="600519",
            stock_code="600519",
            company_name="贵州茅台",
            industry="白酒",
            as_of_date="2026-07-08",
            depth="quick",
            focus="分红,批价,市场预期",
        )

        plan = build_query_plan(request)
        buckets = {item["bucket"] for item in plan}

        self.assertLessEqual(len(plan), 12)
        self.assertIn("negative_signals", buckets)
        self.assertIn("target_narrative", buckets)
        self.assertIn("market_hotspots", buckets)
        self.assertTrue(any("风险" in item["query"] or "利空" in item["query"] for item in plan))

    def test_strict_query_plan_uses_historical_cutoff_anchors(self) -> None:
        """严格历史模式必须移除相对今天表达，并用观察日及年份锚定每条查询。"""
        request = MarketContextRequest(
            target="600519",
            stock_code="600519",
            company_name="贵州茅台",
            industry="白酒",
            as_of_date="2021-06-30",
            depth="standard",
            focus="分红,批价",
            strict_cutoff=True,
        )

        plan = build_query_plan(request)

        self.assertGreater(len(plan), 0)
        for item in plan:
            query = item["query"]
            self.assertIn("截至 2021-06-30", query)
            self.assertIn("2021年及以前", query)
            self.assertNotIn("今日", query)
            self.assertNotIn("近期", query)
            self.assertNotIn("最近", query)
            self.assertNotIn("2026", query)
            self.assertEqual(item["cutoff_anchor"], "2021-06-30")

    def test_strict_query_plan_requires_as_of_date(self) -> None:
        """严格截止没有观察日时应立即失败，避免静默回退到今天。"""
        request = MarketContextRequest(target="600519", strict_cutoff=True)

        with self.assertRaisesRegex(ValueError, "必须提供 as_of_date"):
            build_query_plan(request)

    def test_non_strict_query_plan_keeps_existing_relative_queries(self) -> None:
        """非严格模式保持现有查询文本，避免改变当前时点采集行为。"""
        request = MarketContextRequest(
            target="600519",
            stock_code="600519",
            company_name="贵州茅台",
            industry="白酒",
            as_of_date="2021-06-30",
            depth="standard",
            strict_cutoff=False,
        )

        plan = build_query_plan(request)

        self.assertTrue(any("今日" in item["query"] for item in plan))
        self.assertTrue(any("2026" in item["query"] for item in plan))
        self.assertTrue(all("cutoff_anchor" not in item for item in plan))

    def test_bocha_payload_omits_freshness_when_none(self) -> None:
        """freshness=None 必须省略字段，而不是向 Bocha 发送 JSON null。"""
        without_freshness = build_search_payload("贵州茅台", count=8, freshness=None)
        with_freshness = build_search_payload("贵州茅台", count=8, freshness="oneMonth")

        self.assertNotIn("freshness", without_freshness)
        self.assertEqual(with_freshness["freshness"], "oneMonth")

    def test_strict_cutoff_filters_future_and_undated_claims_and_writes_audit(self) -> None:
        """严格截止应保留全部来源供审计，但事实 claim 和质量 Gate 只使用截止日前有日期来源。"""
        request = MarketContextRequest(
            target="600519",
            stock_code="600519",
            company_name="贵州茅台",
            industry="白酒",
            as_of_date="2021-06-30",
            depth="quick",
            strict_cutoff=True,
        )
        mock_results = [
            {
                "title": "观察日前公告",
                "url": "https://www.cninfo.com.cn/eligible",
                "snippet": "公司在观察日前披露经营信息。",
                "published_at": "2021-06-29",
                "site_name": "巨潮资讯",
            },
            {
                "title": "观察日后报道",
                "url": "https://www.stcn.com/future",
                "snippet": "该内容在观察日后发布。",
                "published_at": "2021-07-01",
                "site_name": "证券时报",
            },
            {
                "title": "无发布日期线索",
                "url": "https://example.com/undated",
                "snippet": "搜索结果没有可验证发布日期。",
                "published_at": "",
                "site_name": "示例站点",
            },
        ]

        with patch(
            "market_context_collector_scripts.run_market_context_collection.BochaWebSearchClient"
        ) as client_class:
            client_class.return_value.search.return_value = mock_results
            result = collect_market_context(request, project_root=self.project_root, force_refresh=True)

        package = json.loads(
            Path(result["generated_artifacts"]["market_context_package_json"]).read_text(encoding="utf-8")
        )
        sources = json.loads(
            Path(result["generated_artifacts"]["market_context_sources_json"]).read_text(encoding="utf-8")
        )
        audit = json.loads(
            Path(result["generated_artifacts"]["collection_audit_json"]).read_text(encoding="utf-8")
        )
        cutoff_audit = package["cutoff_audit"]

        self.assertEqual(result["status"], "partial_with_public_sources")
        self.assertEqual({item["cutoff_status"] for item in package["source_table"]}, {"eligible", "future", "undated"})
        self.assertEqual(len(package["claims"]), 1)
        self.assertEqual(package["claims"][0]["cutoff_status"], "eligible")
        self.assertEqual(cutoff_audit["accepted_source_count"], 1)
        self.assertEqual(cutoff_audit["future_excluded_count"], 1)
        self.assertEqual(cutoff_audit["undated_discovery_count"], 1)
        self.assertEqual(cutoff_audit["undated_fact_claim_count"], 0)
        self.assertTrue(cutoff_audit["cutoff_compliant"])
        self.assertEqual(package["quality_gate"]["accepted_source_count"], 1)
        self.assertEqual(package["quality_gate"]["source_tier_counts"]["S"], 1)
        self.assertEqual(sources["cutoff_audit"], cutoff_audit)
        self.assertEqual(audit["cutoff_audit"], cutoff_audit)
        self.assertTrue(all(call.kwargs["freshness"] is None for call in client_class.return_value.search.call_args_list))

    def test_strict_and_non_strict_cache_paths_are_isolated(self) -> None:
        """相同观察日和查询在严格与非严格策略下不得共享缓存路径。"""
        strict_request = MarketContextRequest(as_of_date="2021-06-30", strict_cutoff=True)
        non_strict_request = MarketContextRequest(as_of_date="2021-06-30", strict_cutoff=False)

        strict_path = query_cache_path(self.project_root, strict_request, "同一查询", None, 8)
        non_strict_path = query_cache_path(self.project_root, non_strict_request, "同一查询", None, 8)

        self.assertNotEqual(strict_path, non_strict_path)
        self.assertIn("strict_cutoff", strict_path.parts)
        self.assertIn("non_strict", non_strict_path.parts)

    def test_dry_run_writes_query_plan_only_package_without_api_key(self) -> None:
        """dry-run 不应依赖 API Key，并应写出可审计的查询计划产物。"""
        request = MarketContextRequest(
            target="600519",
            stock_code="600519",
            company_name="贵州茅台",
            industry="白酒",
            as_of_date="2026-07-08",
            depth="quick",
            focus="分红",
        )

        result = collect_market_context(request, project_root=self.project_root, dry_run=True)
        package_path = Path(result["generated_artifacts"]["market_context_package_json"])
        audit_path = Path(result["generated_artifacts"]["collection_audit_json"])

        self.assertEqual(result["status"], "query_plan_only")
        self.assertTrue(package_path.exists())
        self.assertTrue(audit_path.exists())
        package = json.loads(package_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(package["status"], "query_plan_only")
        self.assertEqual(package["usage_boundary"]["data_type"], "public_web_search_proxy")
        self.assertTrue(audit["dry_run"])
        self.assertGreater(audit["query_count"], 0)


if __name__ == "__main__":
    unittest.main()
