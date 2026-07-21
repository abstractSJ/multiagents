"""受限 Claude Code worker 运行时单元测试。

测试范围（零网络、零真实 Claude 进程）：
- 命令构建：隔离旗标、自定义 agent 选择、工具白名单、内联定义打码；
- 流解析：工具调用与入参提取、MCP/result 捕获、坏行容错；
- 契约校验：合法 Read+Edit 通过、越权工具/路径与合同外文件失败、
  只读任务（无输出文件）禁止任何写类调用；
- 进程封装：mock subprocess 验证"先取指纹、后校验输出"的顺序性。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_console.worker_runtime import (
    WorkerTask,
    build_worker_command,
    is_api_request_rejected,
    json_output_validator,
    parse_worker_stream,
    run_worker,
    scrub_command,
    validate_worker_contract,
)


def _hash(path: Path) -> str:
    """计算测试文件的 SHA-256 指纹。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stream(*, input_path: str, tool: str = "Edit", tool_path: str = "") -> str:
    """构造一段最小的合法 stream-json：init → Read+一个写类调用 → result。"""
    return "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": input_path}},
                            {"type": "tool_use", "name": tool, "input": {"file_path": tool_path}},
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "is_error": False, "permission_denials": []}),
        ]
    )


class WorkerRuntimeCommandTest(unittest.TestCase):
    """验证 worker 命令只暴露显式声明的运行时边界。"""

    def test_command_isolated_and_redacts_inline_definition(self) -> None:
        """命令必须禁用继承状态、选中单个 custom agent，且日志版打码定义。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = WorkerTask(
                agent_name="collector_worker",
                agent_definition={"description": "test", "prompt": "bounded"},
                prompt="Do one task.",
                cwd=root,
                input_paths=(root / "input.json",),
                output_paths=(root / "output.json",),
                mcp_config=root / "empty_mcp.json",
            )
            command = build_worker_command(task)
            self.assertIn("--bare", command)
            self.assertIn("--no-session-persistence", command)
            self.assertIn("--strict-mcp-config", command)
            self.assertEqual(command[command.index("--agent") + 1], "collector_worker")
            self.assertEqual(command[command.index("--tools") + 1 : command.index("--allowedTools")], ["Read", "Edit"])
            # stream-json 必须与 --verbose 成对出现，否则 CLI 直接报错退出。
            self.assertIn("--verbose", command)
            self.assertEqual(
                scrub_command(command)[command.index("--agents") + 1],
                "<inline-agent-definition>",
            )

    def test_permission_mode_keeps_standard_tool_array(self) -> None:
        """permission 模式不得裁剪 API 工具数组，只保留客户端 allow 白名单。

        本机第三方网关按请求形状放行：任何 ``--tools``/``--disallowedTools``
        产生的非标准工具数组都会被 429 拒绝，permission 模式是代理环境下的
        唯一可用实施层。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = WorkerTask(
                agent_name="collector_worker",
                agent_definition={"description": "test", "prompt": "bounded"},
                prompt="Do one task.",
                cwd=root,
                input_paths=(root / "input.json",),
                output_paths=(root / "output.json",),
                mcp_config=root / "empty_mcp.json",
                tool_restriction="permission",
            )
            command = build_worker_command(task)
            self.assertNotIn("--tools", command)
            self.assertNotIn("--disallowedTools", command)
            self.assertEqual(command[command.index("--allowedTools") + 1 : command.index("--permission-mode")], ["Read", "Edit"])
            with self.assertRaises(ValueError):
                build_worker_command(
                    WorkerTask(
                        agent_name="collector_worker",
                        agent_definition={},
                        prompt="x",
                        cwd=root,
                        input_paths=(),
                        output_paths=(),
                        mcp_config=root / "empty_mcp.json",
                        tool_restriction="unknown-mode",
                    )
                )

    def test_api_request_rejection_detection(self) -> None:
        """只有"结果报错 + 429 + 零工具调用"才允许判定为请求级拒绝。"""
        rejected = parse_worker_stream(
            json.dumps({"type": "result", "is_error": True, "api_error_status": 429}),
            1,
        )
        self.assertTrue(is_api_request_rejected(rejected))
        # worker 已经执行过工具的失败不是请求级拒绝，不得触发实施层重试。
        worked_then_failed = parse_worker_stream(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]},
                        }
                    ),
                    json.dumps({"type": "result", "is_error": True, "api_error_status": 429}),
                ]
            ),
            1,
        )
        self.assertFalse(is_api_request_rejected(worked_then_failed))
        ordinary_failure = parse_worker_stream(
            json.dumps({"type": "result", "is_error": True, "api_error_status": None}),
            1,
        )
        self.assertFalse(is_api_request_rejected(ordinary_failure))

    def test_stream_parser_extracts_tools_mcp_and_result(self) -> None:
        """解析器必须保留工具入参、忽略坏行且不抛异常。"""
        stdout = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}),
                "not-json",
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "tool_use", "name": "Read", "input": {"file_path": "input.json"}},
                                {"type": "tool_use", "name": "Edit", "input": {"file_path": "output.json"}},
                            ]
                        },
                    }
                ),
                json.dumps({"type": "result", "is_error": False, "permission_denials": []}),
            ]
        )
        trace = parse_worker_stream(stdout, 0)
        self.assertEqual(trace.tool_names, ["Read", "Edit"])
        self.assertEqual(trace.tool_inputs[0]["file_path"], "input.json")
        self.assertEqual(trace.mcp_servers, [])
        self.assertFalse(trace.result_event["is_error"])
        # 坏行保留在原始行审计里，但不进入结构化事件。
        self.assertEqual(len(trace.lines), 4)
        self.assertEqual(len(trace.events), 3)


class WorkerRuntimeValidationTest(unittest.TestCase):
    """验证 Python 侧独立否决不安全或语义无效的 worker 结果。"""

    def _task(self, root: Path, *, with_output: bool = True) -> WorkerTask:
        """在临时目录里构建一个 Read(+Edit) 契约与配套文件。"""
        input_file = root / "input.json"
        input_file.write_text('{"value": 1}\n', encoding="utf-8")
        (root / "empty_mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
        outputs: tuple[Path, ...] = ()
        if with_output:
            output_file = root / "output.json"
            # 当前 CLI 无独立 Write 工具：协调器先写占位，再让 worker 用 Edit 替换。
            output_file.write_text("PLACEHOLDER\n", encoding="utf-8")
            outputs = (output_file,)
        return WorkerTask(
            agent_name="collector_worker",
            agent_definition={"description": "test", "prompt": "bounded"},
            prompt="Do one task.",
            cwd=root,
            input_paths=(input_file,),
            output_paths=outputs,
            mcp_config=root / "empty_mcp.json",
        )

    def test_exact_json_validator_and_workspace_change_check_pass(self) -> None:
        """合法的 Read+Edit 结果应通过协调器的独立校验闸门。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self._task(root)
            input_hashes = {task.input_paths[0]: _hash(task.input_paths[0])}
            before = {
                "input.json": input_hashes[task.input_paths[0]],
                "output.json": _hash(task.output_paths[0]),
                "empty_mcp.json": _hash(task.mcp_config),
            }
            # 模拟 worker 的 Edit 副作用：占位符被替换为要求的 JSON。
            task.output_paths[0].write_text('{"status": "ready"}\n', encoding="utf-8")
            trace = parse_worker_stream(
                _stream(input_path=str(task.input_paths[0]), tool_path=str(task.output_paths[0])),
                0,
            )
            validation = validate_worker_contract(
                task,
                trace,
                input_hashes,
                before,
                output_validator=json_output_validator({"status": "ready"}),
            )
            self.assertTrue(validation.passed, validation.errors)

    def test_reading_declared_output_before_edit_is_allowed(self) -> None:
        """Edit 工具强制先 Read：读声明输出合法，但漏读声明输入必须失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self._task(root)
            input_hashes = {task.input_paths[0]: _hash(task.input_paths[0])}
            before = {
                "input.json": input_hashes[task.input_paths[0]],
                "output.json": _hash(task.output_paths[0]),
                "empty_mcp.json": _hash(task.mcp_config),
            }
            task.output_paths[0].write_text('{"status": "ready"}\n', encoding="utf-8")

            def trace_with_reads(read_files: list[Path]):
                """构造 Read 序列 + 一次合法 Edit 的轨迹。"""
                blocks = [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": str(path)}}
                    for path in read_files
                ]
                blocks.append({"type": "tool_use", "name": "Edit", "input": {"file_path": str(task.output_paths[0])}})
                return parse_worker_stream(
                    "\n".join(
                        [
                            json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}),
                            json.dumps({"type": "assistant", "message": {"content": blocks}}),
                            json.dumps({"type": "result", "is_error": False, "permission_denials": []}),
                        ]
                    ),
                    0,
                )

            both = validate_worker_contract(
                task,
                trace_with_reads([task.input_paths[0], task.output_paths[0]]),
                input_hashes,
                before,
                output_validator=json_output_validator({"status": "ready"}),
            )
            self.assertTrue(both.passed, both.errors)

            # 只读输出、从未读输入：worker 不可能基于证据完成任务，必须失败。
            output_only = validate_worker_contract(
                task,
                trace_with_reads([task.output_paths[0]]),
                input_hashes,
                before,
                output_validator=json_output_validator({"status": "ready"}),
            )
            self.assertFalse(output_only.passed)
            self.assertFalse(output_only.checks["read_path_contract"])

    def test_unexpected_file_and_forbidden_tool_fail(self) -> None:
        """越权工具、越权路径与合同外新增文件都必须判失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self._task(root)
            input_hashes = {task.input_paths[0]: _hash(task.input_paths[0])}
            before = {
                "input.json": input_hashes[task.input_paths[0]],
                "output.json": _hash(task.output_paths[0]),
                "empty_mcp.json": _hash(task.mcp_config),
            }
            (root / "unexpected.txt").write_text("unexpected", encoding="utf-8")
            trace = parse_worker_stream(
                _stream(input_path=str(task.input_paths[0]), tool="Bash", tool_path=str(root / "secret.txt")),
                0,
            )
            validation = validate_worker_contract(task, trace, input_hashes, before)
            self.assertFalse(validation.passed)
            self.assertFalse(validation.checks["only_allowed_tools_called"])
            self.assertFalse(validation.checks["write_path_contract"])
            self.assertFalse(validation.checks["no_unexpected_workspace_changes"])
            self.assertIn("unexpected.txt", validation.observed["created_files"])

    def test_read_only_worker_must_not_write(self) -> None:
        """未声明输出文件的只读任务：纯 Read 通过，任何 Edit 都判失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self._task(root, with_output=False)
            input_hashes = {task.input_paths[0]: _hash(task.input_paths[0])}
            before = {
                "input.json": input_hashes[task.input_paths[0]],
                "empty_mcp.json": _hash(task.mcp_config),
            }
            read_only = parse_worker_stream(
                "\n".join(
                    [
                        json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "name": "Read",
                                            "input": {"file_path": str(task.input_paths[0])},
                                        }
                                    ]
                                },
                            }
                        ),
                        json.dumps({"type": "result", "is_error": False, "permission_denials": []}),
                    ]
                ),
                0,
            )
            passing = validate_worker_contract(task, read_only, input_hashes, before)
            self.assertTrue(passing.passed, passing.errors)

            sneaky = parse_worker_stream(
                _stream(input_path=str(task.input_paths[0]), tool_path=str(root / "sneaky.json")),
                0,
            )
            failing = validate_worker_contract(task, sneaky, input_hashes, before)
            self.assertFalse(failing.passed)
            self.assertFalse(failing.checks["write_path_contract"])


class WorkerRuntimeExecutionTest(unittest.TestCase):
    """在不依赖真实 Claude 账户的前提下验证 subprocess 封装。"""

    def test_run_worker_captures_and_validates_mocked_process(self) -> None:
        """封装必须在执行前取输入指纹、在执行后校验输出与命令结构。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = WorkerTask(
                agent_name="collector_worker",
                agent_definition={"description": "test", "prompt": "bounded"},
                prompt="Do one task.",
                cwd=root,
                input_paths=(root / "input.json",),
                output_paths=(root / "output.json",),
                mcp_config=root / "empty_mcp.json",
            )
            task.input_paths[0].write_text('{"value": 1}\n', encoding="utf-8")
            task.output_paths[0].write_text("PLACEHOLDER\n", encoding="utf-8")
            task.mcp_config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            stdout = _stream(input_path=str(task.input_paths[0]), tool_path=str(task.output_paths[0]))

            def fake_run(*args, **kwargs):
                """模拟 worker 的 Edit 副作用后返回 stream-json。"""
                task.output_paths[0].write_text('{"status": "ready"}\n', encoding="utf-8")
                return type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": 0})()

            with patch("research_console.worker_runtime.subprocess.run", side_effect=fake_run):
                result = run_worker(
                    task,
                    output_validator=json_output_validator({"status": "ready"}),
                )
            self.assertTrue(result.passed, result.validation.errors)
            self.assertEqual(result.trace.tool_names, ["Read", "Edit"])
            self.assertEqual(result.command[result.command.index("--agent") + 1], "collector_worker")
            # 诊断报告不得包含内联 agent 定义原文。
            report = result.to_dict()
            self.assertIn("<inline-agent-definition>", report["command"])


if __name__ == "__main__":
    unittest.main()
