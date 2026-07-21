"""受限 Claude Code worker 运行时。

本模块是"Python 显式协调器 + 受限 LLM worker"架构里的进程边界：
- Python 侧完全拥有任务定义、可读输入、可写输出、工具白名单、MCP 配置、
  进程生命周期和事后校验；
- Claude Code 只被当作一个不可信的执行体，完成一次有边界的具体任务；
- worker 的最终自然语言回复永远不作为完成依据，一切以文件与流事件为准。

本模块刻意不做研究链路调度、不创建其他 agent、不理解领域语义；这些职责
属于调用它的显式协调器。领域输出校验通过 ``output_validator`` 回调注入。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class WorkerTask:
    """描述一次受限 worker 调用的完整契约。

    参数：
        agent_name: Claude Code 通过 ``--agent`` 选中的内联 custom agent 名。
        agent_definition: 通过 ``--agents`` 传入的 JSON 定义。定义正文应重复
            文件与工具契约，因为它就是 worker 的局部系统指令。
        prompt: 通过 ``-p`` 发送的简短任务消息。
        cwd: worker 的隔离工作目录（同时是"新增文件归因"扫描根）。
        input_paths: 声明的必选输入文件。执行前记录指纹、执行后校验未被修改；
            在 ``require_all_inputs_read=True`` 时每个都必须被 Read。
        output_paths: 允许创建或编辑的文件。为空表示该 worker 是只读任务。
        mcp_config: 显式 MCP 配置。生产任务应指向空配置或窄化配置，
            而不是继承用户全局 MCP 注册表。
        allowed_tools: 暴露给 worker 的内置工具白名单。当前 Claude Code
            print 模式实测只有 Bash/Edit/Read 三个内置工具，没有独立 Write；
            需要新建产物时由协调器预创建占位文件再让 worker 用 Edit 替换。
        disallowed_tools: 额外显式禁用的工具，便于命令行审计。
        tool_restriction: 工具限制的实施层。
            - ``request``：默认。通过 ``--tools``/``--disallowedTools`` 直接
              裁剪 API 请求里的工具数组，禁用工具对模型完全不可见，隔离最强；
              适用于直连 Anthropic API 的环境。
            - ``permission``：不改写请求工具数组（保持标准 Claude Code 请求
              形状），只用 ``--allowedTools`` + ``dontAsk`` 在客户端权限层自动
              拦截。禁用工具对模型仍然可见，但任何调用都会被拒绝并以
              permission_denial 形式出现在 result 事件里，从而被校验判失败。
              适用于按请求形状做白名单的第三方 API 网关——实测本机代理会对
              任何裁剪过工具数组的请求返回 429 "Request rejected"。
        claude_bin: Claude Code 可执行文件路径。Windows 上应传 npm 包内的
            原生 claude.exe，避免 .cmd shim 造成进程失控。
        timeout_seconds: Python 拥有的外层进程超时。
        max_budget_usd: 单次 worker 的 Claude 预算上限。
        permission_mode: 权限模式。``dontAsk`` 保证无人值守 worker 不会
            卡在交互式授权上。
        require_no_extra_files: 是否把"合同外新增/修改/删除文件"判为失败。
        readable_paths: 额外允许 Read 的证据文件（不必全部读取）。用于真实
            财务/估值任务：digest、RAG、filing_set 等可选证据。
        require_all_inputs_read: 是否要求 ``input_paths`` 中每个文件都被 Read。
            合成微任务为 True；真实研究任务证据面宽，通常为 False，只要求
            Read 集合落在 input∪readable∪output 内。
    返回值：
        不可变 dataclass 实例。
    """

    agent_name: str
    agent_definition: dict[str, Any]
    prompt: str
    cwd: Path
    input_paths: tuple[Path, ...]
    output_paths: tuple[Path, ...]
    mcp_config: Path
    allowed_tools: tuple[str, ...] = ("Read", "Edit")
    disallowed_tools: tuple[str, ...] = (
        "Bash",
        "Write",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "Task",
        "Agent",
    )
    tool_restriction: str = "request"
    claude_bin: str = "claude"
    model: str | None = None
    timeout_seconds: int = 600
    max_budget_usd: float = 1.0
    permission_mode: str = "dontAsk"
    require_no_extra_files: bool = True
    readable_paths: tuple[Path, ...] = ()
    require_all_inputs_read: bool = True


@dataclass
class WorkerTrace:
    """保存原始 worker 事件流与独立观测到的关键事实。

    参数：
        return_code: 进程退出码；超时使用 -2。
        lines: 原始 stdout 非空行（含无法解析的坏行，便于审计）。
        events: 成功解析的顶层 stream-json 事件。
        tool_names: 按发生顺序记录的 tool_use 名称。
        tool_inputs: 与 tool_names 一一对应的工具入参。
        mcp_servers: init 事件里声明加载的 MCP 服务器。
        permission_denials: result 事件里报告的权限拒绝。
        result_event: 顶层 result 事件；未收到时为空字典。
        stderr: 进程 stderr 文本。
    返回值：
        dataclass 实例。
    """

    return_code: int
    lines: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    tool_inputs: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    permission_denials: list[Any] = field(default_factory=list)
    result_event: dict[str, Any] = field(default_factory=dict)
    stderr: str = ""


@dataclass
class WorkerValidation:
    """协调器对一次 worker 调用的最终裁决。

    参数：
        passed: 全部检查通过且无错误。
        checks: 各检查项的布尔结果。
        errors: 人类可读错误列表（英文，可能进入控制台事件）。
        observed: 独立观测到的路径、工具、文件变化等事实。
    返回值：
        dataclass 实例。
    """

    passed: bool
    checks: dict[str, bool]
    errors: list[str]
    observed: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """返回可 JSON 序列化的校验报告。"""

        return asdict(self)


@dataclass
class WorkerRunResult:
    """把进程输出、事件轨迹与校验结论作为一个不可拆分的边界返回。"""

    task: WorkerTask
    trace: WorkerTrace
    validation: WorkerValidation
    command: list[str]

    @property
    def passed(self) -> bool:
        """worker 是否满足全部已配置检查。"""

        return self.validation.passed

    def to_dict(self) -> dict[str, Any]:
        """返回不含内联 agent 定义原文的 JSON 诊断报告。"""

        return {
            "command": scrub_command(self.command),
            "validation": self.validation.to_dict(),
            "observed": {
                "return_code": self.trace.return_code,
                "event_count": len(self.trace.events),
                "tool_names": self.trace.tool_names,
                "mcp_servers": self.trace.mcp_servers,
                "permission_denials": self.trace.permission_denials,
                "result_event": self.trace.result_event,
                "stderr": self.trace.stderr,
            },
        }


def _json_arg(value: Any) -> str:
    """把内联 Claude 参数序列化为紧凑且确定的 JSON 字符串。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_worker_command(task: WorkerTask) -> list[str]:
    """构建一条非交互、显式受限的 Claude Code 命令。

    功能：
        命令永远携带 ``--bare``、``--no-session-persistence`` 与
        ``--strict-mcp-config`` + 显式 MCP 配置文件。这样受限任务不会
        静默继承父项目的 CLAUDE.md、skills、hooks、历史会话或 MCP 注册表；
        隔离本身成为可在命令行上审计的显式条件，而不是默认假设。
        工具限制按 ``task.tool_restriction`` 在请求层或权限层实施，
        两种模式下事后校验的判据完全相同。
    参数：
        task: worker 契约。
    返回值：
        subprocess 参数列表（不经 shell）。
    """

    if not task.allowed_tools:
        raise ValueError("A bounded worker must expose at least one allowed tool")
    if task.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if task.max_budget_usd <= 0:
        raise ValueError("max_budget_usd must be positive")
    if task.tool_restriction not in {"request", "permission"}:
        raise ValueError(f"Unknown tool_restriction mode: {task.tool_restriction!r}")

    # request 模式直接改写 API 请求的工具数组；permission 模式保持默认数组
    # （标准请求形状），只在客户端权限层自动放行白名单、拒绝其余调用。
    if task.tool_restriction == "request":
        tool_flags = [
            "--tools",
            *task.allowed_tools,
            "--allowedTools",
            *task.allowed_tools,
            "--disallowedTools",
            *task.disallowed_tools,
        ]
    else:
        tool_flags = ["--allowedTools", *task.allowed_tools]

    return [
        task.claude_bin,
        "--bare",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        str(task.mcp_config),
        "--agent",
        task.agent_name,
        "--agents",
        _json_arg({task.agent_name: task.agent_definition}),
        *tool_flags,
        "--permission-mode",
        task.permission_mode,
        "--max-budget-usd",
        f"{task.max_budget_usd:.2f}",
        # stream-json 在 print 模式下强制要求 --verbose，缺失会直接报错退出。
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
        task.prompt,
    ]


def parse_worker_stream(stdout: str, return_code: int, stderr: str = "") -> WorkerTrace:
    """解析 Claude Code stream-json，绝不信任最终自然语言回复。

    功能：
        坏行只保留在 ``lines`` 里供审计，不进入结构化视图，也不抛异常。
        协调器的文件与工具校验才是权威；worker 的"已完成"句子不能替代校验。
    参数：
        stdout: 进程标准输出全文。
        return_code: 进程退出码。
        stderr: 进程标准错误全文。
    返回值：
        WorkerTrace。
    """

    lines = [line for line in stdout.splitlines() if line.strip()]
    events: list[dict[str, Any]] = []
    tool_names: list[str] = []
    tool_inputs: list[dict[str, Any]] = []
    mcp_servers: list[dict[str, Any]] = []
    permission_denials: list[Any] = []
    result_event: dict[str, Any] = {}

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "system" and event.get("subtype") == "init":
            raw_mcp = event.get("mcp_servers")
            if isinstance(raw_mcp, list):
                mcp_servers.extend(item for item in raw_mcp if isinstance(item, dict))
        if event.get("type") == "result":
            result_event = event
            raw_denials = event.get("permission_denials")
            if isinstance(raw_denials, list):
                permission_denials.extend(raw_denials)
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_names.append(str(block.get("name") or ""))
            value = block.get("input")
            tool_inputs.append(value if isinstance(value, dict) else {})

    return WorkerTrace(
        return_code=return_code,
        lines=lines,
        events=events,
        tool_names=tool_names,
        tool_inputs=tool_inputs,
        mcp_servers=mcp_servers,
        permission_denials=permission_denials,
        result_event=result_event,
        stderr=stderr,
    )


def is_api_request_rejected(trace: WorkerTrace) -> bool:
    """判断一次失败是否为"API 网关在首个请求就拒绝、worker 没做任何事"。

    功能：
        按请求形状做白名单的第三方网关会对裁剪过工具数组的请求直接返回
        429 "Request rejected"，此时事件流里没有任何 tool_use，磁盘也没有
        变化。调用方可据此安全地用 ``tool_restriction="permission"`` 重试，
        而不会与"worker 干了一半失败"的情形混淆。
    参数：
        trace: 解析后的事件轨迹。
    返回值：
        是请求级拒绝返回 True。
    """

    result = trace.result_event
    return bool(
        result.get("is_error") is True
        and result.get("api_error_status") == 429
        and not trace.tool_names
    )


def _sha256(path: Path) -> str:
    """计算单个文件的 SHA-256 指纹，用于输入不可变检查。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_files(root: Path) -> dict[str, str]:
    """采集 worker 工作目录下全部文件指纹（相对路径 → SHA-256）。"""

    if not root.exists():
        return {}
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            relative = str(path.relative_to(root)).replace("\\", "/")
            snapshot[relative] = _sha256(path)
    return snapshot


def _normalize_path_key(path: str | Path) -> str:
    """把路径规范成可比较的键。

    Windows 上工具事件与 Python Path 的盘符大小写、斜杠方向、是否 resolve
    经常不一致；契约比较必须用同一规范，否则合法 Read 会被误判越界。
    """

    text = str(path).strip()
    if not text:
        return ""
    try:
        resolved = str(Path(text).resolve())
    except OSError:
        resolved = text
    return resolved.replace("\\", "/").casefold()


def _contract_path(tool_input: dict[str, Any]) -> str | None:
    """提取 Read/Edit/Write 类工具共用的路径字段。"""

    value = tool_input.get("file_path") or tool_input.get("path")
    return str(value) if value else None


def _paths_for_tool(trace: WorkerTrace, tool_name: str) -> list[str]:
    """返回某个工具名的全部观测路径。"""

    return [
        path
        for name, tool_input in zip(trace.tool_names, trace.tool_inputs)
        if name == tool_name
        for path in [_contract_path(tool_input)]
        if path is not None
    ]


def validate_worker_contract(
    task: WorkerTask,
    trace: WorkerTrace,
    input_hashes_before: dict[Path, str],
    files_before: dict[str, str],
    *,
    output_validator: Callable[[Path], tuple[bool, str | None]] | None = None,
) -> WorkerValidation:
    """独立校验进程、工具、路径、文件、MCP 与可选的领域输出。

    功能：
        校验完全基于流事件与磁盘事实，与 worker 的自述解耦：
        - 进程与 result 事件必须成功且无权限拒绝；
        - 工具调用必须落在白名单内且至少发生一次；
        - Read 路径必须落在"声明输入 ∪ 声明输出"内，且每个声明输入都被
          真正读过。允许读声明输出是因为 Claude Code 的 Edit 工具强制
          "先 Read 后 Edit"，读占位文件是合法且必要的动作；
        - Edit/Write 路径必须是 ``output_paths`` 的子集；声明无输出的只读
          任务不得有任何写类调用；
        - 输入文件指纹必须保持不变；
        - 工作目录内新增/修改/删除必须能归因到声明的输出文件；
        - 领域 schema 交给调用方注入的 ``output_validator``，因为运行时
          无法推断财务采集、digest、估值等各角色的产物契约。
    参数：
        task: worker 契约。
        trace: 解析后的事件轨迹。
        input_hashes_before: 执行前的输入指纹。
        files_before: 执行前的工作目录快照。
        output_validator: 单个输出文件的领域校验回调，返回 (是否有效, 错误)。
    返回值：
        WorkerValidation。
    """

    checks: dict[str, bool] = {}
    errors: list[str] = []
    allowed_tools = set(task.allowed_tools)
    expected_inputs = {_normalize_path_key(path) for path in task.input_paths}
    expected_outputs = {_normalize_path_key(path) for path in task.output_paths}
    optional_reads = {_normalize_path_key(path) for path in task.readable_paths}

    checks["process_exit_code_zero"] = trace.return_code == 0
    if not checks["process_exit_code_zero"]:
        errors.append(f"Claude Code exited with code {trace.return_code}.")

    checks["result_event_not_error"] = trace.result_event.get("is_error") is not True
    if not checks["result_event_not_error"]:
        errors.append("The final Claude result event reports an error.")

    checks["no_permission_denials"] = not trace.permission_denials
    if trace.permission_denials:
        errors.append(f"Permission denials were reported: {trace.permission_denials!r}")

    checks["only_allowed_tools_called"] = bool(trace.tool_names) and set(trace.tool_names).issubset(allowed_tools)
    if not checks["only_allowed_tools_called"]:
        errors.append(f"Unexpected or missing worker tools: {trace.tool_names!r}")

    checks["no_mcp_servers_loaded"] = not trace.mcp_servers
    if trace.mcp_servers:
        errors.append(f"MCP servers were loaded: {trace.mcp_servers!r}")

    read_paths = _paths_for_tool(trace, "Read")
    edit_paths = _paths_for_tool(trace, "Edit")
    write_paths = _paths_for_tool(trace, "Write")
    write_like_paths = edit_paths + write_paths
    # Read 合法范围 = 声明输入 ∪ 可选证据 readable_paths ∪ 声明输出。
    # 输出可读是因为 Edit 强制"先 Read 后 Edit"。
    # 合成微任务 require_all_inputs_read=True：每个 input 都必须读到。
    # 真实研究任务证据面宽，只要求读集合不越界，不强制读完所有证据。
    # 路径比较使用规范化键，避免 Windows 盘符/斜杠差异误杀合法 Read。
    readable = expected_inputs | optional_reads | expected_outputs
    read_keys = {_normalize_path_key(path) for path in read_paths}
    write_keys = {_normalize_path_key(path) for path in write_like_paths}
    within_contract = read_keys.issubset(readable)
    if task.require_all_inputs_read and expected_inputs:
        checks["read_path_contract"] = within_contract and expected_inputs.issubset(read_keys)
    else:
        checks["read_path_contract"] = within_contract and bool(read_paths)
    if not checks["read_path_contract"]:
        errors.append(f"Read paths violated the contract: {read_paths!r}")
    if expected_outputs:
        checks["write_path_contract"] = bool(write_like_paths) and write_keys.issubset(expected_outputs)
    else:
        checks["write_path_contract"] = not write_like_paths
    if not checks["write_path_contract"]:
        errors.append(f"Edit/Write paths were outside the contract: {write_like_paths!r}")

    checks["input_files_unchanged"] = True
    for path, before in input_hashes_before.items():
        if not path.exists():
            checks["input_files_unchanged"] = False
            errors.append(f"Input file disappeared: {path}")
        elif _sha256(path) != before:
            checks["input_files_unchanged"] = False
            errors.append(f"Worker modified an input file: {path}")

    files_after = _snapshot_files(task.cwd)
    created = sorted(set(files_after) - set(files_before))
    deleted = sorted(set(files_before) - set(files_after))
    modified = sorted(
        path for path in set(files_before) & set(files_after) if files_before[path] != files_after[path]
    )
    expected_output_rel = {
        str(path.relative_to(task.cwd)).replace("\\", "/")
        for path in task.output_paths
        if path.is_relative_to(task.cwd)
    }
    if task.require_no_extra_files:
        unexpected_created = sorted(set(created) - expected_output_rel)
        unexpected_modified = sorted(set(modified) - expected_output_rel)
        checks["no_unexpected_workspace_changes"] = not deleted and not unexpected_created and not unexpected_modified
        if deleted:
            errors.append(f"Worker deleted existing files: {deleted!r}")
        if unexpected_created:
            errors.append(f"Worker created unexpected files: {unexpected_created!r}")
        if unexpected_modified:
            errors.append(f"Worker modified unexpected files: {unexpected_modified!r}")
    else:
        checks["no_unexpected_workspace_changes"] = True

    checks["output_files_exist"] = all(path.exists() for path in task.output_paths)
    if not checks["output_files_exist"]:
        missing = [str(path) for path in task.output_paths if not path.exists()]
        errors.append(f"Required output files are missing: {missing!r}")

    if output_validator is None:
        checks["output_validation"] = checks["output_files_exist"]
    else:
        checks["output_validation"] = True
        for path in task.output_paths:
            if not path.exists():
                checks["output_validation"] = False
                continue
            valid, message = output_validator(path)
            if not valid:
                checks["output_validation"] = False
                errors.append(message or f"Output validator rejected {path}")

    return WorkerValidation(
        passed=not errors and all(checks.values()),
        checks=checks,
        errors=errors,
        observed={
            "read_paths": read_paths,
            "edit_paths": edit_paths,
            "write_paths": write_paths,
            "tool_names": trace.tool_names,
            "mcp_servers": trace.mcp_servers,
            "permission_denials": trace.permission_denials,
            "created_files": created,
            "deleted_files": deleted,
            "modified_files": modified,
            "event_count": len(trace.events),
            "result_event": trace.result_event,
            "stderr": trace.stderr,
        },
    )


def _worker_environment() -> dict[str, str]:
    """构建不会继承父 Claude 会话标记的子进程环境。

    嵌套会话变量（CLAUDECODE / CLAUDE_CODE_ENTRYPOINT）会让 CLI 拒绝启动
    或改变行为，必须剥离；同时强制 UTF-8，避免 Windows 控制台编码干扰
    stream-json 解析。
    """

    environment = os.environ.copy()
    for key in list(environment):
        if key.upper() in {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}:
            environment.pop(key, None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def run_worker(
    task: WorkerTask,
    *,
    output_validator: Callable[[Path], tuple[bool, str | None]] | None = None,
) -> WorkerRunResult:
    """运行一次受限 worker 并返回独立校验后的结果。

    功能：
        执行顺序固定为：前置存在性检查 → 输入指纹与目录快照 → 启动受限
        进程 → 解析事件流 → 独立校验。函数刻意保持同步：它是一个小的进程
        边界，异步协调器可通过 ``asyncio.to_thread`` 调用。把 subprocess
        所有权集中在这里，各角色适配器就不必重复实现环境清理与校验。
    参数：
        task: worker 契约。
        output_validator: 可选领域输出校验回调。
    返回值：
        WorkerRunResult；超时不抛异常，而是返回 return_code=-2 的失败结果。
    """

    task.cwd.mkdir(parents=True, exist_ok=True)
    task.mcp_config.parent.mkdir(parents=True, exist_ok=True)
    if not task.mcp_config.exists():
        raise FileNotFoundError(f"MCP config does not exist: {task.mcp_config}")
    for path in task.input_paths:
        if not path.exists():
            raise FileNotFoundError(f"Worker input does not exist: {path}")

    input_hashes_before = {path: _sha256(path) for path in task.input_paths}
    files_before = _snapshot_files(task.cwd)
    command = build_worker_command(task)
    try:
        completed = subprocess.run(
            command,
            cwd=str(task.cwd),
            env=_worker_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=task.timeout_seconds,
            check=False,
        )
        trace = parse_worker_stream(completed.stdout, completed.returncode, completed.stderr)
    except subprocess.TimeoutExpired as exc:
        # 超时也要保留已产生的部分事件流，便于诊断 worker 卡在哪一步。
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        trace = parse_worker_stream(stdout, -2, stderr + "\nWorker process timed out.")

    validation = validate_worker_contract(
        task,
        trace,
        input_hashes_before,
        files_before,
        output_validator=output_validator,
    )
    return WorkerRunResult(task=task, trace=trace, validation=validation, command=command)


def json_output_validator(expected: dict[str, Any]) -> Callable[[Path], tuple[bool, str | None]]:
    """创建一个"输出 JSON 必须与预言完全一致"的确定性校验器。

    适用于合成测试与可精确预知产物的任务；真实研究产物应改用各自的
    schema 校验器，而不是逐字节比对。
    """

    def validate(path: Path) -> tuple[bool, str | None]:
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"Output is not valid JSON: {exc}"
        if actual != expected:
            return False, f"Output mismatch at {path}: expected {expected!r}, got {actual!r}"
        return True, None

    return validate


def scrub_command(command: Iterable[str], *, inline_agent_marker: str = "<inline-agent-definition>") -> list[str]:
    """返回可安全落日志的命令：内联 agent 定义原文被打码。

    定义正文可能包含长提示词与文件路径细节，落进事件或报告只会放大
    体积并暴露内部指令；命令结构本身保留，足以复核隔离与工具白名单。
    """

    items = list(command)
    if "--agents" in items:
        index = items.index("--agents") + 1
        if index < len(items):
            items[index] = inline_agent_marker
    return items
