"""公司研究 Python 主会话协调器单元测试（零真实 Claude / 零重脚本）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_console.company_research_coordinator import (
    _collect_evidence_paths,
    _plan_map,
    run_company_research,
)
from research_console import steps


def _minimal_ready_state(stock: str = "601138", as_of: str = "2026-05-01") -> dict:
    """构造“除估值外大多可复用”的 research_state。"""

    return {
        "schema_version": "test",
        "target": {"stock_code": stock, "company_name": "Test Co", "report_year": "2025"},
        "request": {"stock_code": stock, "as_of_date": as_of, "filing_policy": "recent_history"},
        "financial_input_fingerprint": "abc123",
        "reusable": {
            "collector": True,
            "processor": True,
            "financial_evidence_draft": True,
            "formal_financial_analysis": True,
            "valuation": False,
            "market_context": True,
        },
        "layers": {
            "collector": {"status": "ready", "artifacts": {}},
            "processor": {"status": "ready", "artifacts": {}},
            "financial_evidence_draft": {
                "status": "ready",
                "artifacts": {
                    "filing_set_json": {
                        "path": f"/tmp/{stock}/filing_set.json",
                        "exists": True,
                    }
                },
            },
            "formal_financial_analysis": {
                "status": "ready",
                "artifacts": {
                    "formal_financial_analysis_json": {
                        "path": f"/tmp/{stock}/formal_financial_analysis.json",
                        "exists": True,
                    }
                },
            },
            "market_context": {
                "status": "ready",
                "artifacts": {
                    "market_context_package_json": {
                        "path": f"/tmp/{stock}/market_context_package.json",
                        "exists": True,
                    }
                },
            },
            "valuation": {
                "status": "partial",
                "artifacts": {},
            },
        },
        "next_actions": [
            {
                "step": "valuation_update",
                "owner": "valuation-analyst",
                "reason": "incomplete",
            }
        ],
        "skipped_actions": [],
    }


class PlanIntegrationTest(unittest.TestCase):
    """计划仍走原 steps.build_company_plan。"""

    def test_plan_marks_valuation_pending_when_not_reusable(self) -> None:
        """valuation 不可复用时应 pending。"""

        state = _minimal_ready_state()
        plan = steps.build_company_plan(state, force_refresh=False, llm_mode="claude_cli")
        plan_map = _plan_map(plan)
        self.assertEqual(plan_map["valuation_update"]["status"], "pending")
        self.assertEqual(plan_map["collector_fetch"]["status"], "skipped")
        self.assertEqual(plan_map["formal_financial_analysis"]["status"], "skipped")


class CoordinatorDryRunTest(unittest.TestCase):
    """mock audit：全复用时不应拉起脚本或 agent。"""

    def test_all_reusable_ends_reused_without_agents(self) -> None:
        """各层 reusable 时 final_status=reused。"""

        state = _minimal_ready_state()
        state["reusable"]["valuation"] = True
        state["layers"]["valuation"]["status"] = "ready"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch(
                "research_console.company_research_coordinator.run_research_audit",
                return_value=state,
            ):
                result = run_company_research(
                    {
                        "stock_code": "601138",
                        "as_of_date": "2026-05-01",
                    },
                    workspace=workspace,
                    claude_bin="claude-fake",
                    run_scripts=True,
                    run_llm_agents=True,
                )
            self.assertEqual(result.final_status, "reused")
            self.assertTrue((workspace / "company_research_report.json").exists())
            agent_steps = [s for s in result.steps if s.kind == "llm_agent"]
            self.assertTrue(all(s.status == "skipped" for s in agent_steps))

    def test_event_sink_receives_plan_ready(self) -> None:
        """event_sink 应收到 plan_ready，供控制台 SSE 实时展示。"""

        state = _minimal_ready_state()
        state["reusable"]["valuation"] = True
        state["layers"]["valuation"]["status"] = "ready"
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch(
                "research_console.company_research_coordinator.run_research_audit",
                return_value=state,
            ):
                run_company_research(
                    {"stock_code": "601138", "as_of_date": "2026-05-01"},
                    workspace=workspace,
                    claude_bin="claude-fake",
                    event_sink=events.append,
                )
        types = [item.get("type") for item in events]
        self.assertIn("plan_ready", types)
        self.assertIn("coordinator_message", types)
        self.assertTrue(any(t in {"step_completed", "step_skipped"} for t in types))


class ConsoleRoutingTest(unittest.TestCase):
    """控制台 llm_mode 路由到 Python 协调器。"""

    def test_llm_modes_include_python_agent_coordinator(self) -> None:
        """API 允许的 llm_mode 必须包含 python_agent_coordinator。"""

        from research_console import engine

        self.assertIn("python_agent_coordinator", engine.LLM_MODES)
        self.assertEqual(
            __import__("research_console.config", fromlist=["DEFAULT_COMPANY_LLM_MODE"]).DEFAULT_COMPANY_LLM_MODE,
            "python_agent_coordinator",
        )

    def test_company_pipeline_routes_python_mode(self) -> None:
        """company pipeline 在 python 模式下选择 Python 执行器。"""

        from research_console import engine

        self.assertTrue(hasattr(engine, "_PythonAgentCoordinatorPipeline"))
        # 源码路由断言：避免真的拉起完整 run。
        import inspect

        src = inspect.getsource(engine._company_pipeline)
        self.assertIn('run.llm_mode == "python_agent_coordinator"', src)
        self.assertIn("_PythonAgentCoordinatorPipeline", src)


if __name__ == "__main__":
    unittest.main()
