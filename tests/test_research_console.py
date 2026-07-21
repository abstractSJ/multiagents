"""research_console 后端单元测试。

测试范围（零网络、零子进程）：
- 计划构建：ready 层映射 skipped、缺口层映射 pending、force_refresh 全 pending；
- 披露窗口推导：四种报告类型的窗口起止与"end 不晚于今天"约束；
- artifact 路径守卫：白名单内放行、白名单外（含项目根其他文件）拒绝；
- demo 事件脚本：seq 单调、首尾事件类型、关键事件类型覆盖；
- 估值 summary 宽容提取：中英文字段变体下的三档估值与现价。

计划构建用例依赖仓库内真实样例 research_state；样例缺失时 skipTest 而非失败。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from industry_info_collector_scripts import run_industry_collection
from research_console import app as console_app
from research_console import config, engine, history, state_reader, steps
from research_orchestrator_scripts.audit_company_research_state import default_state_output_path

# 真实样例状态文件：600519 贵州茅台 2025 年报。
SAMPLE_STATE_PATH = (
    config.PROJECT_ROOT
    / "research_orchestrator_scripts"
    / "orchestrator_workspace"
    / "company_state"
    / "600519"
    / "2025"
    / "research_state.json"
)


def _load_sample_state() -> dict | None:
    """读取真实样例 research_state。

    参数：
        无。
    返回值：
        状态字典；文件缺失或损坏返回 None。
    """
    if not SAMPLE_STATE_PATH.exists():
        return None
    try:
        return json.loads(SAMPLE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _minimal_state(
    *,
    valuation: str = "missing",
    formal: str = "partial",
    collector: str = "ready",
    market_context: str = "ready",
    artifact_path: str = "",
    generated_at: str = "2026-07-14T00:00:00+08:00",
) -> dict:
    """构造 coordinator/observer 测试所需的最小六层 research_state。"""
    statuses = {
        "collector": collector,
        "processor": "ready",
        "financial_evidence_draft": "ready",
        "formal_financial_analysis": formal,
        "valuation": valuation,
        "market_context": market_context,
    }
    layers = {name: {"status": status, "artifacts": {}} for name, status in statuses.items()}
    if artifact_path:
        layers["valuation"]["artifacts"]["valuation_report_json"] = {
            "exists": True,
            "path": artifact_path,
        }
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "target": {"stock_code": "600519", "company_name": "贵州茅台", "report_year": "2025"},
        "request": {"target": "600519", "report_year": "2025"},
        "layers": layers,
        "reusable": {name: status == "ready" for name, status in statuses.items()},
        "next_actions": [] if valuation == "ready" and formal == "ready" else [{"owner": "valuation-analyst"}],
        "summary": {"layer_statuses": statuses},
    }


class _FakeProcess:
    """为 stream-json reader 提供可控 stdout/stderr 的最小异步进程替身。"""

    def __init__(self, stdout_lines: list[str], stderr_lines: list[str] | None = None):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for line in stdout_lines:
            self.stdout.feed_data((line + "\n").encode("utf-8"))
        self.stdout.feed_eof()
        for line in stderr_lines or []:
            self.stderr.feed_data((line + "\n").encode("utf-8"))
        self.stderr.feed_eof()
        self.returncode = None
        self.pid = 43210

    async def wait(self) -> int:
        """模拟正常进程退出。"""
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        """模拟 terminate。"""
        self.returncode = -15

    def kill(self) -> None:
        """模拟 kill。"""
        self.returncode = -9


class PlanBuildingTest(unittest.TestCase):
    """验证计划构建对复用/缺口/强制刷新的映射语义。"""

    def setUp(self) -> None:
        """加载真实样例状态；缺失时跳过本组用例。"""
        self.state = _load_sample_state()
        if self.state is None:
            self.skipTest(f"样例 research_state 不存在: {SAMPLE_STATE_PATH}")

    def _plan_map(self, **kwargs) -> dict[str, dict]:
        """构建计划并转成 step_id 索引。"""
        plan = steps.build_company_plan(self.state, **kwargs)
        return {item["step_id"]: item for item in plan}

    def test_ready_layers_map_to_skipped(self) -> None:
        """reusable=true 的层（采集/处理/草稿）应映射为 skipped。"""
        plan = self._plan_map()
        reusable = self.state["reusable"]
        self.assertTrue(reusable["collector"])
        self.assertEqual(plan["collector_fetch"]["status"], "skipped")
        self.assertEqual(plan["collector_fetch"]["skip_reason"], steps.SKIP_REASON_REUSE)
        self.assertTrue(reusable["processor"])
        for step_id in ("processor_parse", "processor_digest", "processor_rag", "processor_compare"):
            self.assertEqual(plan[step_id]["status"], "skipped", step_id)
        self.assertTrue(reusable["financial_evidence_draft"])
        self.assertEqual(plan["financial_evidence_draft"]["status"], "skipped")

    def test_partial_and_missing_layers_map_to_pending(self) -> None:
        """显式构造 formal partial、valuation missing，避免真实样例被运行刷新后漂移。"""
        state = json.loads(json.dumps(self.state, ensure_ascii=False))
        state["layers"]["formal_financial_analysis"]["status"] = "partial"
        state["layers"]["valuation"]["status"] = "missing"
        state["reusable"]["formal_financial_analysis"] = False
        state["reusable"]["valuation"] = False
        plan = {item["step_id"]: item for item in steps.build_company_plan(state)}
        self.assertEqual(plan["formal_financial_analysis"]["status"], "pending")
        self.assertEqual(plan["valuation_update"]["status"], "pending")

    def test_market_context_ready_maps_to_skipped(self) -> None:
        """market_context 层 ready 时对应步骤应为 skipped。"""
        # 真实样例会随 as_of_date 审计从 ready 变 stale，测试显式构造目标分支，
        # 避免端到端运行刷新状态后让计划单测产生时间耦合。
        state = json.loads(json.dumps(self.state, ensure_ascii=False))
        state["layers"]["market_context"]["status"] = "ready"
        state["reusable"]["market_context"] = True
        plan = {item["step_id"]: item for item in steps.build_company_plan(state)}
        self.assertEqual(plan["market_context_update"]["status"], "skipped")

    def test_force_refresh_makes_all_pending(self) -> None:
        """force_refresh=true 时全部步骤 pending。"""
        plan = steps.build_company_plan(self.state, force_refresh=True)
        for item in plan:
            self.assertEqual(item["status"], "pending", item["step_id"])

    def test_orchestrator_steps_always_pending(self) -> None:
        """audit/final_audit/deliver 属于编排器步骤，始终 pending。"""
        plan = self._plan_map()
        for step_id in ("audit", "final_audit", "deliver"):
            self.assertEqual(plan[step_id]["status"], "pending", step_id)


class DisclosureWindowTest(unittest.TestCase):
    """验证披露窗口推导公式与今天上限约束。"""

    TODAY = _dt.date(2026, 7, 13)

    def test_annual_2025(self) -> None:
        """annual FY2025 → 2026-01-01 起，end 收拢到今天。"""
        start, end = steps.derive_disclosure_window("annual", "2025", today=self.TODAY)
        self.assertEqual(start, "2026-01-01")
        self.assertEqual(end, "2026-07-13")

    def test_q1_2026(self) -> None:
        """q1 2026 → 2026-04-01 起，end 不晚于今天。"""
        start, end = steps.derive_disclosure_window("q1", "2026", today=self.TODAY)
        self.assertEqual(start, "2026-04-01")
        self.assertEqual(end, "2026-07-13")

    def test_semiannual_2025(self) -> None:
        """semiannual 2025 → 2025-07-01 .. 2025-12-31。"""
        start, end = steps.derive_disclosure_window("semiannual", "2025", today=self.TODAY)
        self.assertEqual(start, "2025-07-01")
        self.assertEqual(end, "2025-12-31")

    def test_q3_2025(self) -> None:
        """q3 2025 → 2025-10-01 .. 2025-12-31。"""
        start, end = steps.derive_disclosure_window("q3", "2025", today=self.TODAY)
        self.assertEqual(start, "2025-10-01")
        self.assertEqual(end, "2025-12-31")

    def test_end_never_after_today(self) -> None:
        """任意组合下 end 都不晚于今天。"""
        for rtype in ("annual", "q1", "semiannual", "q3"):
            for year in ("2024", "2025", "2026"):
                _, end = steps.derive_disclosure_window(rtype, year, today=self.TODAY)
                self.assertLessEqual(end, self.TODAY.isoformat(), f"{rtype}/{year}")

    def test_explicit_cutoff_caps_window_and_collapses_future_start(self) -> None:
        """显式历史截止日必须压低 end；截止日在自然窗口前时 start/end 同日收拢。"""
        start, end = steps.derive_disclosure_window(
            "annual", "2025", today=self.TODAY, cutoff="2026-03-31"
        )
        self.assertEqual((start, end), ("2026-01-01", "2026-03-31"))
        future_start, future_end = steps.derive_disclosure_window(
            "annual", "2025", today=self.TODAY, cutoff="2025-12-15"
        )
        self.assertEqual((future_start, future_end), ("2025-12-15", "2025-12-15"))

    def test_collector_command_uses_cutoff_without_breaking_old_call(self) -> None:
        """命令构建器应透传截止窗口，同时保留 today 位置参数的旧调用方式。"""
        old_cmd = steps.build_collector_cmd("600519", "annual", "2025", self.TODAY)
        cutoff_cmd = steps.build_collector_cmd(
            "600519", "annual", "2025", self.TODAY, cutoff="2026-03-31"
        )
        self.assertEqual(old_cmd[old_cmd.index("--end-date") + 1], "2026-07-13")
        self.assertEqual(cutoff_cmd[cutoff_cmd.index("--end-date") + 1], "2026-03-31")

    def test_recent_collector_commands_include_june_q1_and_exclude_future_h1(self) -> None:
        """近期历史批量命令在六月应包含当年 Q1，但不能查询尚未开始的当年半年报。"""
        commands = steps.build_recent_collector_cmds("600519", "2026-06-15", annual_lookback=2)
        keys = {(item["report_type"], item["report_year"]) for item in commands}
        self.assertIn(("q1", "2026"), keys)
        self.assertNotIn(("semiannual", "2026"), keys)
        self.assertIn(("annual", "2025"), keys)
        for item in commands:
            self.assertEqual(item["cmd"][item["cmd"].index("--keyword") + 1], "600519")
            self.assertLessEqual(item["disclosure_end"], "2026-06-15")

    def test_processor_parse_command_can_pin_announcement(self) -> None:
        """多版本财报处理必须把选中公告 ID 传给解析器，避免处理摘要或旧修订版。"""
        cmd = steps.build_processor_parse_cmd("600519", "q1", "2026", announcement_id="abc123")
        self.assertEqual(cmd[cmd.index("--announcement-id") + 1], "abc123")
        pdf_cmd = steps.build_processor_parse_cmd("600519", "q1", "2026", pdf_path="D:/reports/q1.pdf")
        self.assertEqual(pdf_cmd[pdf_cmd.index("--pdf") + 1], "D:/reports/q1.pdf")
        self.assertNotIn("--stock-code", pdf_cmd)

    def test_invalid_cutoff_is_rejected(self) -> None:
        """披露窗口 cutoff 也使用严格十位 ISO 日期。"""
        for invalid in ("20260331", "2026-3-31", "2026-02-30"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                steps.derive_disclosure_window("annual", "2025", today=self.TODAY, cutoff=invalid)


class ArtifactGuardTest(unittest.TestCase):
    """验证 artifact 路径白名单守卫。"""

    def test_absolute_path_inside_whitelist_allowed(self) -> None:
        """白名单工作区内的绝对路径放行。"""
        path = config.COLLECTOR_WORKSPACE / "manifests" / "cninfo_all_reports.json"
        self.assertTrue(state_reader.is_path_allowed(str(path)))
        path2 = config.ORCHESTRATOR_WORKSPACE / "company_state" / "600519" / "2025" / "research_state.json"
        self.assertTrue(state_reader.is_path_allowed(str(path2)))

    def test_relative_path_inside_whitelist_allowed(self) -> None:
        """白名单内的相对路径（按项目根解析）放行。"""
        self.assertTrue(
            state_reader.is_path_allowed("info_collector_scripts/collector_workspace/manifests/cninfo_all_reports.json")
        )
        self.assertTrue(state_reader.is_path_allowed("research_console/console_workspace/runs"))

    def test_system_path_denied(self) -> None:
        """系统敏感路径拒绝。"""
        self.assertFalse(state_reader.is_path_allowed(r"C:\Windows\system32\drivers\etc\hosts"))

    def test_project_root_files_denied(self) -> None:
        """项目根本身（白名单之外）的文件拒绝，例如 CLAUDE.md。"""
        self.assertFalse(state_reader.is_path_allowed(str(config.PROJECT_ROOT / "CLAUDE.md")))
        self.assertFalse(state_reader.is_path_allowed("CLAUDE.md"))

    def test_escape_via_dotdot_denied(self) -> None:
        """借 .. 逃出白名单的路径拒绝。"""
        sneaky = str(config.COLLECTOR_WORKSPACE / ".." / ".." / "CLAUDE.md")
        self.assertFalse(state_reader.is_path_allowed(sneaky))

    def test_market_context_local_config_is_explicitly_denied(self) -> None:
        """即使位于白名单工作区，含 Bocha 凭据的 local_config.json 也必须拒绝。"""
        self.assertFalse(state_reader.is_path_allowed(config.MARKET_CONTEXT_LOCAL_CONFIG))
        with self.assertRaises(PermissionError):
            state_reader.read_artifact(str(config.MARKET_CONTEXT_LOCAL_CONFIG))


class AuditOutputPathTest(unittest.TestCase):
    """验证 research_state 默认输出始终受 company_state 根约束。"""

    def test_untrusted_components_cannot_escape_company_state(self) -> None:
        """target/report_year 中的绝对路径与 .. 必须被规整为普通目录名。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {
                "target": {"stock_code": "", "report_year": ""},
                "request": {"target": "../../escape", "report_year": "D:/outside"},
            }
            output = default_state_output_path(root, state)
            base = (root / "research_orchestrator_scripts" / "orchestrator_workspace" / "company_state").resolve()
            self.assertTrue(output.is_relative_to(base))
            self.assertNotIn("..", output.relative_to(base).parts)

    def test_state_reader_uses_writer_path_sanitization(self) -> None:
        """英文名称含空格时，事件路径必须与 audit 实际写入路径完全一致。"""
        state = {
            "target": {"stock_code": "", "report_year": ""},
            "request": {"target": "bank of chine", "report_year": ""},
        }
        expected = default_state_output_path(config.PROJECT_ROOT, state)
        self.assertEqual(state_reader.state_file_path(state), expected)
        self.assertIn("bank_of_chine", expected.parts)
        self.assertNotIn("bank of chine", expected.parts)


class IndustryCliValidationTest(unittest.TestCase):
    """验证纯行业与公司锚点两种 CLI 输入 Gate。"""

    def test_target_only_investment_research_is_allowed(self) -> None:
        """无锚点的买方行业研究应由行业证据优先链路正常接收。"""
        argv = [
            "run_industry_collection.py",
            "--target",
            "工业气体",
            "--as-of-date",
            "2026-07-14",
            "--deliverable-type",
            "investment_research",
        ]
        with patch.object(sys, "argv", argv):
            args = run_industry_collection.parse_args()
        self.assertEqual(args.target, "工业气体")
        self.assertEqual(args.deliverable_type, "investment_research")

    def test_partial_anchor_is_rejected(self) -> None:
        """一旦进入公司验证模式，代码、名称与财年必须成套提供。"""
        argv = ["run_industry_collection.py", "--stock-code", "000001", "--as-of-date", "2026-07-14"]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            run_industry_collection.parse_args()


class ApiValidationTest(unittest.IsolatedAsyncioTestCase):
    """验证程序化调用不能绕过浏览器表单的最小目标参数 Gate。"""

    async def test_empty_company_run_is_rejected_before_background_task(self) -> None:
        """空 company 请求必须返回 400，且不能真的创建协调器 run。"""
        create_run = AsyncMock()
        request = console_app.RunRequest(mode="company", params={})
        with patch.object(console_app.ENGINE, "create_run_checked", new=create_run):
            response = await console_app.create_run(request)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("params.target", payload["error"])
        create_run.assert_not_awaited()

    async def test_company_run_defaults_missing_as_of_date_to_today(self) -> None:
        """company API 缺失 as_of_date 时应在创建 run 前补成本地今天。"""
        created = type("CreatedRun", (), {"run_id": "r_today"})()
        create_run = AsyncMock(return_value=created)
        request = console_app.RunRequest(mode="company", params={"target": "600519"})
        with patch.object(console_app.ENGINE, "create_run_checked", new=create_run):
            response = await console_app.create_run(request)
        self.assertEqual(response.status_code, 200)
        submitted_params = create_run.await_args.args[1]
        self.assertEqual(submitted_params["as_of_date"], _dt.date.today().isoformat())

    async def test_company_run_rejects_invalid_or_future_as_of_date(self) -> None:
        """非法或未来知识截止日必须返回 400，且不能创建后台 run。"""
        future = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        for value in ("2026/07/15", future):
            with self.subTest(as_of_date=value):
                create_run = AsyncMock()
                request = console_app.RunRequest(
                    mode="company",
                    params={"target": "600519", "as_of_date": value},
                )
                with patch.object(console_app.ENGINE, "create_run_checked", new=create_run):
                    response = await console_app.create_run(request)
                self.assertEqual(response.status_code, 400)
                create_run.assert_not_awaited()

    async def test_industry_partial_anchor_is_rejected_by_api(self) -> None:
        """industry 公司验证参数缺字段时应在启动脚本前给出明确错误。"""
        create_run = AsyncMock()
        request = console_app.RunRequest(mode="industry", params={"stock_code": "600519"})
        with patch.object(console_app.ENGINE, "create_run_checked", new=create_run):
            response = await console_app.create_run(request)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("requires stock_code, company_name, and fiscal_year/report_year together", payload["error"])
        create_run.assert_not_awaited()


class DemoTimelineTest(unittest.TestCase):
    """验证 demo 脚本化事件序列的结构约束。"""

    def setUp(self) -> None:
        """生成一份 demo 时间轴。"""
        self.timeline = engine.build_demo_timeline("r_demo_test")
        self.events = [event for _, event in self.timeline]

    def test_seq_strictly_increasing(self) -> None:
        """seq 必须从 1 开始严格递增。"""
        seqs = [event["seq"] for event in self.events]
        self.assertEqual(seqs[0], 1)
        for prev, curr in zip(seqs, seqs[1:]):
            self.assertEqual(curr, prev + 1)

    def test_first_and_last_events(self) -> None:
        """首事件 run_started，末事件 run_completed(status=completed)。"""
        self.assertEqual(self.events[0]["type"], "run_started")
        self.assertEqual(self.events[-1]["type"], "run_completed")
        self.assertEqual(self.events[-1]["payload"]["status"], "completed")

    def test_required_event_types_present(self) -> None:
        """至少包含一次 step_progress / backflow / step_waiting_llm。"""
        types = {event["type"] for event in self.events}
        for required in (
            "step_progress",
            "backflow",
            "step_waiting_llm",
            "plan_ready",
            "state_refreshed",
            "work_item_upsert",
            "agent_started",
            "tool_activity",
            "handoff",
        ):
            self.assertIn(required, types)

    def test_plan_contains_two_skipped_steps(self) -> None:
        """demo 计划应含 2 个 skipped 步骤。"""
        plan_events = [event for event in self.events if event["type"] == "plan_ready"]
        self.assertEqual(len(plan_events), 1)
        skipped = [item for item in plan_events[0]["payload"]["steps"] if item["status"] == "skipped"]
        self.assertEqual(len(skipped), 2)
        self.assertEqual(plan_events[0]["payload"]["trace_mode"], "runtime")
        self.assertIn("milestone_states", plan_events[0]["payload"])

    def test_state_refreshes_replace_stale_next_actions(self) -> None:
        """状态刷新必须携带 next_actions，避免计划初始缺口在完成后残留。"""
        refreshed = [event for event in self.events if event["type"] == "state_refreshed"]
        self.assertTrue(refreshed)
        self.assertTrue(all("next_actions" in event["payload"] for event in refreshed))
        self.assertEqual(refreshed[-1]["payload"]["next_actions"], [])

    def test_demo_has_explicit_final_delivery_before_terminal(self) -> None:
        """runtime 模式必须显式画出调度台到交付台的最终报告链路。"""
        self.assertEqual(self.events[-2]["type"], "handoff")
        self.assertEqual(self.events[-2]["payload"]["kind"], "final_delivery")
        self.assertEqual(self.events[-2]["payload"]["to_station"], "deliver")

    def test_demo_summary_marked_as_demo(self) -> None:
        """demo 结论卡的一句话结论必须标注"演示数据"。"""
        summary = self.events[-1]["payload"]["summary"]
        self.assertIn("Demo data", summary["one_line_conclusion"])
        self.assertIn("fair_value", summary)
        self.assertEqual(summary["stock_code"], "600519")


class ValuationSummaryExtractionTest(unittest.TestCase):
    """验证估值报告的宽容字段提取。"""

    def test_chinese_scenario_keys_and_variant_price(self) -> None:
        """中文情景键 + intraday_price_cny 变体现价应正确提取。"""
        report = {
            "valuation_view": "undervalued",
            "status": "completed",
            "market_snapshot": {"intraday_price_cny": 36.83, "total_market_cap_cny": 928846913484.83},
            "fair_value_range_per_share": {
                "悲观": {"fair_value_cny": 30.0},
                "基准": 41.5,
                "乐观": {"range_cny": [48.0, 52.0]},
            },
            "key_assumptions": ["假设A", "假设B"],
            "valuation_falsifiers": ["证伪1"],
        }
        summary = state_reader.extract_valuation_summary(report)
        self.assertEqual(summary["current_price"], 36.83)
        self.assertEqual(summary["market_cap"], 928846913484.83)
        self.assertEqual(summary["fair_value"]["bear"], 30.0)
        self.assertEqual(summary["fair_value"]["base"], 41.5)
        self.assertEqual(summary["fair_value"]["bull"], 50.0)
        self.assertEqual(summary["valuation_view"], "undervalued")
        self.assertNotEqual(summary["price_source"], "missing")
        # 缺 upside 字段时按合理价值与现价反推。
        self.assertAlmostEqual(summary["upside_downside"]["bear"], 30.0 / 36.83 - 1.0, places=3)
        self.assertAlmostEqual(summary["upside_downside"]["base"], 41.5 / 36.83 - 1.0, places=3)

    def test_english_flat_keys(self) -> None:
        """英文扁平键 + current_price 应正确提取。"""
        report = {
            "market_snapshot": {"current_price": 11.0},
            "fair_value_range_per_share": {"bear": 10, "base": 12, "bull": 15},
            "upside_downside_vs_current_price": {"bear": -9.1, "base": 9.1, "bull": 36.4},
        }
        summary = state_reader.extract_valuation_summary(report)
        self.assertEqual(summary["current_price"], 11.0)
        self.assertEqual(summary["fair_value"]["bear"], 10.0)
        self.assertEqual(summary["fair_value"]["base"], 12.0)
        self.assertEqual(summary["fair_value"]["bull"], 15.0)
        # 绝对值大于 1.5 的数字按百分数处理（-9.1 → -0.091）。
        self.assertAlmostEqual(summary["upside_downside"]["bear"], -0.091, places=4)
        self.assertAlmostEqual(summary["upside_downside"]["bull"], 0.364, places=4)

    def test_nested_percent_upside_variant(self) -> None:
        """真实报告的嵌套 upside 形态（vs_intraday_x → bear_point_percent）应可下钻。"""
        report = {
            "market_snapshot": {"reference_close_2026_05_26_cny": 37.0},
            "fair_value_range_per_share": {"bear": 32.5, "base": 41.5, "bull": 48.5},
            "upside_downside_vs_current_price": {
                "vs_intraday_36_83": {
                    "bear_point_percent": -11.8,
                    "base_point_percent": 12.7,
                    "bull_point_percent": 31.7,
                }
            },
        }
        summary = state_reader.extract_valuation_summary(report)
        self.assertEqual(summary["current_price"], 37.0)
        self.assertAlmostEqual(summary["upside_downside"]["bear"], -0.118, places=4)
        self.assertAlmostEqual(summary["upside_downside"]["base"], 0.127, places=4)
        self.assertAlmostEqual(summary["upside_downside"]["bull"], 0.317, places=4)

    def test_explicit_small_percent_is_always_divided_by_one_hundred(self) -> None:
        """*_percent=1.2 表示 1.2%，不能被解释成 120%。"""
        summary = state_reader.extract_valuation_summary(
            {
                "upside_downside_vs_current_price": {
                    "vs_reference": {"base_point_percent": 1.2}
                }
            }
        )
        self.assertAlmostEqual(summary["upside_downside"]["base"], 0.012, places=6)

    def test_object_assumptions_and_long_falsifier_alias_are_flattened(self) -> None:
        """对象型假设与新版证伪/调整触发字段应保留关键信息并合并去重。"""
        report = {
            "key_assumptions": {
                "收入增速": "未来两年保持 8%-10%",
                "margin": {"description": "毛利率维持稳定"},
            },
            "valuation_falsifiers": ["库存显著恶化"],
            "valuation_falsifiers_and_revision_triggers": [
                {"condition": "库存显著恶化", "revision_action": "下修目标价 15%"},
                {"trigger": "分红率低于 50%", "revision_action": "提高风险折价"},
            ],
        }
        summary = state_reader.extract_valuation_summary(report)
        self.assertTrue(any("收入增速" in item for item in summary["key_assumptions"]))
        self.assertTrue(any("毛利率" in item for item in summary["key_assumptions"]))
        self.assertTrue(any("库存" in item for item in summary["valuation_falsifiers"]))
        self.assertTrue(any("下修" in item for item in summary["valuation_falsifiers"]))
        self.assertTrue(any("分红率" in item for item in summary["valuation_falsifiers"]))

    def test_missing_fields_yield_none_not_error(self) -> None:
        """字段全缺时返回 None/空而不是抛异常。"""
        summary = state_reader.extract_valuation_summary({})
        self.assertIsNone(summary["current_price"])
        self.assertEqual(summary["price_source"], "missing")
        self.assertIsNone(summary["fair_value"]["bear"])
        self.assertEqual(summary["valuation_view"], "unknown")

    def test_python_agent_valuation_schema_is_console_extractable(self) -> None:
        """python_agent_coordinator 新估值字段应进入结论卡，不能再 View Undetermined。"""
        report = {
            "confidence": "low",
            "price_context": {
                "current_price": 121.09,
                "price_source": "public_web_search_proxy",
                "market_cap_proxy_cny": 147800000000,
            },
            "executive_summary": {
                "thesis": "Base fair value above proxy price with low confidence.",
                "base_target_price": 175.0,
                "upside_downside_vs_current_price": {
                    "bear_pct": -13.3,
                    "base_pct": 44.5,
                    "bull_pct": 114.7,
                },
            },
            "fair_value_scenarios": {
                "bear": {"fair_value_per_share": 105.0},
                "base": {"fair_value_per_share": 175.0},
                "bull": {"fair_value_per_share": 260.0},
            },
            "base_target_price": {"value": 175.0, "unit": "CNY_per_share"},
            "key_assumptions": ["gm mid-70s", "q1 seasonality haircut"],
        }
        summary = state_reader.extract_valuation_summary(report, as_of_date="2026-07-21")
        self.assertEqual(summary["current_price"], 121.09)
        self.assertEqual(summary["price_source"], "public_web_search_proxy")
        self.assertEqual(summary["fair_value"]["bear"], 105.0)
        self.assertEqual(summary["fair_value"]["base"], 175.0)
        self.assertEqual(summary["fair_value"]["bull"], 260.0)
        self.assertEqual(summary["valuation_view"], "undervalued")
        self.assertIn("Base fair value", summary["one_line_conclusion"])
        self.assertAlmostEqual(summary["upside_downside"]["base"], 0.445, places=3)
        self.assertEqual(summary["confidence"], "low")


class DecisionHistoryTest(unittest.TestCase):
    """验证公司历史决策冻结、本地行情回看与损坏记录降级。"""

    @staticmethod
    def _events(summary: dict) -> list[dict]:
        """构造带唯一完成事件的历史 run。"""
        return [
            {"seq": 1, "ts": "2026-07-10T18:00:00+08:00", "type": "run_started", "payload": {}},
            {
                "seq": 2,
                "ts": "2026-07-10T18:30:00+08:00",
                "type": "run_completed",
                "payload": {"status": "completed", "summary": summary},
            },
        ]

    @staticmethod
    def _summary(source_path: str = "") -> dict:
        """构造可冻结的最小公司结论。"""
        return {
            "company_name": "样例公司",
            "stock_code": "000001",
            "report_year": "2025",
            "as_of_date": "2026-07-10",
            "valuation_view": "fair",
            "valuation_view_raw": "fairly_valued",
            "one_line_conclusion": "当时结论",
            "current_price": 10.0,
            "price_source": "fixture",
            "price_observation": {
                "status": "available",
                "observation_date": "2026-07-10",
                "price": 10.0,
                "source": "fixture",
            },
            "fair_value": {"bear": 8.0, "base": 12.0, "bull": 16.0, "unit": "元/股"},
            "artifact_paths": {"valuation_report_md": source_path} if source_path else {},
            "gaps": [],
        }

    def test_snapshot_deep_copies_summary_and_never_overwrites(self) -> None:
        """summary 后续变化不得污染快照，重复冻结不得覆盖首次文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r_history"
            run_dir.mkdir()
            source = Path(tmp) / "valuation_report.md"
            source.write_text("version-one", encoding="utf-8")
            summary = self._summary(str(source))
            snapshot = history.build_decision_snapshot(
                "r_history",
                "company",
                {"as_of_date": "2026-07-10", "report_type": "annual"},
                self._events(summary),
                frozen_at="2026-07-10T18:30:01+08:00",
            )
            summary["fair_value"]["base"] = 999.0
            self.assertEqual(snapshot["decision"]["fair_value"]["base"], 12.0)
            signature = snapshot["source_artifacts"]["valuation_report_md"]
            self.assertEqual(signature["status"], "available")
            self.assertEqual(len(signature["sha256"]), 64)

            first, created = history.freeze_decision_snapshot(run_dir, snapshot)
            self.assertTrue(created)
            replacement = json.loads(json.dumps(snapshot))
            replacement["decision"]["one_line_conclusion"] = "不得覆盖"
            second, created_again = history.freeze_decision_snapshot(run_dir, replacement)
            self.assertFalse(created_again)
            self.assertEqual(second, first)
            disk = json.loads((run_dir / history.SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(disk["decision"]["one_line_conclusion"], "当时结论")

    def test_old_run_get_derives_without_materializing(self) -> None:
        """旧 run 的只读访问只派生，首次写 review 前不生成快照文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            snapshot, status, warnings = history.derive_or_load_snapshot(
                run_dir,
                "r_old",
                "company",
                {"as_of_date": "2026-07-10"},
                self._events(self._summary()),
                materialize=False,
            )
            self.assertEqual(status, "derived")
            self.assertEqual(warnings, [])
            self.assertEqual(snapshot["knowledge_cutoff"], "2026-07-10")
            self.assertFalse((run_dir / history.SNAPSHOT_FILENAME).exists())

            materialized, status, _ = history.derive_or_load_snapshot(
                run_dir,
                "r_old",
                "company",
                {"as_of_date": "2026-07-10"},
                self._events(self._summary()),
                materialize=True,
            )
            self.assertEqual(status, "frozen")
            self.assertEqual(materialized["run_id"], "r_old")
            self.assertTrue((run_dir / history.SNAPSHOT_FILENAME).exists())

    def test_local_tencent_prices_take_same_source_and_build_metrics(self) -> None:
        """腾讯 qfqday 同源覆盖两端时优先使用，并计算描述性指标。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tencent_daily_quote_000001.json").write_text(
                json.dumps(
                    {
                        "data": {
                            "sz000001": {
                                "qfqday": [
                                    ["2026-07-09", "9.8", "10.0", "10.1", "9.7", "100"],
                                    ["2026-07-13", "12.5", "13.0", "13.2", "12.4", "120"],
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "eastmoney_000001.json").write_text(
                json.dumps(
                    {
                        "result": {
                            "data": [
                                {"TRADE_DATE": "2026-07-10 00:00:00", "CLOSE_PRICE": 10.5},
                                {"TRADE_DATE": "2026-07-14 00:00:00", "CLOSE_PRICE": 13.5},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            snapshot = history.build_decision_snapshot(
                "r_price",
                "company",
                {"as_of_date": "2026-07-10"},
                self._events(self._summary()),
            )
            review = history.build_review(
                snapshot,
                "2026-07-14",
                roots=[root],
                today=_dt.date(2026, 7, 15),
            )
            pair = review["prices"]["stock"]
            self.assertTrue(pair["same_source"])
            self.assertEqual(pair["source"], "tencent_qfqday")
            self.assertEqual(pair["baseline"]["observation_date"], "2026-07-09")
            self.assertEqual(pair["current"]["observation_date"], "2026-07-13")
            self.assertAlmostEqual(review["metrics"]["spot_price_change"], 0.3)
            self.assertEqual(review["metrics"]["elapsed_days"], 4)
            self.assertEqual(review["metrics"]["valuation_bucket"]["bucket"], "base_to_bull")
            self.assertIn("bear", review["metrics"]["valuation_bucket"]["distances_to_points"])
            self.assertEqual(len(review["limitations"]), 2)

    def test_manual_current_fallback_uses_frozen_baseline_and_saves_falsification(self) -> None:
        """本地 current 缺失时使用手工值，baseline 回退冻结价格并提示口径。"""
        snapshot = history.build_decision_snapshot(
            "r_manual_current",
            "company",
            {"as_of_date": "2026-07-10"},
            self._events(self._summary()),
        )
        with patch("research_console.history.load_local_price_series", return_value={
            "tencent_qfqday": [],
            "eastmoney_trade_close": [],
        }):
            review = history.build_review(
                snapshot,
                "2026-07-14",
                current_price=14,
                current_price_date="2026-07-14",
                current_price_source="用户券商收盘截图",
                falsification_status="breached",
                falsification_notes="渠道价格条件已触发",
                today=_dt.date(2026, 7, 15),
            )
        pair = review["prices"]["stock"]
        self.assertEqual(pair["baseline"]["source"], "decision_snapshot")
        self.assertEqual(pair["current"]["source_kind"], "manual_input")
        self.assertTrue(any("frozen decision price" in item for item in pair["basis_warnings"]))
        self.assertAlmostEqual(review["metrics"]["spot_price_change"], 0.4)
        self.assertEqual(review["falsification_status"], "breached")
        self.assertIn("渠道价格", review["falsification_notes"])

    def test_manual_benchmark_fills_missing_local_pair(self) -> None:
        """benchmark 本地缺失时可由两端手工 observation 计算变化与超额。"""
        snapshot = history.build_decision_snapshot(
            "r_manual_benchmark",
            "company",
            {"as_of_date": "2026-07-10"},
            self._events(self._summary()),
        )
        with patch("research_console.history.load_local_price_series", return_value={
            "tencent_qfqday": [],
            "eastmoney_trade_close": [],
        }):
            review = history.build_review(
                snapshot,
                "2026-07-14",
                current_price=12,
                current_price_date="2026-07-14",
                current_price_source="stock-manual",
                benchmark_code="000300",
                benchmark_baseline_price=100,
                benchmark_baseline_date="2026-07-10",
                benchmark_baseline_source="benchmark-manual",
                benchmark_current_price=110,
                benchmark_current_date="2026-07-14",
                benchmark_current_source="benchmark-manual",
                today=_dt.date(2026, 7, 15),
            )
        benchmark = review["prices"]["benchmark"]
        self.assertTrue(benchmark["same_source"])
        self.assertAlmostEqual(review["metrics"]["benchmark_change"], 0.1)
        self.assertAlmostEqual(review["metrics"]["excess_return"], 0.1)

    def test_valuation_bucket_is_interval_and_requires_monotonic_points(self) -> None:
        """四段区间边界稳定，缺档或非单调三档必须 unavailable。"""
        snapshot = history.build_decision_snapshot(
            "r_bucket",
            "company",
            {"as_of_date": "2026-07-10"},
            self._events(self._summary()),
        )
        expected = [(7, "below_bear"), (9, "bear_to_base"), (14, "base_to_bull"), (17, "above_bull")]
        for price, bucket in expected:
            with self.subTest(price=price):
                result = history._valuation_bucket(snapshot, price)
                self.assertEqual(result["bucket"], bucket)
                self.assertEqual(set(result["distances_to_points"]), {"bear", "base", "bull"})

        broken = json.loads(json.dumps(snapshot))
        broken["decision"]["fair_value"] = {"bear": 12, "base": 10, "bull": 16}
        result = history._valuation_bucket(broken, 11)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "fair_value_non_monotonic")

    def test_eastmoney_reader_rejects_non_positive_prices_and_uses_previous_day(self) -> None:
        """非正价格不可进入指标，最近合法交易日可以向前回退。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "eastmoney_000001.json").write_text(
                json.dumps(
                    {
                        "result": {
                            "data": [
                                {"TRADE_DATE": "2026-07-10", "CLOSE_PRICE": 10},
                                {"TRADE_DATE": "2026-07-13", "CLOSE_PRICE": 0},
                                {"TRADE_DATE": "2026-07-14", "CLOSE_PRICE": -2},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            pair = history.resolve_price_pair("000001", "2026-07-10", "2026-07-14", roots=[root])
            self.assertEqual(pair["baseline"]["observation_date"], "2026-07-10")
            self.assertEqual(pair["current"]["observation_date"], "2026-07-10")
            self.assertEqual(history._price_change(pair), 0.0)

    def test_corrupt_review_lines_return_warnings(self) -> None:
        """损坏 review 不阻塞其余记录读取。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / history.REVIEWS_FILENAME).write_text(
                '{"review_id":"ok"}\n{broken\n[]\n',
                encoding="utf-8",
            )
            reviews, warnings = history.read_reviews(run_dir)
            self.assertEqual([item["review_id"] for item in reviews], ["ok"])
            self.assertEqual(len(warnings), 2)

    def test_review_date_validation(self) -> None:
        """回看日格式、未来日期与早于知识截止日均应明确拒绝。"""
        with self.assertRaises(history.HistoryValidationError):
            history.parse_review_date("2026/07/10")
        with self.assertRaises(history.HistoryValidationError):
            history.parse_review_date("2026-07-09", cutoff="2026-07-10", today=_dt.date(2026, 7, 15))
        with self.assertRaises(history.HistoryValidationError):
            history.parse_review_date("2026-07-16", cutoff="2026-07-10", today=_dt.date(2026, 7, 15))


class DecisionFreezeEngineTest(unittest.IsolatedAsyncioTestCase):
    """验证引擎在唯一终态前冻结快照并对失败降级。"""

    async def test_snapshot_artifact_precedes_terminal_event(self) -> None:
        """成功冻结时 artifact_created 必须先于 run_completed。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = engine.Run("r_freeze", "company", {"as_of_date": "2026-07-10"}, "manual", Path(tmp))
            summary = DecisionHistoryTest._summary()
            frozen_summary, status = await engine._freeze_company_decision_before_terminal(run, summary, "completed")
            await run.bus.publish(run.run_id, "run_completed", payload={"status": status, "summary": frozen_summary})
            types = [event["type"] for event in run.bus.events]
            self.assertEqual(types[-2:], ["artifact_created", "run_completed"])
            self.assertTrue((Path(tmp) / history.SNAPSHOT_FILENAME).exists())

    async def test_snapshot_failure_downgrades_completed_to_partial(self) -> None:
        """冻结异常保留结论并将 completed 降级为 partial。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = engine.Run("r_freeze_fail", "company", {"as_of_date": "2026-07-10"}, "manual", Path(tmp))
            with patch("research_console.engine.history.freeze_decision_snapshot", side_effect=OSError("disk full")):
                summary, status = await engine._freeze_company_decision_before_terminal(
                    run,
                    DecisionHistoryTest._summary(),
                    "completed",
                )
            self.assertEqual(status, "partial")
            self.assertTrue(any("snapshot freezing failed" in item for item in summary["gaps"]))
            self.assertEqual(run.bus.events[-1]["type"], "run_error")
            self.assertFalse(any(event["type"] == "run_completed" for event in run.bus.events))


class DecisionHistoryApiTest(unittest.IsolatedAsyncioTestCase):
    """验证 decision/reviews API 的旧 run 物化与明确错误状态。"""

    async def test_get_derives_and_post_materializes_old_run(self) -> None:
        """GET 不写文件，POST 首次创建 review 时冻结快照。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            run = engine.Run(
                "r_api_history",
                "company",
                {"as_of_date": "2026-07-10", "stock_code": "000001"},
                "manual",
                run_dir,
            )
            run.status = "completed"
            run.bus.load_events(DecisionHistoryTest._events(DecisionHistoryTest._summary()))
            console_app.ENGINE.runs[run.run_id] = run
            try:
                get_response = await console_app.get_decision(run.run_id)
                get_payload = json.loads(get_response.body.decode("utf-8"))
                self.assertEqual(get_payload["status"], "derived")
                self.assertFalse((run_dir / history.SNAPSHOT_FILENAME).exists())

                with patch("research_console.history.load_local_price_series", return_value={
                    "tencent_qfqday": [],
                    "eastmoney_trade_close": [],
                }):
                    post_response = await console_app.create_review(
                        run.run_id,
                        console_app.DecisionReviewRequest(
                            review_date="2026-07-14",
                            current_price=13,
                            current_price_date="2026-07-14",
                            current_price_source="API 手工收盘价",
                            falsification_status="held",
                            falsification_notes="核心证伪条件尚未触发",
                            note="首次回看",
                        ),
                    )
                post_payload = json.loads(post_response.body.decode("utf-8"))
                self.assertEqual(post_response.status_code, 201)
                self.assertEqual(post_payload["status"], "created")
                self.assertTrue((run_dir / history.SNAPSHOT_FILENAME).exists())
                self.assertEqual(post_payload["review_count"], 1)
                self.assertEqual(post_payload["review"]["falsification_status"], "held")
                self.assertEqual(post_payload["review"]["prices"]["stock"]["current"]["source_kind"], "manual_input")
            finally:
                console_app.ENGINE.runs.pop(run.run_id, None)

    async def test_invalid_review_date_does_not_materialize_snapshot(self) -> None:
        """无效 POST 不应产生不可逆快照副作用。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            run = engine.Run(
                "r_invalid_review",
                "company",
                {"as_of_date": "2026-07-10", "stock_code": "000001"},
                "manual",
                run_dir,
            )
            run.status = "completed"
            run.bus.load_events(DecisionHistoryTest._events(DecisionHistoryTest._summary()))
            console_app.ENGINE.runs[run.run_id] = run
            try:
                response = await console_app.create_review(
                    run.run_id,
                    console_app.DecisionReviewRequest(review_date="2026-07-09"),
                )
                payload = json.loads(response.body.decode("utf-8"))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(payload["status"], "invalid_review_request")
                self.assertFalse((run_dir / history.SNAPSHOT_FILENAME).exists())
            finally:
                console_app.ENGINE.runs.pop(run.run_id, None)

    async def test_invalid_manual_price_and_date_do_not_materialize_snapshot(self) -> None:
        """非正手工价格和越界日期都必须在 snapshot 物化前返回 400。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            run = engine.Run(
                "r_invalid_manual",
                "company",
                {"as_of_date": "2026-07-10", "stock_code": "000001"},
                "manual",
                run_dir,
            )
            run.status = "completed"
            run.bus.load_events(DecisionHistoryTest._events(DecisionHistoryTest._summary()))
            console_app.ENGINE.runs[run.run_id] = run
            try:
                requests = [
                    console_app.DecisionReviewRequest(
                        review_date="2026-07-14",
                        current_price=0,
                        current_price_date="2026-07-14",
                        current_price_source="manual",
                    ),
                    console_app.DecisionReviewRequest(
                        review_date="2026-07-14",
                        current_price=12,
                        current_price_date="2026-07-15",
                        current_price_source="manual",
                    ),
                ]
                for request in requests:
                    response = await console_app.create_review(run.run_id, request)
                    payload = json.loads(response.body.decode("utf-8"))
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(payload["status"], "invalid_review_request")
                    self.assertFalse((run_dir / history.SNAPSHOT_FILENAME).exists())
            finally:
                console_app.ENGINE.runs.pop(run.run_id, None)

    async def test_missing_noncompany_and_summary_unavailable_have_explicit_status(self) -> None:
        """三类不可用情况必须返回稳定 status，而不是模糊 500。"""
        missing = await console_app.get_decision("not-exists")
        self.assertEqual(json.loads(missing.body.decode("utf-8"))["status"], "run_not_found")

        with tempfile.TemporaryDirectory() as tmp:
            industry = engine.Run("r_industry_history", "industry", {}, "manual", Path(tmp) / "industry")
            no_summary = engine.Run("r_no_summary", "company", {}, "manual", Path(tmp) / "company")
            console_app.ENGINE.runs[industry.run_id] = industry
            console_app.ENGINE.runs[no_summary.run_id] = no_summary
            try:
                unsupported = await console_app.get_decision(industry.run_id)
                unavailable = await console_app.get_decision(no_summary.run_id)
                self.assertEqual(json.loads(unsupported.body.decode("utf-8"))["status"], "unsupported_run_mode")
                self.assertEqual(json.loads(unavailable.body.decode("utf-8"))["status"], "summary_unavailable")
            finally:
                console_app.ENGINE.runs.pop(industry.run_id, None)
                console_app.ENGINE.runs.pop(no_summary.run_id, None)


class DecisionSummaryContractTest(unittest.TestCase):
    """验证结论摘要新增字段与前端静态契约。"""

    def test_summary_normalizes_view_and_preserves_price_basis(self) -> None:
        """估值观点归一但保留 raw，价格观察日与 cutoff 状态完整输出。"""
        state = _minimal_state(valuation="ready", formal="ready")
        state["request"]["as_of_date"] = "2026-07-10"
        report = {
            "valuation_view": "fairly_valued",
            "market_snapshot": {
                "reference_close_2026_07_09_cny": 10.0,
                "price_source": "local_fixture",
                "date_treatment": "使用不晚于基准日的最近交易日",
            },
            "fair_value_range_per_share": {"bear": 8, "base": 12, "bull": 16},
        }
        summary = state_reader.build_company_summary(state, report, {}, {})
        self.assertEqual(summary["as_of_date"], "2026-07-10")
        self.assertEqual(summary["valuation_view"], "fair")
        self.assertEqual(summary["valuation_view_raw"], "fairly_valued")
        self.assertEqual(summary["price_observation"]["observation_date"], "2026-07-09")
        self.assertEqual(summary["cutoff_status"], "before_cutoff")
        self.assertIn("最近交易日", summary["price_basis"])

    def test_frontend_contains_review_contract_without_framework_or_chart(self) -> None:
        """零构建前端必须展示双区、调用新 API，且不引入图表框架。"""
        html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (config.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css = (config.STATIC_DIR / "style.css").read_text(encoding="utf-8")
        self.assertIn("Original View / Current Review", html)
        self.assertIn('<html lang="en">', html)
        self.assertIn("Research Workshop · Multi-Agent Investment Research Console", html)
        self.assertIn("/decision", js)
        self.assertIn("/reviews", js)
        self.assertIn("unavailable", js)
        self.assertIn("TSR", js)
        self.assertIn("current_price_source", js)
        self.assertIn("benchmark_baseline_price", js)
        self.assertIn("falsification_status", js)
        self.assertIn("below_bear", js)
        self.assertIn(".review-form", css)
        self.assertNotIn("Chart.js", html + js)
        self.assertNotIn("React", html + js)

        # 路径主视图必须保持固定研究链路；TaskCreate 产生的动态工作项只进入侧栏，
        # 否则节点数量和连接关系会随协调器措辞变化，破坏前端静态路径契约。
        route_start = js.index("function routeMilestones()")
        task_start = js.index("function taskItems()", route_start)
        route_body = js[route_start:task_start]
        self.assertIn("state.stepOrder", route_body)
        self.assertIn("state.steps.get", route_body)
        self.assertNotIn("state.workOrder", route_body)
        self.assertNotIn("state.workItems", route_body)
        self.assertIn("const milestones = routeMilestones();", js)

        # initial audit 的 step_completed 早于 plan_ready，必须先缓存再回放，不能让第一个
        # 固定里程碑永远停在 Pending。
        self.assertIn("earlyStepPatches: new Map()", js)
        self.assertIn("state.earlyStepPatches.set", js)
        self.assertIn("for (const [id, patch] of state.earlyStepPatches.entries())", js)
        self.assertIn("case 'target_resolved':", js)


if __name__ == "__main__":
    unittest.main()
