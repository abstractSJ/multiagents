"""近期财报集合策略的单元测试。"""

from __future__ import annotations

import unittest

from research_orchestrator_scripts.recent_filing_policy import (
    build_filing_identity,
    calculate_financial_input_fingerprint,
    derive_recent_filing_plan,
    normalize_filing_policy,
)


class RecentFilingPolicyTest(unittest.TestCase):
    """验证近期财报候选窗口、默认模式与输入指纹。"""

    def test_june_plan_includes_q1_but_excludes_future_interims(self) -> None:
        """六月只能查询当年一季报，不能把尚未开始的半年报/Q3伪装成可用候选。"""
        plan = derive_recent_filing_plan("2026-06-15", annual_lookback=2)
        keys = {(item.report_type, item.report_year) for item in plan}

        self.assertIn(("annual", "2025"), keys)
        self.assertIn(("annual", "2024"), keys)
        self.assertIn(("annual", "2023"), keys)
        self.assertIn(("q1", "2026"), keys)
        self.assertNotIn(("semiannual", "2026"), keys)
        self.assertNotIn(("q3", "2026"), keys)
        self.assertIn(("q1", "2025"), keys)
        self.assertIn(("semiannual", "2025"), keys)
        self.assertIn(("q3", "2025"), keys)

    def test_july_h1_is_discovery_only_until_normal_deadline(self) -> None:
        """七月已开始半年报窗口，但未到八月底常规截止日时，缺失半年报不应阻塞。"""
        plan = derive_recent_filing_plan("2026-07-19", annual_lookback=2)
        h1 = next(item for item in plan if item.report_type == "semiannual" and item.report_year == "2026")
        self.assertFalse(h1.expected_by_cutoff)

    def test_late_year_plan_includes_all_current_interims(self) -> None:
        """十月后当年 q1、半年报和 q3 都进入候选，但最终可用性仍由 manifest 决定。"""
        plan = derive_recent_filing_plan("2026-11-15", annual_lookback=2)
        keys = {(item.report_type, item.report_year) for item in plan}
        for report_type in ("q1", "semiannual", "q3"):
            self.assertIn((report_type, "2026"), keys)

    def test_early_year_skips_unopened_current_interims(self) -> None:
        """一季度披露窗口尚未开始时，计划不得生成未来查询窗口。"""
        plan = derive_recent_filing_plan("2026-02-15", annual_lookback=2)
        keys = {(item.report_type, item.report_year) for item in plan}
        self.assertNotIn(("q1", "2026"), keys)
        self.assertNotIn(("semiannual", "2026"), keys)
        self.assertNotIn(("q3", "2026"), keys)
        self.assertIn(("q3", "2025"), keys)

    def test_explicit_type_and_year_preserve_single_filing_mode(self) -> None:
        """同时固定类型和财年时保持旧的单份财报语义。"""
        self.assertEqual(
            normalize_filing_policy("", report_type="annual", report_year="2025"),
            "single_filing",
        )
        self.assertEqual(normalize_filing_policy("", report_type="annual", report_year=""), "single_filing")

    def test_fingerprint_is_order_independent_but_identity_sensitive(self) -> None:
        """同一财报集合顺序变化不应失效，版本或 PDF 身份变化必须失效。"""
        annual = build_filing_identity(
            {
                "stock_code": "600519",
                "report_type": "annual",
                "report_year": "2025",
                "announcement_id": "a1",
                "published_at": "2026-03-30",
                "local_relative_path": "annual.pdf",
            },
            pdf_sha256="hash-a",
        )
        q1 = build_filing_identity(
            {
                "stock_code": "600519",
                "report_type": "q1",
                "report_year": "2026",
                "announcement_id": "q1",
                "published_at": "2026-04-29",
                "local_relative_path": "q1.pdf",
            },
            pdf_sha256="hash-q1",
        )
        first = calculate_financial_input_fingerprint([annual, q1])
        second = calculate_financial_input_fingerprint([q1, annual])
        revised = dict(q1, announcement_id="q1-revised")
        revised_pdf = dict(q1, pdf_sha256="hash-q1-revised")

        self.assertEqual(first, second)
        self.assertNotEqual(first, calculate_financial_input_fingerprint([annual, revised]))
        self.assertNotEqual(first, calculate_financial_input_fingerprint([annual, revised_pdf]))


if __name__ == "__main__":
    unittest.main()
