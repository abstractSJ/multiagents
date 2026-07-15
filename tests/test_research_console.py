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
        "market_context": "ready",
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


class CoordinatorCommandTest(unittest.TestCase):
    """验证完整 /rec 提示词与 stream-json 命令。"""

    def test_prompt_transfers_company_params_once(self) -> None:
        """提示词只调用一次完整 /rec，并正确映射 report_year→fiscal_year。"""
        prompt = steps.build_company_coordinator_prompt(
            {
                "target": "贵州茅台",
                "stock_code": "600519",
                "company_name": "贵州茅台",
                "report_year": 2025,
                "report_type": "annual",
                "depth": "deep",
                "focus": "盈利质量 与 分红",
                "as_of_date": "2026-07-14",
                "force_refresh": True,
                "run_market_context": False,
                "market_context_freshness": "oneWeek",
            },
            "r_test",
        )
        self.assertTrue(prompt.startswith("/rec "))
        self.assertEqual(prompt.count("/rec"), 1)
        self.assertIn('fiscal_year="2025"', prompt)
        self.assertIn('force_refresh=true', prompt)
        self.assertIn('run_market_context=false', prompt)
        self.assertIn('market_context_freshness="oneWeek"', prompt)
        self.assertIn("不实现或输出阶段二 research_requests 协议", prompt)
        self.assertIn("[任务#编号]", prompt)

    def test_command_uses_verified_stream_json_flags_without_bare(self) -> None:
        """命令包含 stream-json/verbose/partial/auto，且不关闭项目环境发现。"""
        cmd = steps.build_claude_stream_command("/rec target=600519", "C:/bin/claude.exe")
        self.assertEqual(cmd[:3], ["C:/bin/claude.exe", "-p", "/rec target=600519"])
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertIn("--include-partial-messages", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--bare", cmd)
        self.assertNotIn("--input-format", cmd)

    def test_windows_npm_shim_resolves_to_native_executable(self) -> None:
        """Windows 优先直启 npm 包内 claude.exe，确保取消时能控制真实进程。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim = root / "claude.CMD"
            native = root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            native.parent.mkdir(parents=True)
            shim.write_text("@echo off", encoding="utf-8")
            native.write_bytes(b"")
            with patch("research_console.engine.shutil.which", return_value=str(shim)), patch.object(
                engine.os, "name", "nt"
            ):
                self.assertEqual(engine._resolve_claude_executable(), str(native))

    def test_coordinator_prompt_preserves_valuation_and_industry_options(self) -> None:
        """programmatic API 传入的 /rec 定价与行业参数不得被白名单静默丢弃。"""
        prompt = steps.build_company_coordinator_prompt(
            {
                "target": "600519",
                "fiscal_year": "2025",
                "market_price": 1420.5,
                "valuation_method": "DCF",
                "run_industry": True,
            },
            "r_options",
        )
        self.assertIn('market_price="1420.5"', prompt)
        self.assertIn('valuation_method="DCF"', prompt)
        self.assertIn("run_industry=true", prompt)

    def test_audit_command_accepts_fiscal_year_alias(self) -> None:
        """仅传 fiscal_year 时 audit 仍必须收到统一的 --report-year。"""
        cmd = state_reader.build_audit_command({"target": "600519", "fiscal_year": "2024"}, write_state=False)
        self.assertEqual(cmd[cmd.index("--report-year") + 1], "2024")
        normalized = engine.Engine._normalize_company_params({"fiscal_year": 2024})
        self.assertEqual(normalized["report_year"], "2024")
        self.assertEqual(normalized["fiscal_year"], "2024")
        with self.assertRaises(ValueError):
            engine.Engine._normalize_company_params({"report_year": 2025, "fiscal_year": 2024})

    def test_market_context_command_supports_strict_cutoff(self) -> None:
        """strict_cutoff=true 必须把硬截止开关传给市场上下文采集脚本。"""
        cmd = steps.build_market_context_cmd(
            target="600519",
            stock_code="600519",
            company_name="贵州茅台",
            as_of_date="2026-07-14",
            strict_cutoff=True,
        )
        self.assertIn("--strict-cutoff", cmd)

    def test_formal_prompt_uses_dated_formal_dir_and_requires_cutoff_audit(self) -> None:
        """正式分析产物必须写入 dated formal_dir，并显式要求 cutoff_audit。"""
        formal_dir = "D:/workspace/formal/2026-07-14"
        pack = steps.build_formal_financial_analysis_prompt(
            {
                "company_name": "贵州茅台",
                "stock_code": "600519",
                "report_year": "2025",
                "report_type": "annual",
                "analyst_dir": "D:/workspace/analyst",
                "formal_dir": formal_dir,
                "as_of_date": "2026-07-14",
                "source_report_published_at": "2026-04-17",
            }
        )
        self.assertTrue(all(Path(path).parent == Path(formal_dir) for path in pack["expected_artifacts"]))
        self.assertIn("cutoff_audit", pack["prompt"])
        self.assertIn("2026-07-14", pack["prompt"])

    def test_force_refresh_command_builders_add_overwrite(self) -> None:
        """legacy force_refresh 必须真正覆盖 parse/digest/RAG 旧产物。"""
        self.assertIn("--overwrite", steps.build_processor_parse_cmd("600519", "annual", "2025", overwrite=True))
        self.assertIn("--overwrite", steps.build_digest_prepare_cmd("D:/content.json", overwrite=True))
        self.assertIn("--overwrite", steps.build_digest_auto_cmd("D:/pipeline", overwrite=True))
        self.assertIn("--overwrite", steps.build_rag_cmd("D:/content.json", overwrite=True))

    def test_industry_event_command_preserves_list_and_counterfactual_with_anchor(self) -> None:
        """公司锚点事件研究也要传递全部事件参数，列表按逗号编码。"""
        cmd = steps.build_industry_collect_cmd(
            {
                "stock_code": "000001",
                "company_name": "样例公司",
                "fiscal_year": "2025",
                "as_of_date": "2026-07-14",
                "deliverable_type": "theme_event_study",
                "impact_variables": ["price", "supply"],
                "counterfactual_assumption": "无事件基线",
            }
        )
        self.assertEqual(cmd[cmd.index("--impact-variables") + 1], "price,supply")
        self.assertEqual(cmd[cmd.index("--counterfactual-assumption") + 1], "无事件基线")


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


class ClaudeCoordinatorMapperTest(unittest.TestCase):
    """验证 Claude Code 顶层事件的宽容映射与去重。"""

    def test_init_text_partial_and_result_mapping(self) -> None:
        """init 捕获 session，partial 节流，完整文本与 result 正常保存。"""
        mapper = engine.ClaudeCoordinatorEventMapper(partial_interval=0.5)
        init = mapper.map_event({"type": "system", "subtype": "init", "session_id": "s_123"}, now=1.0)
        self.assertEqual(init[0]["type"], "coordinator_session_started")
        self.assertEqual(mapper.session_id, "s_123")

        mapper.map_event(
            {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
            now=1.0,
        )
        first = mapper.map_event(
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "正在"}}},
            now=1.0,
        )
        second = mapper.map_event(
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "研究"}}},
            now=1.1,
        )
        flushed = mapper.map_event(
            {"type": "stream_event", "event": {"type": "content_block_stop"}},
            now=1.2,
        )
        self.assertEqual(first[0]["payload"]["text"], "正在")
        self.assertEqual(second, [])
        self.assertEqual(flushed[0]["payload"]["text"], "正在研究")

        full = mapper.map_event(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "完整结论"}]}},
            now=2.0,
        )
        self.assertEqual(full[0]["payload"], {"text": "完整结论", "partial": False})
        mapper.map_event(
            {"type": "result", "subtype": "success", "session_id": "s_123", "is_error": False},
            now=3.0,
        )
        self.assertEqual(mapper.result["subtype"], "success")

    def test_agent_tool_and_task_events_are_deduplicated(self) -> None:
        """assistant Agent fallback 与 system task_started 不得重复发启动事件。"""
        mapper = engine.ClaudeCoordinatorEventMapper()
        assistant = mapper.map_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "Agent",
                            "input": {"subagent_type": "valuation-analyst", "description": "完成估值"},
                        }
                    ]
                },
            }
        )
        duplicate = mapper.map_event(
            {
                "type": "system",
                "subtype": "task_started",
                "tool_use_id": "tool_1",
                "task_id": "task_1",
                "subagent_type": "valuation-analyst",
                "description": "完成估值",
            }
        )
        completed = mapper.map_event(
            {
                "type": "system",
                "subtype": "task_notification",
                "tool_use_id": "tool_1",
                "task_id": "task_1",
                "status": "completed",
                "summary": "估值完成",
            }
        )
        self.assertEqual([item["type"] for item in assistant], ["agent_started", "handoff"])
        self.assertEqual(assistant[1]["payload"]["kind"], "delegation")
        self.assertEqual(duplicate, [])
        self.assertEqual([item["type"] for item in completed], ["agent_completed", "handoff"])
        self.assertEqual(completed[0]["payload"]["agent_name"], "valuation-analyst")
        self.assertFalse(completed[0]["payload"]["is_error"])
        self.assertEqual(completed[1]["payload"]["kind"], "delivery")

    def test_background_running_ack_waits_for_terminal_notification(self) -> None:
        """后台 Agent 的 running 回执只能登记 task_id，不能抢先发完成事件。"""
        mapper = engine.ClaudeCoordinatorEventMapper()
        mapper.map_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_bg",
                            "name": "Agent",
                            "input": {"subagent_type": "financial-analyst", "description": "后台分析"},
                        }
                    ]
                },
            }
        )
        running = mapper.map_event(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "tool_bg", "content": "running"}]
                },
                "tool_use_result": {"status": "running", "task_id": "task_bg"},
            }
        )
        self.assertEqual(running, [])
        self.assertIsNotNone(mapper._agent_record("task_bg"))

        completed = mapper.map_event(
            {
                "type": "system",
                "subtype": "task_notification",
                "tool_use_id": "tool_bg",
                "task_id": "task_bg",
                "status": "completed",
                "summary": "分析完成",
            }
        )
        self.assertEqual([item["type"] for item in completed], ["agent_completed", "handoff"])
        self.assertEqual(completed[0]["payload"]["runtime_task_id"], "task_bg")

    def test_async_launched_ack_is_not_terminal(self) -> None:
        """async_launched 只是后台接受回执，不得让 Agent 和交付链提前结束。"""
        mapper = engine.ClaudeCoordinatorEventMapper()
        mapper.map_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_async",
                            "name": "Agent",
                            "input": {
                                "subagent_type": "information-collector",
                                "description": "[任务#2] 补齐年报文件",
                            },
                        }
                    ]
                },
            }
        )
        ack = mapper.map_event(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "tool_async", "content": "launched"}]},
                "tool_use_result": {"status": "async_launched", "task_id": "runtime_async"},
            }
        )
        self.assertEqual(ack, [])
        record = mapper._agent_record("runtime_async")
        self.assertIsNotNone(record)
        self.assertEqual(record["work_item_id"], "2")
        self.assertNotIn("tool_async", mapper.completed_agents)

    def test_local_bash_task_started_does_not_create_fake_agent(self) -> None:
        """后台 Bash 只能补充工具运行 id，不能生成 agent_name=agent 的假角色。"""
        mapper = engine.ClaudeCoordinatorEventMapper()
        started = mapper.map_event(
            {
                "type": "assistant",
                "parent_tool_use_id": "parent_agent",
                "message": {
                    "content": [{"type": "tool_use", "id": "bash_tool", "name": "Bash", "input": {"command": "python x.py"}}]
                },
            }
        )
        self.assertEqual([item["type"] for item in started], ["tool_activity", "coordinator_message"])
        system_started = mapper.map_event(
            {
                "type": "system",
                "subtype": "task_started",
                "task_type": "local_bash",
                "tool_use_id": "bash_tool",
                "task_id": "bash_runtime",
                "description": "运行脚本",
            }
        )
        self.assertEqual(system_started, [])
        self.assertNotIn("bash_runtime", mapper.active_agents)
        self.assertEqual(mapper.active_tools["bash_tool"]["runtime_task_id"], "bash_runtime")

    def test_task_create_and_update_emit_stable_work_items(self) -> None:
        """TaskCreate/TaskUpdate 应保留高层调度任务标题与真实状态。"""
        mapper = engine.ClaudeCoordinatorEventMapper()
        mapper.map_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "task_create_tool",
                            "name": "TaskCreate",
                            "input": {"subject": "完成三档估值", "description": "输出目标价", "activeForm": "正在估值"},
                        }
                    ]
                },
            }
        )
        created = mapper.map_event(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "task_create_tool", "content": "ok"}]},
                "tool_use_result": {"task": {"id": "5", "subject": "完成三档估值"}},
            }
        )
        self.assertEqual([item["type"] for item in created], ["work_item_upsert"])
        self.assertEqual(created[0]["payload"]["work_item_id"], "5")
        self.assertEqual(created[0]["payload"]["status"], "pending")

        mapper.map_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "task_update_tool",
                            "name": "TaskUpdate",
                            "input": {"taskId": "5", "status": "in_progress"},
                        }
                    ]
                },
            }
        )
        updated = mapper.map_event(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "task_update_tool", "content": "ok"}]},
                "tool_use_result": {"task": {"id": "5"}},
            }
        )
        self.assertEqual(updated[0]["payload"]["status"], "in_progress")
        self.assertEqual(updated[0]["payload"]["title"], "完成三档估值")

    def test_tool_activity_inherits_parent_agent_and_completes(self) -> None:
        """子 Agent 的普通工具调用应保持 owner，并用同一 tool id 收束。"""
        mapper = engine.ClaudeCoordinatorEventMapper()
        mapper.map_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "agent_parent",
                            "name": "Agent",
                            "input": {"subagent_type": "valuation-analyst", "description": "估值"},
                        }
                    ]
                },
            }
        )
        started = mapper.map_event(
            {
                "type": "assistant",
                "parent_tool_use_id": "agent_parent",
                "message": {"content": [{"type": "tool_use", "id": "read_tool", "name": "Read", "input": {"file_path": "secret"}}]},
            }
        )
        self.assertEqual(started[0]["type"], "tool_activity")
        self.assertEqual(started[0]["owner"], "valuation-analyst")
        self.assertEqual(started[0]["payload"]["invocation_id"], "agent_parent")
        self.assertNotIn("file_path", started[0]["payload"])
        completed = mapper.map_event(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "read_tool", "content": "x" * 1000}]},
                "tool_use_result": {"status": "completed"},
            }
        )
        self.assertEqual(completed[0]["payload"]["phase"], "completed")
        self.assertLessEqual(len(completed[0]["payload"]["summary"]), engine._TOOL_SUMMARY_LIMIT)

    def test_bad_json_line_is_recoverable(self) -> None:
        """坏 JSON 行返回错误描述，不向调用者抛异常。"""
        event, error = engine.parse_claude_stream_line('{"type":')
        self.assertIsNone(event)
        self.assertTrue(error)
        event, error = engine.parse_claude_stream_line('{"type":"result"}')
        self.assertEqual(event, {"type": "result"})
        self.assertIsNone(error)


class MilestoneProjectionTest(unittest.TestCase):
    """验证 coordinator 左栏交付里程碑由 audit 状态而非静态计划驱动。"""

    def test_ready_transition_completes_initially_pending_steps(self) -> None:
        """formal/valuation 从缺失变 ready 后应在里程碑中完成，初始复用层仍为 skipped。"""
        initial = _minimal_state(valuation="missing", formal="partial")
        plan = steps.build_company_plan(initial, llm_mode="coordinator_cli")
        changed = _minimal_state(valuation="ready", formal="ready")
        projected = engine._build_milestone_states(
            changed,
            plan,
            run_market_context=True,
        )
        self.assertEqual(projected["collector_fetch"]["run_status"], "skipped")
        self.assertEqual(projected["formal_financial_analysis"]["run_status"], "completed")
        self.assertEqual(projected["valuation_update"]["run_status"], "completed")
        self.assertEqual(projected["valuation_update"]["readiness_status"], "ready")
        self.assertNotIn("audit", projected)
        self.assertNotIn("deliver", projected)

    def test_force_refresh_requires_artifact_signature_change(self) -> None:
        """强制刷新不能把运行前已经 ready 的旧文件立即算成本轮完成。"""
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "valuation_report.json"
            artifact.write_text("old", encoding="utf-8")
            state = _minimal_state(valuation="ready", formal="ready", artifact_path=str(artifact))
            plan = steps.build_company_plan(state, force_refresh=True, llm_mode="coordinator_cli")
            baseline = engine._milestone_baseline_signatures(state)
            pending = engine._build_milestone_states(
                state,
                plan,
                run_market_context=True,
                force_refresh=True,
                baseline_signatures=baseline,
            )
            self.assertEqual(pending["valuation_update"]["run_status"], "pending")
            time.sleep(0.002)
            artifact.write_text("new-content", encoding="utf-8")
            completed = engine._build_milestone_states(
                state,
                plan,
                run_market_context=True,
                force_refresh=True,
                baseline_signatures=baseline,
            )
            self.assertEqual(completed["valuation_update"]["run_status"], "completed")


class CoordinatorPersistenceTest(unittest.TestCase):
    """验证新 meta 字段与旧运行恢复兼容。"""

    def test_meta_persists_session_and_old_meta_loads(self) -> None:
        """新 meta 写入 session/execution_mode；旧 meta 缺字段仍可恢复。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_dir = root / "r_new"
            new_dir.mkdir()
            run = engine.Run("r_new", "company", {}, "coordinator_cli", new_dir)
            run.status = "completed"
            run.claude_session_id = "session-new"
            run.execution_mode = "coordinator_cli"
            run.persist_meta()
            payload = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["claude_session_id"], "session-new")
            self.assertEqual(payload["execution_mode"], "coordinator_cli")

            old_dir = root / "r_old"
            old_dir.mkdir()
            (old_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "r_old",
                        "mode": "company",
                        "status": "completed",
                        "created_at": "2026-07-14T00:00:00+08:00",
                        "params": {},
                        "llm_mode": "manual",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(config, "RUNS_DIR", root):
                restored = engine.Engine()
                restored.load_persisted_runs()
            self.assertIn("r_old", restored.runs)
            self.assertIsNone(restored.runs["r_old"].claude_session_id)
            self.assertIsNone(restored.runs["r_old"].execution_mode)

    def test_failed_atomic_replace_preserves_previous_meta(self) -> None:
        """原子替换失败时旧 meta 仍须完整可解析，临时文件也应清理。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r_atomic"
            run_dir.mkdir()
            run = engine.Run("r_atomic", "company", {}, "manual", run_dir)
            run.status = "running"
            run.persist_meta()
            run.status = "completed"
            with patch("research_console.engine.os.replace", side_effect=OSError("replace failed")):
                run.persist_meta()
            payload = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "running")
            self.assertEqual(list(run_dir.glob(".meta.*.tmp")), [])

    def test_unresolved_orphan_pid_retains_company_lease(self) -> None:
        """重启时无法清理遗留 coordinator，必须阻止同公司新 run。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "r_orphan"
            run_dir.mkdir()
            (run_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "r_orphan",
                        "mode": "company",
                        "status": "running",
                        "created_at": "2026-07-14T00:00:00+08:00",
                        "params": {"stock_code": "600519", "report_year": "2025"},
                        "llm_mode": "coordinator_cli",
                        "coordinator_pid": 43210,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(config, "RUNS_DIR", root), patch(
                "research_console.engine._cleanup_persisted_coordinator", return_value=False
            ):
                restored = engine.Engine()
                restored.load_persisted_runs()
            run = restored.runs["r_orphan"]
            self.assertTrue(run.orphan_process_unresolved)
            with patch.object(config, "RUNS_DIR", root):
                with self.assertRaises(engine.WorkspaceLeaseConflict):
                    restored.create_run(
                        "company", {"stock_code": "600519", "report_year": "2024"}, "manual"
                    )

    def test_recovery_honors_existing_terminal_without_duplication(self) -> None:
        """events 已有 completed、meta 仍 running 时只修复 meta，不追加 failed 终态。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "r_terminal"
            run_dir.mkdir()
            (run_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "r_terminal",
                        "mode": "company",
                        "status": "running",
                        "created_at": "2026-07-14T00:00:00+08:00",
                        "params": {},
                        "llm_mode": "manual",
                    }
                ),
                encoding="utf-8",
            )
            events = [
                {"seq": 1, "ts": "t1", "run_id": "r_terminal", "type": "run_started", "payload": {}},
                {
                    "seq": 3,
                    "ts": "t3",
                    "run_id": "r_terminal",
                    "type": "run_completed",
                    "payload": {"status": "completed"},
                },
            ]
            (run_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            with patch.object(config, "RUNS_DIR", root):
                restored = engine.Engine()
                restored.load_persisted_runs()
            run = restored.runs["r_terminal"]
            self.assertEqual(run.status, "completed")
            self.assertEqual(sum(event["type"] == "run_completed" for event in run.bus.events), 1)
            self.assertEqual(run.bus.max_seq, 3)


class CoordinatorAsyncTest(unittest.IsolatedAsyncioTestCase):
    """验证 reader、状态观察器与 coordinator runner 的异步行为。"""

    async def test_chunked_ndjson_reader_accepts_lines_over_default_stream_limit(self) -> None:
        """超过 asyncio 默认 64 KiB 的单行事件必须完整读出，不触发分隔符异常。"""
        reader = asyncio.StreamReader()
        long_line = b'{"type":"assistant","payload":"' + (b"x" * 200_000) + b'"}'
        reader.feed_data(long_line[:90_000])
        reader.feed_data(long_line[90_000:] + b"\n")
        reader.feed_data(b'{"type":"result"}\n')
        reader.feed_eof()
        lines = [line async for line in engine._iter_ndjson_lines(reader)]
        self.assertEqual(lines[0], long_line)
        self.assertEqual(lines[1], b'{"type":"result"}')

    async def test_stream_reader_saves_raw_lines_and_survives_bad_json(self) -> None:
        """原始流完整落盘，坏行只生成 warning，session 立即写入 meta。"""
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "session-1"}, ensure_ascii=False),
            "{bad json",
            json.dumps(
                {"type": "result", "subtype": "success", "session_id": "session-1", "is_error": False, "permission_denials": []},
                ensure_ascii=False,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r_stream"
            run_dir.mkdir()
            run = engine.Run("r_stream", "company", {}, "coordinator_cli", run_dir)
            fake = _FakeProcess(lines, ["stderr note " + ("x" * 200_000)])
            create_proc = AsyncMock(return_value=fake)
            with patch("research_console.engine.shutil.which", return_value="C:/bin/claude.exe"), patch(
                "research_console.engine.asyncio.create_subprocess_exec", new=create_proc
            ):
                outcome = await engine._stream_claude_coordinator(run, "/rec target=600519")
            self.assertEqual(outcome.exit_code, 0)
            self.assertNotIn("limit", create_proc.await_args.kwargs)
            coordinator_env = create_proc.await_args.kwargs["env"]
            self.assertEqual(coordinator_env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "0")
            self.assertEqual(run.claude_session_id, "session-1")
            raw = (run_dir / config.COORDINATOR_EVENTS_FILENAME).read_text(encoding="utf-8")
            self.assertIn("{bad json", raw)
            types = [event["type"] for event in run.bus.events]
            self.assertIn("coordinator_session_started", types)
            self.assertTrue(
                any(
                    event["type"] == "coordinator_message" and "坏行" in event["payload"].get("text", "")
                    for event in run.bus.events
                )
            )

    async def test_state_observer_deduplicates_state_and_artifact_events(self) -> None:
        """语义状态变化与新产物各发一次，重复 audit 不刷屏。"""
        initial = _minimal_state()
        changed = _minimal_state(valuation="ready", formal="ready", artifact_path="D:/tmp/valuation_report.json")
        run = engine.Run("r_observer", "company", {}, "coordinator_cli", None)
        run.plan = steps.build_company_plan(initial, llm_mode="coordinator_cli")
        observer = engine.CompanyStateObserver(run, {}, initial, initial_plan=run.plan, interval=0.01)
        audit = AsyncMock(side_effect=[(changed, 0, ""), (changed, 0, "")])
        with patch("research_console.engine.state_reader.run_audit", new=audit):
            self.assertTrue(await observer.poll_once())
            self.assertFalse(await observer.poll_once())
        state_events = [event for event in run.bus.events if event["type"] == "state_refreshed"]
        artifact_events = [event for event in run.bus.events if event["type"] == "artifact_created"]
        self.assertEqual(len(state_events), 1)
        self.assertEqual(len(artifact_events), 1)
        self.assertEqual(state_events[0]["payload"]["layer_statuses"]["valuation"], "ready")
        self.assertEqual(
            state_events[0]["payload"]["milestone_states"]["valuation_update"]["run_status"],
            "completed",
        )
        for call in audit.await_args_list:
            self.assertFalse(call.kwargs["write_state"])
            self.assertIs(call.kwargs["process_registry"], run.procs)

    async def test_generated_at_only_change_is_ignored(self) -> None:
        """只改变 generated_at 的重审计不应产生 state_refreshed。"""
        initial = _minimal_state(generated_at="2026-07-14T00:00:00+08:00")
        rewritten = _minimal_state(generated_at="2026-07-14T00:00:05+08:00")
        run = engine.Run("r_same", "company", {}, "coordinator_cli", None)
        observer = engine.CompanyStateObserver(run, {}, initial, interval=0.01)
        with patch("research_console.engine.state_reader.run_audit", new=AsyncMock(return_value=(rewritten, 0, ""))):
            self.assertFalse(await observer.poll_once())
        self.assertFalse(any(event["type"] == "state_refreshed" for event in run.bus.events))

    async def test_coordinator_runner_never_dispatches_legacy_main_line(self) -> None:
        """coordinator_cli 只运行一次完整 /rec，随后 final audit + deliver。"""
        state = _minimal_state(valuation="ready", formal="ready")
        run = engine.Run(
            "r_coord",
            "company",
            {"target": "600519", "report_year": "2025", "run_market_context": True},
            "coordinator_cli",
            None,
        )
        pipeline = engine._CompanyCoordinatorPipeline(run)

        async def fake_audit(step_id: str, force_refresh: bool = False):
            pipeline.state = state
            pipeline._refresh_ctx()
            return state

        outcome = engine.CoordinatorProcessOutcome(
            0,
            {"type": "result", "subtype": "success", "is_error": False, "permission_denials": []},
            [],
            "session-test",
        )
        with patch.object(pipeline, "_step_audit", side_effect=fake_audit) as audit_mock, patch.object(
            pipeline, "_step_deliver", new=AsyncMock(return_value={"one_line_conclusion": "可用结论", "fair_value": {"base": 10}})
        ), patch(
            "research_console.engine._stream_claude_coordinator", new=AsyncMock(return_value=outcome)
        ) as stream_mock, patch.object(
            pipeline, "_dispatch", new=AsyncMock(side_effect=AssertionError("legacy dispatch must not run"))
        ) as dispatch_mock:
            status = await pipeline.execute()
        self.assertEqual(status, "completed")
        self.assertEqual(audit_mock.call_count, 2)
        stream_mock.assert_awaited_once()
        dispatch_mock.assert_not_awaited()
        self.assertEqual(run.bus.events[-1]["type"], "run_completed")

    async def test_permission_denial_downgrades_usable_delivery_to_partial(self) -> None:
        """CLI 成功但存在 permission denial 时，有可用交付则 partial 而非 completed。"""
        state = _minimal_state(valuation="ready", formal="ready")
        run = engine.Run("r_partial", "company", {}, "coordinator_cli", None)
        pipeline = engine._CompanyCoordinatorPipeline(run)
        outcome = engine.CoordinatorProcessOutcome(
            0,
            {"type": "result", "is_error": False, "permission_denials": [{"tool": "Bash"}]},
            [],
            "session-test",
        )
        status, issues = pipeline._coordinator_status(
            outcome,
            state,
            {"one_line_conclusion": "仍可交付", "fair_value": {"base": 10}},
        )
        self.assertEqual(status, "partial")
        self.assertTrue(any("permission denial" in issue for issue in issues))

    async def test_partial_preview_is_coalesced_and_not_persisted(self) -> None:
        """累计文本预览只保留最新内存快照，durable 文件只保存最终完整消息。"""
        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "events.jsonl"
            bus = engine.EventBus(events_file)
            await bus.publish("r_partial", "coordinator_message", payload={"text": "正", "partial": True})
            await bus.publish("r_partial", "coordinator_message", payload={"text": "正在", "partial": True})
            self.assertEqual(len(bus.events), 1)
            self.assertEqual(bus.events[0]["payload"]["text"], "正在")
            self.assertFalse(events_file.exists())

            final = await bus.publish(
                "r_partial", "coordinator_message", payload={"text": "正在研究", "partial": False}
            )
            self.assertEqual(final["seq"], 3)
            self.assertEqual(len(bus.events), 1)
            persisted = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([item["payload"]["text"] for item in persisted], ["正在研究"])

    async def test_event_bus_continues_from_max_seq_after_gap(self) -> None:
        """历史 seq 有空洞时，新事件必须从最大值加一，而不是 len(events)+1。"""
        bus = engine.EventBus(None)
        bus.load_events(
            [
                {"seq": 1, "type": "run_started", "payload": {}},
                {"seq": 3, "type": "step_started", "payload": {}},
                "not-an-object",
            ]
        )
        event = await bus.publish("r_gap", "step_completed", payload={})
        self.assertEqual(event["seq"], 4)
        self.assertEqual([item["seq"] for item in bus.events], [1, 3, 4])

    async def test_stream_subprocess_cancel_terminates_before_unregister(self) -> None:
        """legacy 脚本在 readline 阻塞期间取消，也必须先杀进程再移出登记表。"""
        proc = _FakeProcess([])
        proc.stdout = asyncio.StreamReader()  # 不 feed EOF，让读取持续阻塞
        run = engine.Run("r_cancel_proc", "company", {}, "manual", None)
        terminate = AsyncMock()
        with patch("research_console.engine.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), patch(
            "research_console.engine._terminate_process", new=terminate
        ):
            task = asyncio.create_task(
                engine._stream_subprocess(run, "collector_fetch", "information-collector", ["fake"])
            )
            await asyncio.sleep(0)
            self.assertIn(proc, run.procs)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        terminate.assert_awaited_once_with(proc)
        self.assertNotIn(proc, run.procs)

    async def test_run_audit_cancel_terminates_registered_process(self) -> None:
        """audit communicate 被取消时终止子进程，并清理所属 run 的进程登记。"""
        proc = _FakeProcess([])
        release = asyncio.Event()

        async def communicate():
            await release.wait()
            return b"{}", b""

        proc.communicate = communicate
        registry: set[object] = set()
        terminate = AsyncMock()
        with patch("research_console.state_reader.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), patch(
            "research_console.state_reader.terminate_subprocess", new=terminate
        ):
            task = asyncio.create_task(state_reader.run_audit({}, process_registry=registry))
            await asyncio.sleep(0)
            self.assertIn(proc, registry)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        terminate.assert_awaited_once_with(proc)
        self.assertNotIn(proc, registry)

    async def test_wrapper_waits_child_task_before_cancel_terminal(self) -> None:
        """并行分支取消收尾事件必须发生在 run_completed(cancelled) 之前。"""
        run = engine.Run("r_child", "company", {}, "manual", None)
        manager = engine.Engine()
        child_started = asyncio.Event()

        async def child() -> None:
            child_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await run.bus.publish(run.run_id, "step_failed", step_id="market_context_update", payload={})
                raise

        async def pipeline(_run: engine.Run) -> str:
            _run.track_child_task(asyncio.create_task(child()))
            await child_started.wait()
            await asyncio.Event().wait()
            return "completed"

        task = asyncio.create_task(manager._run_wrapper(run, pipeline))
        await child_started.wait()
        task.cancel()
        await task
        self.assertEqual(run.bus.events[-1]["type"], "run_completed")
        self.assertEqual(run.bus.events[-1]["payload"]["status"], "cancelled")
        self.assertEqual(len(run.child_tasks), 0)

    async def test_workspace_lease_rejects_alias_and_releases_on_cancel(self) -> None:
        """公司名与股票代码经只读 audit 归一后共享租约，取消完成后可再次创建。"""
        state = _minimal_state(valuation="ready", formal="ready")
        manager = engine.Engine()
        blocker = asyncio.Event()

        async def pipeline(_run: engine.Run) -> str:
            await blocker.wait()
            return "completed"

        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "RUNS_DIR", Path(tmp)), patch(
            "research_console.engine.state_reader.run_audit", new=AsyncMock(return_value=(state, 0, ""))
        ), patch("research_console.engine._company_pipeline", new=pipeline):
            first = await manager.create_run_checked(
                "company", {"target": "贵州茅台", "report_year": "2025"}, "manual"
            )
            with self.assertRaises(engine.WorkspaceLeaseConflict) as caught:
                await manager.create_run_checked(
                    "company", {"stock_code": "600519", "report_year": "2025"}, "manual"
                )
            self.assertEqual(caught.exception.run_id, first.run_id)
            with self.assertRaises(engine.WorkspaceLeaseConflict):
                await manager.create_run_checked(
                    "company", {"stock_code": "600519", "report_year": "2024"}, "manual"
                )
            self.assertTrue(await manager.cancel_run(first.run_id))
            await asyncio.gather(first.task, return_exceptions=True)
            self.assertEqual(first.status, "cancelled")

            blocker.set()
            second = await manager.create_run_checked(
                "company", {"stock_code": "600519", "report_year": "2025"}, "manual"
            )
            await second.task
            self.assertEqual(second.status, "completed")

    async def test_health_cli_probe_runs_off_event_loop(self) -> None:
        """慢速 claude --version 探测期间，事件循环中的其他协程仍应按时运行。"""
        def slow_version() -> str:
            time.sleep(0.08)
            return "test-version"

        async def ticker(started: float) -> float:
            await asyncio.sleep(0.01)
            return time.perf_counter() - started

        with patch("research_console.app._claude_cli_version", side_effect=slow_version), patch.object(
            config, "bocha_key_present", return_value=False
        ), patch.object(config, "missing_scripts", return_value=[]):
            started = time.perf_counter()
            payload, tick_elapsed = await asyncio.gather(console_app.health(), ticker(started))
        self.assertLess(tick_elapsed, 0.05)
        self.assertEqual(payload["claude_cli_version"], "test-version")

    async def test_preexisting_llm_artifacts_do_not_complete_pending_step(self) -> None:
        """pending LLM 步骤必须看到文件在启动后变化，旧四件套不能立即冒充新产物。"""
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.json"
            second = Path(tmp) / "b.md"
            first.write_text("old", encoding="utf-8")
            second.write_text("old", encoding="utf-8")
            groups = [[str(first), str(second)]]
            baseline = {path: engine._artifact_signature(path) for path in groups[0]}
            self.assertIsNone(engine._complete_group(groups, baseline))

            manager = engine.Engine()
            run = engine.Run("r_stale", "company", {}, "manual", None)
            run.llm_artifact_groups["formal_financial_analysis"] = groups
            run.llm_artifact_baselines["formal_financial_analysis"] = baseline
            manager.runs[run.run_id] = run
            ok, missing = manager.manual_complete(run.run_id, "formal_financial_analysis", force=False)
            self.assertFalse(ok)
            self.assertEqual(set(missing), {str(first), str(second)})

            first.write_text("new-content", encoding="utf-8")
            second.write_text("new-content", encoding="utf-8")
            self.assertEqual(engine._complete_group(groups, baseline), groups[0])

    async def test_manual_skip_only_accepts_registered_consumer(self) -> None:
        """未知、display-only 和无消费者步骤应拒绝；注册窗口内步骤只接受一次。"""
        manager = engine.Engine()
        run = engine.Run("r_skip", "company", {}, "manual", None)
        manager.runs[run.run_id] = run
        self.assertFalse(manager.manual_skip(run.run_id, "unknown"))
        self.assertFalse(manager.manual_skip(run.run_id, "audit"))
        run.skip_accepting_steps.add("processor_parse")
        self.assertTrue(manager.manual_skip(run.run_id, "processor_parse"))
        self.assertFalse(manager.manual_skip(run.run_id, "processor_parse"))
        self.assertEqual(run.manual_signals["processor_parse"], "skip")

    async def test_degraded_step_makes_legacy_delivery_partial(self) -> None:
        """降级完成必须汇总为 partial，干净链路仍保持 completed。"""
        run = engine.Run("r_degraded", "company", {}, "manual", None)
        pipeline = engine._CompanyPipeline(run)
        self.assertEqual(pipeline._final_status(), "completed")
        pipeline._mark("processor_compare", "degraded")
        self.assertEqual(pipeline._final_status(), "partial")


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
        self.assertIn("同时提供", payload["error"])
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
        self.assertIn("演示数据", summary["one_line_conclusion"])
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
        self.assertTrue(any("冻结 decision price" in item for item in pair["basis_warnings"]))
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
            self.assertTrue(any("快照冻结失败" in item for item in summary["gaps"]))
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
        self.assertIn("当时结论 / 现在回看", html)
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


if __name__ == "__main__":
    unittest.main()
