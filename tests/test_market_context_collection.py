"""市场上下文采集 v1 的单元测试。

测试只覆盖查询规划、Bocha 响应归一化和 dry-run 产物写入，不真实调用外部 Web Search，
从而避免把 API Key、网络波动或额度消耗引入自动化验证。
"""

from __future__ import annotations

from collections import Counter
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_context_collector_scripts.bocha_web_search_client import build_search_payload, normalize_bocha_response
from market_context_collector_scripts.market_context_query_planner import (
    DEPTH_QUERY_LIMITS,
    MarketContextRequest,
    build_query_plan,
)
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
        """standard 深度应在 20 条额度内覆盖六桶，并优先保障公司叙事和反方搜索。"""
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
        bucket_counts = Counter(item["bucket"] for item in plan)

        self.assertEqual(len(plan), 20)
        self.assertEqual(set(bucket_counts), {
            "market_hotspots",
            "target_narrative",
            "sector_context",
            "theme_mapping",
            "peer_context",
            "negative_signals",
        })
        self.assertGreaterEqual(bucket_counts["target_narrative"], 5)
        self.assertGreaterEqual(bucket_counts["negative_signals"], 5)
        self.assertTrue(any("贵州茅台" in item["query"] for item in plan))
        self.assertTrue(any("风险" in item["query"] or "利空" in item["query"] for item in plan))

    def test_depth_query_limits_keep_quick_and_deep_unchanged(self) -> None:
        """本次优化只收紧 standard，quick 12 与 deep 72 的公开额度保持不变。"""
        self.assertEqual(DEPTH_QUERY_LIMITS, {"quick": 12, "standard": 20, "deep": 72})

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

    def test_strict_cutoff_filters_model_facing_sources_and_preserves_full_audit(self) -> None:
        """严格截止只向模型产物暴露合格来源，同时让审计和原始结果保留完整发现记录。"""
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

        package_path = Path(result["generated_artifacts"]["market_context_package_json"])
        markdown_path = Path(result["generated_artifacts"]["market_context_package_md"])
        sources_path = Path(result["generated_artifacts"]["market_context_sources_json"])
        audit_path = Path(result["generated_artifacts"]["collection_audit_json"])
        raw_path = Path(result["generated_artifacts"]["raw_search_results_json"])
        package = json.loads(package_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        cutoff_audit = package["cutoff_audit"]

        self.assertEqual(result["status"], "partial_with_public_sources")
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(len(package["source_table"]), 1)
        self.assertEqual({item["cutoff_status"] for item in package["source_table"]}, {"eligible"})
        self.assertTrue(all(item["eligible_for_claim"] for item in package["source_table"]))
        self.assertEqual(sources["sources"], package["source_table"])
        self.assertEqual(len(package["claims"]), 1)
        self.assertEqual(package["claims"][0]["cutoff_status"], "eligible")
        self.assertIn("观察日前公告", markdown)
        self.assertNotIn("观察日后报道", markdown)
        self.assertNotIn("无发布日期线索", markdown)

        self.assertEqual(cutoff_audit["total_source_count"], 3)
        self.assertEqual(cutoff_audit["accepted_source_count"], 1)
        self.assertEqual(cutoff_audit["future_excluded_count"], 1)
        self.assertEqual(cutoff_audit["undated_discovery_count"], 1)
        self.assertEqual(cutoff_audit["undated_fact_claim_count"], 0)
        self.assertTrue(cutoff_audit["cutoff_compliant"])
        self.assertEqual(package["quality_gate"]["accepted_source_count"], 1)
        self.assertEqual(package["quality_gate"]["source_tier_counts"]["S"], 1)
        self.assertEqual(sources["cutoff_audit"], cutoff_audit)
        self.assertEqual(audit["cutoff_audit"], cutoff_audit)
        self.assertEqual(audit["source_count"], 1)
        self.assertEqual(audit["total_discovered_source_count"], 3)
        self.assertEqual(audit["eligible_source_count"], 1)
        self.assertEqual(audit["excluded_source_count"], 2)
        self.assertEqual({item["cutoff_status"] for item in audit["excluded_source_index"]}, {"future", "undated"})
        self.assertTrue(
            all(
                "title" not in item and "snippet" not in item and "query" not in item
                for item in audit["excluded_source_index"]
            )
        )
        self.assertEqual(audit["query_telemetry"]["cache_query_count"], 0)
        self.assertEqual(audit["query_telemetry"]["live_query_count"], 12)
        self.assertEqual(audit["query_telemetry"]["successful_query_count"], 12)
        self.assertEqual(audit["query_telemetry"]["empty_query_count"], 0)
        self.assertEqual(len(raw["raw_results"]), 12)
        self.assertTrue(all(len(group["results"]) == 3 for group in raw["raw_results"]))
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

    def test_collection_audit_reports_live_cache_success_and_empty_query_telemetry(self) -> None:
        """采集审计应区分实时请求、缓存命中、成功调用和成功但无结果的查询。"""
        request = MarketContextRequest(
            target="600519",
            stock_code="600519",
            company_name="贵州茅台",
            industry="白酒",
            as_of_date="2026-07-08",
            depth="quick",
        )
        one_result = {
            "title": "市场上下文线索",
            "url": "https://www.cninfo.com.cn/context",
            "snippet": "用于验证查询遥测的公开来源。",
            "published_at": "2026-07-01",
            "site_name": "巨潮资讯",
        }

        with patch(
            "market_context_collector_scripts.run_market_context_collection.BochaWebSearchClient"
        ) as client_class:
            client_class.return_value.search.side_effect = [[]] + [[one_result]] * 11
            first_result = collect_market_context(request, project_root=self.project_root)
            first_audit = json.loads(
                Path(first_result["generated_artifacts"]["collection_audit_json"]).read_text(encoding="utf-8")
            )

            client_class.return_value.search.reset_mock()
            second_result = collect_market_context(request, project_root=self.project_root)
            second_audit = json.loads(
                Path(second_result["generated_artifacts"]["collection_audit_json"]).read_text(encoding="utf-8")
            )

        self.assertEqual(first_audit["query_telemetry"]["cache_query_count"], 0)
        self.assertEqual(first_audit["query_telemetry"]["live_query_count"], 12)
        self.assertEqual(first_audit["query_telemetry"]["successful_query_count"], 12)
        self.assertEqual(first_audit["query_telemetry"]["empty_query_count"], 1)
        self.assertEqual(second_audit["query_telemetry"]["cache_query_count"], 12)
        self.assertEqual(second_audit["query_telemetry"]["live_query_count"], 0)
        self.assertEqual(second_audit["query_telemetry"]["successful_query_count"], 12)
        self.assertEqual(second_audit["query_telemetry"]["empty_query_count"], 1)
        client_class.return_value.search.assert_not_called()

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
