"""Python 强约束 agent 层单元测试（生产注册表）。

测试范围（零网络、零真实 Claude 进程）：
- 注册表只暴露正式财务 / 估值两个生产 agent；
- 未知 agent 拒绝；
- 槽位绑定与 WorkerTask 编译；
- prepare_invocation 不覆盖已有非 PLACEHOLDER 输出。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_console.agent_runtime import (
    AGENT_REGISTRY,
    AgentInvocation,
    build_worker_task,
    get_agent,
    list_agents,
    prepare_invocation,
    write_json,
)


class AgentRegistryTest(unittest.TestCase):
    """注册表是 Python 拥有的权威清单。"""

    def test_list_agents_exposes_production_agents_only(self) -> None:
        """当前主流程只注册正式财务与估值两个 LLM worker。"""

        items = list_agents()
        ids = {item["agent_id"] for item in items}
        self.assertEqual(ids, {"formal_financial_analyst", "company_valuation_analyst"})
        for item in items:
            self.assertTrue(item["inputs"])
            self.assertTrue(item["outputs"])
            self.assertEqual(item["allowed_tools"], ["Read", "Edit"])
            self.assertIsNone(AGENT_REGISTRY[item["agent_id"]].fixture_builder)

    def test_unknown_agent_is_rejected(self) -> None:
        """未知 agent_id 必须硬失败，禁止静默落到通用 agent。"""

        with self.assertRaises(KeyError):
            get_agent("not-a-real-agent")


class AgentContractCompileTest(unittest.TestCase):
    """路径绑定与 WorkerTask 编译。"""

    def test_missing_input_slot_fails_before_worker(self) -> None:
        """缺输入槽位时在编译阶段失败，而不是启动 Claude。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = get_agent("formal_financial_analyst")
            inv = AgentInvocation(
                agent_id="formal_financial_analyst",
                cwd=root,
                input_paths={"research_state": root / "state.json"},
                output_paths={
                    "formal_json": root / "formal.json",
                    "formal_md": root / "formal.md",
                },
                mcp_config=root / "empty_mcp.json",
            )
            with self.assertRaises(ValueError):
                build_worker_task(spec, inv)

    def test_build_worker_task_uses_registry_agent_name(self) -> None:
        """编译后的 WorkerTask 必须钉死 agent_id 与声明路径顺序。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "research_state.json"
            filing = root / "filing_set.json"
            formal_json = root / "formal_financial_analysis.json"
            formal_md = root / "formal_financial_analysis.md"
            write_json(state, {"layers": {}, "target": {"stock_code": "600519"}})
            write_json(filing, {"source_filings": []})
            mcp = root / "empty_mcp.json"
            write_json(mcp, {"mcpServers": {}})
            inv = AgentInvocation(
                agent_id="formal_financial_analyst",
                cwd=root,
                input_paths={"research_state": state, "filing_set": filing},
                output_paths={"formal_json": formal_json, "formal_md": formal_md},
                context={"stock_code": "600519"},
                mcp_config=mcp,
                tool_restriction="permission",
            )
            task = build_worker_task(get_agent("formal_financial_analyst"), inv)
            self.assertEqual(task.agent_name, "formal_financial_analyst")
            self.assertEqual(task.input_paths, (state, filing))
            self.assertEqual(task.output_paths, (formal_json, formal_md))
            self.assertEqual(task.allowed_tools, ("Read", "Edit"))
            self.assertIn("financial-analyst", task.agent_definition["prompt"])


class PrepareInvocationTest(unittest.TestCase):
    """输出预创建行为。"""

    def test_prepare_invocation_does_not_clobber_real_output(self) -> None:
        """输出已有非 PLACEHOLDER 内容时不得被预创建覆盖。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "research_state.json"
            filing = root / "filing_set.json"
            formal_json = root / "formal.json"
            formal_md = root / "formal.md"
            write_json(state, {"layers": {}, "target": {}})
            write_json(filing, {})
            formal_json.write_text('{"already": true}\n', encoding="utf-8")
            formal_md.write_text("# existing\n", encoding="utf-8")
            inv = AgentInvocation(
                agent_id="formal_financial_analyst",
                cwd=root,
                input_paths={"research_state": state, "filing_set": filing},
                output_paths={"formal_json": formal_json, "formal_md": formal_md},
                mcp_config=root / "empty_mcp.json",
                precreate_outputs=True,
            )
            prepare_invocation(get_agent("formal_financial_analyst"), inv)
            self.assertEqual(formal_json.read_text(encoding="utf-8"), '{"already": true}\n')
            self.assertEqual(formal_md.read_text(encoding="utf-8"), "# existing\n")


if __name__ == "__main__":
    unittest.main()
