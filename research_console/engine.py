"""运行引擎：Run / EventBus / 流水线执行器。

功能：
- EventBus：run 内事件的追加、持久化（events.jsonl）与订阅唤醒；
- Run：单次运行的状态容器（计划、步骤状态、子进程、手动信号、取消）；
- Engine：run 生命周期管理（创建、取消、手动完成/跳过、历史恢复）；
- 公司/行业流水线：脚本流式执行、进度探针、回流检测、LLM 三模式等待；
- demo 脚本化事件序列与 replay 事件合成。

编排原则：后端不搬运研究逻辑，只调用既有脚本并监视工作区文件落盘；
LLM 步骤的完成判定永远以期望产物出现为准。
"""

from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from research_console import config, history, state_reader, steps

logger = logging.getLogger("research_console.engine")

RUN_MODES = ("company", "industry", "demo", "replay")
# python_agent_coordinator：Python 主会话 + 注册表 agent（精确 I/O）
# 已移除 Claude Code 主会话 /rec（coordinator_cli）黑盒调度。
LLM_MODES = ("python_agent_coordinator", "manual", "claude_cli", "skip")

# 主线上一旦失败就无法继续的步骤（下游产物依赖其输出）。
_FATAL_COMPANY_STEPS = {
    "audit",
    "collector_fetch",
    "processor_parse",
    "processor_digest",
    "processor_rag",
    "financial_evidence_draft",
}

# 只有这些步骤在 legacy 流水线中存在实际的 skip 消费点；audit/final_audit/
# deliver 和 coordinator display-only 计划绝不能接受手动跳过。
_COMPANY_SKIP_CONSUMERS = {
    "collector_fetch",
    "processor_parse",
    "processor_digest",
    "processor_rag",
    "processor_compare",
    "financial_evidence_draft",
    "formal_financial_analysis",
    "market_context_update",
    "valuation_update",
}
_INDUSTRY_SKIP_CONSUMERS = {"industry_collect", "industry_validate", "industry_research"}

# 只有通过质量 Gate 的公开网页代理包可算干净完成；其他状态（含 partial、
# blocked、空/未知）一律降级，防止 denylist 漏掉新增状态。
_GOOD_MARKET_STATUS = "ready_public_proxy"

_VALUATION_FILES = (
    "valuation_report.json",
    "valuation_report.md",
    "valuation_evidence_table.json",
    "valuation_audit.json",
)


def _kind_of(path: str | Path) -> str:
    """按扩展名判断 artifact 展示类型。

    参数：
        path: 文件路径。
    返回值：
        json/md/jsonl/pdf/other 之一。
    """
    suffix = Path(str(path)).suffix.lower()
    return {".json": "json", ".md": "md", ".jsonl": "jsonl", ".pdf": "pdf"}.get(suffix, "other")


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """单个 run 的事件总线。

    功能：
        持有事件列表与 asyncio.Condition；publish 追加事件、写 events.jsonl
        并唤醒所有 SSE 订阅者；订阅者用 wait_beyond 等待新事件。
    参数：
        events_file: events.jsonl 路径；None 表示不持久化（仅内存）。
    返回值：
        实例。
    """

    def __init__(self, events_file: Path | None):
        self.events: list[dict[str, Any]] = []
        self.cond = asyncio.Condition()
        self.events_file = events_file
        # seq 不能由 len(events) 推导：瞬态 partial 不落盘、损坏历史行会被跳过，
        # 两种情况都会产生合法空洞。独立游标保证重启后仍严格单调且不重复。
        self._next_seq = 1
        self._last_seq = 0

    async def publish(
        self,
        run_id: str,
        event_type: str,
        step_id: str | None = None,
        owner: str | None = None,
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        """追加并广播一个事件。

        参数：
            run_id: 运行标识。
            event_type: 契约事件类型。
            step_id: 关联步骤（可空）。
            owner: 关联角色（可空）。
            payload: 事件负载。
            ts: 显式时间戳（replay 用文件 mtime）；缺省取当前时间。
        返回值：
            完整事件字典（含 seq）。
        """
        event: dict[str, Any] = {
            "seq": 0,
            "ts": ts or state_reader.now_iso(),
            "run_id": run_id,
            "type": event_type,
        }
        if step_id:
            event["step_id"] = step_id
        if owner:
            event["owner"] = owner
        event["payload"] = payload or {}
        await self.publish_prepared(event)
        return event

    async def publish_prepared(self, event: dict[str, Any]) -> None:
        """发布一个已构造好的事件（demo/replay 复用，seq 以总线为准重排）。

        参数：
            event: 事件字典；seq 会被重新赋值以保证 run 内单调递增。
        返回值：
            无。
        """
        async with self.cond:
            # cumulative partial 只是实时打字预览：保留最新一条即可。旧预览已被在线
            # 客户端消费，继续留在内存只会形成 1+2+…+n 的文本放大。
            is_partial = self._is_partial_preview(event)
            if is_partial or event.get("type") == "coordinator_message":
                self.events = [item for item in self.events if not self._is_partial_preview(item)]
            # seq 必须以总线为唯一事实源；即使内存中删除预览或历史存在空洞也不回退。
            event["seq"] = self._next_seq
            self._next_seq += 1
            self._last_seq = int(event["seq"])
            self.events.append(event)
            self._persist(event)
            self.cond.notify_all()

    @staticmethod
    def _is_partial_preview(event: dict[str, Any]) -> bool:
        """判断事件是否为可丢弃的 coordinator 累计文本预览。"""
        payload = event.get("payload") if isinstance(event, dict) else {}
        return bool(
            event.get("type") == "coordinator_message"
            and isinstance(payload, dict)
            and payload.get("partial") is True
        )

    @property
    def max_seq(self) -> int:
        """返回已经分配的最大事件序号。"""
        return self._last_seq

    def _persist(self, event: dict[str, Any]) -> None:
        """把权威事件追加写入 events.jsonl。

        cumulative partial 已由原始 ``claude_events.jsonl`` 完整审计，控制台事件文件
        只保存最终 assistant 消息，避免长会话产生 O(n²) 磁盘放大。

        参数：
            event: 事件字典。
        返回值：
            无。
        """
        if not self.events_file or self._is_partial_preview(event):
            return
        try:
            with self.events_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("Failed to write event file: %s", self.events_file, exc_info=True)

    def load_events(self, events: list[dict[str, Any]]) -> None:
        """启动恢复时批量装载合法历史事件（不触发持久化与通知）。

        参数：
            events: 历史事件列表；非对象、无正整数 seq 或重复 seq 会被忽略。
        返回值：
            无。
        """
        valid: list[dict[str, Any]] = []
        seen: set[int] = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0 or seq in seen:
                continue
            seen.add(seq)
            valid.append(event)
        valid.sort(key=lambda item: int(item["seq"]))
        self.events = valid
        self._last_seq = max(seen, default=0)
        self._next_seq = self._last_seq + 1

    def append_recovered(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """恢复阶段同步追加一条事件，并沿用最大历史 seq 继续编号。"""
        event = {
            "seq": self._next_seq,
            "ts": state_reader.now_iso(),
            "run_id": run_id,
            "type": event_type,
            "payload": payload,
        }
        self._next_seq += 1
        self._last_seq = int(event["seq"])
        self.events.append(event)
        self._persist(event)
        return event

    def snapshot(self, after_seq: int = 0) -> list[dict[str, Any]]:
        """获取 seq 大于 after_seq 的事件快照。

        参数：
            after_seq: 断线重连的补发游标。
        返回值：
            事件列表。
        """
        return [event for event in self.events if event.get("seq", 0) > after_seq]

    async def wait_beyond(self, after_seq: int, timeout: float) -> bool:
        """等待最大事件序号超过 SSE 游标。

        参数：
            after_seq: 当前客户端已消费的最大 seq。
            timeout: 最长等待秒数（SSE 心跳节奏）。
        返回值：
            出现更大 seq 返回 True；超时返回 False。
        """
        async with self.cond:
            if self._last_seq > after_seq:
                return True
            try:
                await asyncio.wait_for(self.cond.wait(), timeout)
            except asyncio.TimeoutError:
                return False
            return self._last_seq > after_seq


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

class Run:
    """单次运行的状态容器。

    参数：
        run_id: 运行标识（r_ + 时间戳 + 随机后缀）。
        mode: company/industry/demo/replay。
        params: 运行参数。
        llm_mode: manual/claude_cli/skip。
        run_dir: 持久化目录；None 表示不落盘。
    返回值：
        实例。
    """

    def __init__(self, run_id: str, mode: str, params: dict[str, Any], llm_mode: str, run_dir: Path | None):
        self.run_id = run_id
        self.mode = mode
        self.params = dict(params or {})
        self.llm_mode = llm_mode
        self.status = "running"
        self.created_at = state_reader.now_iso()
        self.run_dir = run_dir
        self.bus = EventBus(run_dir / "events.jsonl" if run_dir else None)
        self.task: asyncio.Task | None = None
        self.procs: set[Any] = set()
        # 与主流水线并行、但不由 await 调用栈自动拥有的任务统一登记；终态发布前
        # 必须取消并等待它们，确保 run_completed 之后不会再出现迟到事件。
        self.child_tasks: set[asyncio.Task[Any]] = set()
        # 阶段一只持久化会话标识与执行模式，为后续人工 --resume 留接口；
        # 服务重启后仍沿用现有“运行中断即失败”策略，不自动恢复 Claude 会话。
        self.claude_session_id: str | None = None
        self.execution_mode: str | None = None
        self.coordinator_pid: int | None = None
        self.cancel_event = asyncio.Event()
        # 手动信号：step_id → skip / complete / complete_force，由 REST 写入、流水线轮询消费。
        self.manual_signals: dict[str, str] = {}
        # 只有真正存在消费窗口的步骤才进入集合；coordinator display-only 计划为空，
        # 防止 API 对永远不会生效的 /skip 错误返回成功。
        self.skip_accepting_steps: set[str] = set()
        # LLM 步骤的期望产物组：任一组全部落盘即视为完成（估值有新旧两种目录布局）。
        self.llm_artifact_groups: dict[str, list[list[str]]] = {}
        self.llm_artifact_baselines: dict[str, dict[str, tuple[int, int] | None]] = {}
        self.step_status: dict[str, str] = {}
        self.plan: list[dict[str, Any]] = []
        self.failed_steps: list[str] = []
        self.runtime_skipped: list[str] = []
        self.degraded_steps: set[str] = set()
        self.llm_mode_skipped = False
        # 同一正式公司工作区只允许一个活动 run；键由 Engine 在创建时分配。
        self.workspace_lease_key: str | None = None
        # 服务异常退出后若无法确认/清理持久化 coordinator PID，保留租约阻止新写者。
        self.orphan_process_unresolved = False

    def track_child_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """登记一个并行子任务，并在任务结束后自动移除。"""
        self.child_tasks.add(task)
        task.add_done_callback(self.child_tasks.discard)
        return task

    def persist_meta(self) -> None:
        """把运行元信息写入 meta.json。

        参数：
            无。
        返回值：
            无。
        """
        if not self.run_dir:
            return
        meta = {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "params": self.params,
            "llm_mode": self.llm_mode,
            "claude_session_id": self.claude_session_id,
            "execution_mode": self.execution_mode,
            "coordinator_pid": self.coordinator_pid,
        }
        meta_path = self.run_dir / "meta.json"
        temp_path = self.run_dir / f".meta.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            # 同目录临时文件 + fsync + os.replace 保证崩溃时旧 meta 仍完整可读；
            # 不能直接 write_text 截断目标，否则历史 run 可能在重启后完全消失。
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(meta, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, meta_path)
        except OSError:
            logger.warning("Failed to write meta.json: %s", self.run_dir, exc_info=True)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def brief(self) -> dict[str, Any]:
        """生成 run 列表条目。

        参数：
            无。
        返回值：
            {run_id, mode, status, created_at, params} 字典。
        """
        history_info = history.run_history_brief(
            self.mode,
            self.params,
            self.bus.events,
            self.run_dir,
        )
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "params": self.params,
            "llm_mode": self.llm_mode,
            "execution_mode": self.execution_mode,
            "claude_session_id": self.claude_session_id,
            **history_info,
        }


# ---------------------------------------------------------------------------
# 日志节流
# ---------------------------------------------------------------------------

class StepLogThrottle:
    """step_log 行事件节流器。

    功能：
        每步最多推送约 STEP_LOG_MAX_LINES 行；超出后普通行折叠计数，
        含错误/失败/完成等关键字的行优先保留，避免 SSE 洪泛又不丢关键信息。
    参数：
        run: 所属运行。
        step_id: 步骤标识。
        owner: 角色。
    返回值：
        实例。
    """

    _IMPORTANT = ("error", "fail", "exception", "traceback", "warning", "失败", "错误", "异常", "警告", "完成", "成功")

    def __init__(self, run: Run, step_id: str, owner: str):
        self.run = run
        self.step_id = step_id
        self.owner = owner
        self.sent = 0
        self.suppressed = 0

    async def emit(self, line: str) -> None:
        """按节流规则推送一行日志。

        参数：
            line: 原始日志行。
        返回值：
            无。
        """
        text = line.rstrip()
        if not text:
            return
        lowered = text.lower()
        important = any(marker in lowered for marker in self._IMPORTANT)
        limit = config.STEP_LOG_MAX_LINES
        # 关键行额外放宽 50 行配额：错误信息比普通进度行更值得占用事件带宽。
        if self.sent < limit or (important and self.sent < limit + 50):
            self.sent += 1
            await self.run.bus.publish(
                self.run.run_id, "step_log", self.step_id, self.owner, {"line": text[:800]}
            )
            return
        self.suppressed += 1
        if self.suppressed % 200 == 0:
            await self.run.bus.publish(
                self.run.run_id,
                "step_log",
                self.step_id,
                self.owner,
                {"line": f"…Log output is long; {self.suppressed} lines have been collapsed"},
            )

    async def flush(self) -> None:
        """步骤结束时补发折叠统计。

        参数：
            无。
        返回值：
            无。
        """
        if self.suppressed:
            await self.run.bus.publish(
                self.run.run_id,
                "step_log",
                self.step_id,
                self.owner,
                {"line": f"This step collapsed {self.suppressed} log lines in total"},
            )


# ---------------------------------------------------------------------------
# 子进程流式执行
# ---------------------------------------------------------------------------

async def _stream_subprocess(
    run: Run,
    step_id: str,
    owner: str,
    cmd: list[str],
    *,
    emit_log: bool = True,
    strip_claude_env: bool = False,
    timeout: float | None = None,
) -> tuple[int, list[str]]:
    """启动子进程并逐行流式读取 stdout（stderr 合并）。

    功能：
        - 不经 shell，env 强制 UTF-8，按 utf-8 errors=replace 解码；
        - 行事件经节流器推送；始终保留尾部若干行用于失败诊断；
        - 子进程登记到 run.procs，取消运行时由引擎统一 terminate→kill；
        - 超时后终止进程并返回特殊退出码 -2。
    参数：
        run: 所属运行。
        step_id: 步骤标识。
        owner: 角色。
        cmd: 命令参数列表。
        emit_log: 是否推送 step_log 事件。
        strip_claude_env: 是否剥离 CLAUDE* 环境变量（claude CLI 子进程用）。
        timeout: 超时秒数；缺省用 SCRIPT_TIMEOUT_SECONDS。
    返回值：
        (退出码, 尾部日志行列表)。
    """
    throttle = StepLogThrottle(run, step_id, owner)
    tail: deque[str] = deque(maxlen=60)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(config.PROJECT_ROOT),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=state_reader.subprocess_env(strip_claude=strip_claude_env),
        )
    except OSError as exc:
        return -1, [f"Failed to start subprocess: {exc}"]
    run.procs.add(proc)
    try:
        try:
            async with asyncio.timeout(timeout or config.SCRIPT_TIMEOUT_SECONDS):
                assert proc.stdout is not None
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    tail.append(line)
                    if emit_log:
                        await throttle.emit(line)
                await proc.wait()
        except TimeoutError:
            await _terminate_process(proc)
            tail.append("Subprocess timed out and was terminated")
            return -2, list(tail)
        except asyncio.CancelledError:
            # 取消必须在 finally 移除登记之前完成进程树清理，否则外层守护器
            # 无法再通过 run.procs 找到该进程。
            await _terminate_process(proc)
            raise
        except Exception:
            await _terminate_process(proc)
            raise
        if emit_log:
            await throttle.flush()
        return proc.returncode or 0, list(tail)
    finally:
        run.procs.discard(proc)


# ---------------------------------------------------------------------------
# 控制台 owner 常量（demo / python coordinator 事件共用）
# ---------------------------------------------------------------------------

_COORDINATOR_OWNER = steps.ORCHESTRATOR
_KNOWN_AGENT_OWNERS = {
    steps.INFO_COLLECTOR,
    steps.INFO_PROCESSOR,
    steps.FINANCIAL_ANALYST,
    steps.VALUATION_ANALYST,
    steps.MARKET_CONTEXT_COLLECTOR,
    steps.INDUSTRY_INFO_COLLECTOR,
    steps.INDUSTRY_RESEARCHER,
}


class WorkspaceLeaseConflict(RuntimeError):
    """同一正式公司工作区已有活动 run。"""

    def __init__(self, lease_key: str, run_id: str):
        super().__init__(f"The research target is already running: {run_id}")
        self.lease_key = lease_key
        self.run_id = run_id


class Engine:
    """run 生命周期管理器。

    功能：
        创建/取消运行、转发手动完成与跳过信号、启动时恢复历史 run。
    参数：
        无。
    返回值：
        实例。
    """

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        # 键为 company:<stock_code>；无法规范化代码的名称请求使用 company:unresolved。
        # 单进程 FastAPI 由锁保证并发 POST 原子占用，终态/取消/异常均在 wrapper 释放。
        self._workspace_leases: dict[str, str] = {}
        self._lease_lock = asyncio.Lock()

    # -- 生命周期 -----------------------------------------------------------

    def load_persisted_runs(self) -> None:
        """扫描 console_workspace/runs 恢复历史运行（只读）。

        功能：
            读取 meta.json 与 events.jsonl；若上次服务退出时 run 仍是
            running，说明执行被打断，补写 run_error + run_completed(failed)，
            让前端不会永远等待。
        参数：
            无。
        返回值：
            无。
        """
        if not config.RUNS_DIR.exists():
            return
        for meta_file in sorted(config.RUNS_DIR.glob("*/meta.json")):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Skipped corrupted run metadata: %s", meta_file)
                continue
            run_id = str(meta.get("run_id") or meta_file.parent.name)
            run = Run(
                run_id,
                str(meta.get("mode") or "company"),
                meta.get("params") or {},
                str(meta.get("llm_mode") or config.DEFAULT_LLM_MODE),
                meta_file.parent,
            )
            run.created_at = str(meta.get("created_at") or run.created_at)
            run.status = str(meta.get("status") or "failed")
            run.claude_session_id = str(meta.get("claude_session_id") or "") or None
            run.execution_mode = str(meta.get("execution_mode") or "") or None
            # 历史 meta 可能仍含 coordinator_pid（旧 /rec 主会话）；不再恢复或清理该进程。
            run.coordinator_pid = None
            events: list[dict[str, Any]] = []
            events_file = meta_file.parent / "events.jsonl"
            if events_file.exists():
                try:
                    for line in events_file.read_text(encoding="utf-8").splitlines():
                        text = line.strip()
                        if not text:
                            continue
                        try:
                            event = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            events.append(event)
                except OSError:
                    logger.warning("Failed to read event file: %s", events_file, exc_info=True)
            run.bus.load_events(events)
            terminal_events = [event for event in run.bus.events if event.get("type") == "run_completed"]
            if terminal_events:
                # terminal 已经 durable、meta 仍为 running 是正常的崩溃窗口；以最后一个
                # 合法终态修复 meta，绝不能再追加第二个 failed 终态。
                terminal_payload = terminal_events[-1].get("payload") or {}
                run.status = str(terminal_payload.get("status") or run.status or "failed")
                run.persist_meta()
            elif run.status == "running":
                # 服务重启且事件中确实没有终态：补中断诊断与唯一失败终态。
                run.status = "failed"
                run.bus.append_recovered(run_id, "run_error", {"error": "The service restarted and interrupted the run"})
                run.bus.append_recovered(run_id, "run_completed", {"status": "failed"})
                run.persist_meta()
            self.runs[run_id] = run

    @staticmethod
    def _normalize_company_params(params: dict[str, Any]) -> dict[str, Any]:
        """统一 company 请求中的财年别名，并拒绝互相矛盾的双字段。"""
        normalized = dict(params or {})
        report_year = str(normalized.get("report_year") or "").strip()
        fiscal_year = str(normalized.get("fiscal_year") or "").strip()
        if report_year and fiscal_year and report_year != fiscal_year:
            raise ValueError(f"report_year={report_year} conflicts with fiscal_year={fiscal_year}")
        year = report_year or fiscal_year
        if year:
            normalized["report_year"] = year
            normalized["fiscal_year"] = year
        return normalized

    @staticmethod
    def _company_lease_key(params: dict[str, Any], state: dict[str, Any] | None = None) -> str:
        """生成公司正式工作区租约键，按股票维度覆盖所有共享写目录。

        valuation/market_context 目录不含财年，因此同一股票的不同财年也必须串行。
        未能规范化为股票代码的公司名请求共用 unresolved 租约，宁可降低并发度，也
        不能让“公司名”和“代码”两个别名绕过单写者保护。
        """
        target = state.get("target", {}) if isinstance(state, dict) else {}
        stock_code = str(target.get("stock_code") or params.get("stock_code") or "").strip()
        raw_target = str(params.get("target") or "").strip()
        if not stock_code and raw_target.isdigit() and len(raw_target) == 6:
            stock_code = raw_target
        identity = stock_code or "unresolved"
        return f"company:{identity.lower()}"

    async def create_run_checked(self, mode: str, params: dict[str, Any], llm_mode: str) -> Run:
        """规范化研究目标、原子占用正式工作区后创建 run。

        公司名和股票代码可能指向同一工作区，因此创建前先做一次只读 audit 获取规范化
        stock_code；预检失败时仍以原始目标加锁，让后续正式 audit 给出具体错误。
        """
        normalized_params = (
            self._normalize_company_params(params) if mode == "company" else dict(params or {})
        )
        lease_key: str | None = None
        if mode == "company":
            state, code, _ = await state_reader.run_audit(normalized_params, write_state=False)
            if code == 0 and state:
                target = state.get("target", {}) if isinstance(state.get("target"), dict) else {}
                for key in ("stock_code", "company_name"):
                    if target.get(key) and not normalized_params.get(key):
                        normalized_params[key] = target[key]
                filing_policy = str(state.get("filing_policy") or (state.get("request") or {}).get("filing_policy") or "")
                if filing_policy:
                    normalized_params.setdefault("filing_policy", filing_policy)
                normalized_params.setdefault("annual_lookback", int((state.get("request") or {}).get("annual_lookback") or 2))
                if filing_policy == "single_filing":
                    for key in ("report_year", "report_type"):
                        if target.get(key) and not normalized_params.get(key):
                            normalized_params[key] = target[key]
                resolved_code = str(target.get("stock_code") or "").strip()
                if re.fullmatch(r"\d{6}", resolved_code):
                    raw_target = str(normalized_params.get("target") or "").strip()
                    if raw_target and raw_target != resolved_code:
                        normalized_params.setdefault("input_target", raw_target)
                    normalized_params["target"] = resolved_code
                lease_key = self._company_lease_key(normalized_params, state)
            else:
                lease_key = self._company_lease_key(normalized_params)

        async with self._lease_lock:
            if lease_key:
                owner = self._workspace_leases.get(lease_key)
                existing = self.runs.get(owner or "")
                if owner and existing and (existing.status == "running" or existing.orphan_process_unresolved):
                    raise WorkspaceLeaseConflict(lease_key, owner)
                if owner:
                    self._workspace_leases.pop(lease_key, None)
            return self.create_run(mode, normalized_params, llm_mode, lease_key=lease_key)

    def create_run(
        self,
        mode: str,
        params: dict[str, Any],
        llm_mode: str,
        *,
        lease_key: str | None = None,
    ) -> Run:
        """创建并启动一次运行。

        参数：
            mode: company/industry/demo/replay。
            params: 运行参数。
            llm_mode: manual/claude_cli/skip。
            lease_key: 已规范化的正式工作区租约键；公司模式缺省时按原始参数生成。
        返回值：
            Run 实例（后台任务已启动）。
        """
        if mode == "company":
            params = self._normalize_company_params(params)
            lease_key = lease_key or self._company_lease_key(params)
            owner = self._workspace_leases.get(lease_key)
            existing = self.runs.get(owner or "")
            if owner and existing and (existing.status == "running" or existing.orphan_process_unresolved):
                raise WorkspaceLeaseConflict(lease_key, owner)
            if owner:
                self._workspace_leases.pop(lease_key, None)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"r_{stamp}_{secrets.token_hex(3)}"
        run_dir = config.RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        run = Run(run_id, mode, params, llm_mode, run_dir)
        run.workspace_lease_key = lease_key
        if lease_key:
            self._workspace_leases[lease_key] = run_id
        if mode == "company":
            if llm_mode == "python_agent_coordinator":
                run.execution_mode = "python_agent_coordinator"
            else:
                run.execution_mode = "legacy_dag"
        else:
            run.execution_mode = mode
        run.persist_meta()
        self.runs[run_id] = run
        pipeline = {
            "company": _company_pipeline,
            "industry": _industry_pipeline,
            "demo": _demo_pipeline,
            "replay": _replay_pipeline,
        }[mode]
        run.task = asyncio.create_task(self._run_wrapper(run, pipeline), name=f"run:{run_id}")
        return run

    async def _run_wrapper(self, run: Run, pipeline: Callable[[Run], Awaitable[str]]) -> None:
        """流水线外层守护：统一处理取消与未捕获异常。

        参数：
            run: 运行实例。
            pipeline: 流水线协程函数。
        返回值：
            无。
        """
        try:
            run.status = await pipeline(run)
        except asyncio.CancelledError:
            # 先结束并行协程，再清理其进程，最后发布唯一终态；顺序反过来会让
            # market_context 等分支在 run_completed 之后继续发布 step_failed。
            await self._cancel_child_tasks(run)
            await self._kill_procs(run)
            run.status = "cancelled"
            try:
                if not self._has_terminal_event(run):
                    await run.bus.publish(run.run_id, "run_completed", payload={"status": "cancelled"})
            except Exception:
                logger.warning("Failed to publish cancellation event: %s", run.run_id, exc_info=True)
        except Exception as exc:
            logger.exception("Run %s failed with an exception", run.run_id)
            await self._cancel_child_tasks(run)
            await self._kill_procs(run)
            run.status = "failed"
            try:
                if not self._has_terminal_event(run):
                    await run.bus.publish(run.run_id, "run_error", payload={"error": str(exc)})
                    await run.bus.publish(run.run_id, "run_completed", payload={"status": "failed"})
            except Exception:
                logger.warning("Failed to publish failure event: %s", run.run_id, exc_info=True)
        finally:
            await self._cancel_child_tasks(run)
            await self._kill_procs(run)
            run.persist_meta()
            self._release_workspace_lease(run)

    @staticmethod
    def _has_terminal_event(run: Run) -> bool:
        """判断 run 是否已经发布过 run_completed。"""
        return any(event.get("type") == "run_completed" for event in run.bus.events)

    async def _cancel_child_tasks(self, run: Run) -> None:
        """取消并等待 run 登记的全部并行子任务。"""
        tasks = [task for task in list(run.child_tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        run.child_tasks.clear()

    def _release_workspace_lease(self, run: Run) -> None:
        """仅由当前持有者释放正式工作区租约，避免误删后继 run 的占用。"""
        key = run.workspace_lease_key
        if key and self._workspace_leases.get(key) == run.run_id:
            self._workspace_leases.pop(key, None)

    async def _kill_procs(self, run: Run) -> None:
        """终止运行中登记的所有子进程（terminate→kill）。

        参数：
            run: 运行实例。
        返回值：
            无。
        """
        procs = [proc for proc in list(run.procs) if proc.returncode is None]
        if procs:
            await asyncio.gather(*(_terminate_process(proc) for proc in procs), return_exceptions=True)
        for proc in procs:
            run.procs.discard(proc)

    # -- 外部控制 -----------------------------------------------------------

    async def cancel_run(self, run_id: str) -> bool:
        """取消一次运行。

        参数：
            run_id: 运行标识。
        返回值：
            成功发起取消返回 True；Run not found or already finished返回 False。
        """
        run = self.runs.get(run_id)
        if not run or run.status != "running" or not run.task:
            return False
        run.cancel_event.set()
        run.task.cancel()
        # 等待守护器完成结构化清理；若任务在首次调度前就被取消，协程主体不会
        # 进入 try/finally，此处负责补齐取消终态、meta 与工作区租约释放。
        await asyncio.gather(run.task, return_exceptions=True)
        if run.status == "running":
            await self._cancel_child_tasks(run)
            await self._kill_procs(run)
            run.status = "cancelled"
            if not self._has_terminal_event(run):
                await run.bus.publish(run.run_id, "run_completed", payload={"status": "cancelled"})
            run.persist_meta()
            self._release_workspace_lease(run)
        return True

    def manual_complete(self, run_id: str, step_id: str, force: bool) -> tuple[bool, list[str]]:
        """手动标记 LLM 步骤完成。

        功能：
            仅对处于等待 LLM 产物状态的步骤有效；期望产物齐全时直接确认，
            不齐且 force=True 时强制完成（交付降级），否则返回缺失清单。
        参数：
            run_id: 运行标识。
            step_id: 步骤标识。
            force: 是否强制完成。
        返回值：
            (是否成功, 缺失产物路径列表)。
        """
        run = self.runs.get(run_id)
        if not run or run.status != "running":
            return False, ["Run not found or already finished"]
        groups = run.llm_artifact_groups.get(step_id)
        if not groups:
            return False, ["This step is not waiting for LLM artifacts"]
        missing = _best_group_missing(groups, run.llm_artifact_baselines.get(step_id))
        if missing and not force:
            return False, missing
        run.manual_signals[step_id] = "complete_force" if missing else "complete"
        return True, []

    def manual_skip(self, run_id: str, step_id: str) -> bool:
        """手动跳过一个尚未结束的步骤。

        参数：
            run_id: 运行标识。
            step_id: 步骤标识。
        返回值：
            信号写入成功返回 True。
        """
        run = self.runs.get(run_id)
        if not run or run.status != "running":
            return False
        if step_id not in run.skip_accepting_steps:
            return False
        if run.step_status.get(step_id) in {"completed", "failed", "skipped", "skipped_plan", "degraded"}:
            return False
        run.manual_signals[step_id] = "skip"
        # 同一消费窗口只接收一次信号；真正的执行协程仍会从 manual_signals 读取。
        run.skip_accepting_steps.discard(step_id)
        return True


def _artifact_signature(path_value: str) -> tuple[int, int] | None:
    """返回文件的纳秒 mtime 与大小，用于识别本次步骤是否真的刷新产物。"""
    try:
        stat = Path(path_value).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _best_group_missing(
    groups: list[list[str]],
    baseline: dict[str, tuple[int, int] | None] | None = None,
) -> list[str]:
    """计算“最接近完成”的产物组的缺失或未刷新清单。

    功能：
        LLM 步骤允许多个候选目录布局；只要任一组全部落盘且相对步骤启动前
        新建/变化即完成。提示缺失时选缺得最少的那组。
    参数：
        groups: 产物路径组列表。
        baseline: 步骤启动前的文件签名；None 表示仅检查存在性。
    返回值：
        缺失或仍为旧版本的路径列表；有任一组齐全且已刷新时为空列表。
    """
    best: list[str] | None = None
    for group in groups:
        missing = []
        for path in group:
            current = _artifact_signature(path)
            if current is None or (baseline is not None and current == baseline.get(path)):
                missing.append(path)
        if not missing:
            return []
        if best is None or len(missing) < len(best):
            best = missing
    return best or []


def _complete_group(
    groups: list[list[str]],
    baseline: dict[str, tuple[int, int] | None] | None = None,
) -> list[str] | None:
    """返回第一组全部落盘且相对基线已刷新的期望产物。"""
    for group in groups:
        if all(
            (current := _artifact_signature(path)) is not None
            and (baseline is None or current != baseline.get(path))
            for path in group
        ):
            return group
    return None


# ---------------------------------------------------------------------------
# 公司流水线
# ---------------------------------------------------------------------------

class _CompanyPipeline:
    """公司研究链路执行器。

    参数：
        run: 运行实例。
    返回值：
        实例；execute() 返回最终 run 状态字符串。
    """

    def __init__(self, run: Run):
        self.run = run
        self.bus = run.bus
        params = run.params
        self.force_refresh = bool(params.get("force_refresh"))
        self.depth = str(params.get("depth") or "standard")
        self.focus = str(params.get("focus") or "")
        self.as_of_date = str(params.get("as_of_date") or _dt.date.today().isoformat())
        self.freshness = str(params.get("market_context_freshness") or "oneMonth")
        self.run_market_context = params.get("run_market_context", True) is not False
        self.state: dict[str, Any] | None = None
        self.ctx: dict[str, Any] = {}
        self.allow_incomplete_digest = False
        self.backflow_sent: set[str] = set()
        # legacy 主线与 market_context 并行完成时会同时刷新状态；串行 audit 防止
        # 较早启动的扫描后到并覆盖较新的 research_state/self.ctx。
        self._audit_lock = asyncio.Lock()

    # -- 基础工具 -----------------------------------------------------------

    def _audit_params(self, force_refresh: bool = False) -> dict[str, Any]:
        """组装 audit 脚本参数。

        参数：
            force_refresh: 是否透传强制刷新（仅首次盘点使用用户输入值）。
        返回值：
            audit 参数字典。
        """
        params = self.run.params
        return {
            "target": params.get("target") or "",
            "stock_code": params.get("stock_code") or "",
            "company_name": params.get("company_name") or "",
            "report_year": params.get("report_year") or params.get("fiscal_year") or "",
            "report_type": params.get("report_type") or "",
            "filing_policy": params.get("filing_policy") or "",
            "annual_lookback": int(params.get("annual_lookback") or 2),
            "depth": self.depth,
            "focus": self.focus,
            "as_of_date": self.as_of_date,
            "force_refresh": force_refresh,
        }

    def _refresh_ctx(self) -> None:
        """把 research_state 中的关键路径缓存到 ctx，供后续步骤构建命令。

        参数：
            无。
        返回值：
            无。
        """
        state = self.state or {}
        target = state.get("target", {}) or {}
        layers = state.get("layers", {}) or {}
        filings = state.get("filings") if isinstance(state.get("filings"), list) else []
        primary_filing = next(
            (
                item
                for item in filings
                if str(item.get("report_type") or "") == str(target.get("report_type") or "")
                and str(item.get("report_year") or "") == str(target.get("report_year") or "")
            ),
            filings[-1] if filings else {},
        )

        def art(layer: str, key: str) -> str:
            info = layers.get(layer, {}).get("artifacts", {}).get(key, {})
            if not isinstance(info, dict) or not info.get("path"):
                info = (primary_filing.get(layer) or {}).get("artifacts", {}).get(key, {})
            return str(info.get("path") or "") if isinstance(info, dict) else ""

        def report_dir(layer: str) -> str:
            info = layers.get(layer, {}).get("report_dir", {})
            if not isinstance(info, dict) or not info.get("path"):
                info = (primary_filing.get(layer) or {}).get("report_dir", {})
            return str(info.get("path") or "") if isinstance(info, dict) else ""

        ctx = self.ctx
        ctx["filing_policy"] = str(state.get("filing_policy") or self.run.params.get("filing_policy") or "")
        ctx["filings"] = filings
        ctx["filing_plan"] = state.get("filing_plan") if isinstance(state.get("filing_plan"), list) else []
        ctx["financial_input_fingerprint"] = str(state.get("financial_input_fingerprint") or "")
        ctx["research_state_path"] = str(state_reader.state_file_path(state)) if state else ""
        ctx["stock_code"] = str(target.get("stock_code") or self.run.params.get("stock_code") or self.run.params.get("target") or "")
        ctx["company_name"] = str(target.get("company_name") or self.run.params.get("company_name") or "")
        ctx["report_year"] = str(target.get("report_year") or self.run.params.get("report_year") or "")
        ctx["report_type"] = str(target.get("report_type") or self.run.params.get("report_type") or "annual")
        ctx["report_stem"] = str(target.get("report_stem") or "")
        ctx["main_pdf"] = art("collector", "main_pdf")
        ctx["summary_pdf"] = art("collector", "summary_pdf")
        ctx["report_dir"] = report_dir("processor")
        content_json = art("processor", "content_json")
        if not content_json and ctx["report_dir"]:
            content_json = str(Path(ctx["report_dir"]) / "content.json")
        ctx["content_json"] = content_json
        ctx["pipeline_dir"] = str(Path(ctx["report_dir"]) / "digest_pipeline") if ctx["report_dir"] else ""
        ctx["llm_digest_path"] = art("processor", "llm_digest_json")
        ctx["digest_audit_path"] = art("processor", "digest_audit_json") or (
            str(Path(ctx["report_dir"]) / "digest_audit.json") if ctx["report_dir"] else ""
        )
        ctx["rag_chunks_path"] = art("processor", "rag_chunks_jsonl")
        ctx["summary_comparison_path"] = art("processor", "summary_comparison_json")
        analyst_dir = report_dir("financial_evidence_draft") or report_dir("formal_financial_analysis")
        if not analyst_dir and ctx["report_stem"] and ctx["stock_code"] and ctx["report_year"]:
            # 草稿还未生成时按脚本的自动推导规则预判输出目录，供 LLM 提示词与产物监视使用。
            analyst_dir = str(
                config.ANALYST_WORKSPACE / "reports" / ctx["report_type"] / ctx["report_year"] / ctx["stock_code"] / ctx["report_stem"]
            )
        ctx["analyst_dir"] = analyst_dir
        filing_set_path = art("financial_evidence_draft", "filing_set_json") or (
            str(Path(analyst_dir) / "filing_set.json") if analyst_dir and ctx.get("filing_policy") == "recent_history" else ""
        )
        ctx["filing_set_path"] = filing_set_path
        ctx["analyst_report_path"] = art("financial_evidence_draft", "analyst_report_json") or filing_set_path or (
            str(Path(analyst_dir) / "analyst_report.json") if analyst_dir else ""
        )
        # 多期交接包本身已按 as_of_date 隔离；单份模式继续使用旧的 as_of 子目录。
        if analyst_dir and ctx.get("filing_policy") == "recent_history":
            formal_dir = Path(analyst_dir)
        else:
            formal_dir = Path(analyst_dir) / "as_of" / self.as_of_date if analyst_dir else None
        ctx["formal_dir"] = str(formal_dir) if formal_dir else ""
        ctx["formal_json_path"] = str(formal_dir / "formal_financial_analysis.json") if formal_dir else ""
        ctx["as_of_date"] = self.as_of_date
        selected_record = layers.get("collector", {}).get("selected_record", {}) or primary_filing.get("selected_record", {}) or {}
        ctx["source_report_published_at"] = str(selected_record.get("published_at") or "")
        if ctx["stock_code"]:
            ctx["market_package_dir"] = str(config.MARKET_CONTEXT_WORKSPACE / "packages" / ctx["stock_code"] / self.as_of_date)
            ctx["valuation_dir"] = str(config.VALUATION_WORKSPACE / "reports" / ctx["stock_code"] / self.as_of_date)
            ctx["valuation_dir_legacy"] = str(config.VALUATION_WORKSPACE / ctx["stock_code"] / self.as_of_date)

    async def _emit(self, event_type: str, step_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        """发布事件的便捷封装（owner 自动取步骤定义，公司/行业步骤都支持）。

        参数：
            event_type: 事件类型。
            step_id: 步骤标识（可空）。
            payload: 负载。
        返回值：
            无。
        """
        step_def = (steps.COMPANY_STEP_MAP.get(step_id) or steps.INDUSTRY_STEP_MAP.get(step_id)) if step_id else None
        await self.bus.publish(self.run.run_id, event_type, step_id, step_def.owner if step_def else None, payload or {})

    async def _artifact(self, step_id: str, path: str | Path) -> None:
        """发布 artifact_created 事件。

        参数：
            step_id: 步骤标识。
            path: 产物路径。
        返回值：
            无。
        """
        path_text = str(path)
        await self._emit(
            "artifact_created",
            step_id,
            {"path": path_text, "name": Path(path_text).name, "kind": _kind_of(path_text)},
        )

    async def _backflow(self, key: str, from_step: str, to_owner: str, reason: str) -> None:
        """发布回流事件（同一缺口只提示一次，不自动重跑上游）。

        参数：
            key: 去重键。
            from_step: 发现缺口的步骤。
            to_owner: 建议承接缺口的角色。
            reason: 回流原因。
        返回值：
            无。
        """
        if key in self.backflow_sent:
            return
        self.backflow_sent.add(key)
        step_def = steps.COMPANY_STEP_MAP.get(from_step) or steps.INDUSTRY_STEP_MAP.get(from_step)
        await self.bus.publish(
            self.run.run_id,
            "backflow",
            from_step,
            step_def.owner if step_def else None,
            {"from_step": from_step, "to_owner": to_owner, "reason": reason},
        )

    async def _refresh_state(self, emit_event: bool = True) -> None:
        """静默重跑 audit 并广播 state_refreshed（层状态面板实时刷新）。

        参数：
            emit_event: 是否发布 state_refreshed 事件。
        返回值：
            无。
        """
        async with self._audit_lock:
            state, code, _ = await state_reader.run_audit(
                self._audit_params(), process_registry=self.run.procs
            )
            if code == 0 and state:
                self.state = state
                self._refresh_ctx()
                if emit_event:
                    summary = state.get("summary", {})
                    await self._emit(
                        "state_refreshed",
                        None,
                        {
                            "layer_statuses": summary.get("layer_statuses", {}),
                            "reusable": state.get("reusable", {}),
                            # 待办随层状态同步，避免已补齐的缺口在前端继续残留。
                            "next_actions": state.get("next_actions", []),
                        },
                    )
            else:
                logger.warning("Run %s state refresh failed", self.run.run_id)

    def _mark(self, step_id: str, status: str) -> None:
        """记录步骤终态，用于最终 run 状态判定。

        参数：
            step_id: 步骤标识。
            status: completed/failed/skipped/skipped_plan/degraded。
        返回值：
            无。
        """
        self.run.step_status[step_id] = status
        if status == "failed" and step_id not in self.run.failed_steps:
            self.run.failed_steps.append(step_id)
        if status == "skipped" and step_id not in self.run.runtime_skipped:
            self.run.runtime_skipped.append(step_id)
        if status == "degraded":
            self.run.degraded_steps.add(step_id)

    async def _consume_skip_signal(self, step_id: str) -> bool:
        """检查并消费步骤开始前的手动跳过信号。

        参数：
            step_id: 步骤标识。
        返回值：
            已跳过返回 True。
        """
        # 确定性步骤开始后即关闭“开始前跳过”窗口；LLM 步骤进入等待阶段时
        # 会由 _run_llm_step 重新开放可消费窗口。
        self.run.skip_accepting_steps.discard(step_id)
        if self.run.manual_signals.get(step_id) == "skip":
            self.run.manual_signals.pop(step_id, None)
            await self._emit("step_skipped", step_id, {"reason": "Skipped manually by the user"})
            self._mark(step_id, "skipped")
            return True
        return False

    # -- 主流程 -------------------------------------------------------------

    async def execute(self) -> str:
        """执行公司研究链路。

        参数：
            无。
        返回值：
            最终 run 状态：completed/partial/failed。
        """
        run = self.run
        await self._emit("run_started", None, {"mode": run.mode, "params": run.params, "llm_mode": run.llm_mode})
        broken = config.missing_scripts()
        if broken:
            await self._emit("run_error", None, {"error": "Required orchestration scripts are missing: " + "; ".join(broken)})
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"

        state = await self._step_audit("audit", force_refresh=self.force_refresh)
        if state is None:
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"

        plan = steps.build_company_plan(
            state,
            force_refresh=self.force_refresh,
            llm_mode=run.llm_mode,
            run_market_context=self.run_market_context,
        )
        run.plan = plan
        plan_map = {item["step_id"]: item for item in plan}
        for item in plan:
            if item["status"] == "skipped":
                self._mark(item["step_id"], "skipped_plan")
                if item.get("skip_reason") == steps.SKIP_REASON_LLM_MODE:
                    run.llm_mode_skipped = True
        run.skip_accepting_steps = {
            item["step_id"]
            for item in plan
            if item["status"] == "pending" and item["step_id"] in _COMPANY_SKIP_CONSUMERS
        }
        summary_block = state.get("summary", {})
        await self._emit(
            "plan_ready",
            None,
            {
                "steps": plan,
                "research_state_path": str(state_reader.state_file_path(state)),
                "layer_statuses": summary_block.get("layer_statuses", {}),
                "reusable": state.get("reusable", {}),
                "next_actions": state.get("next_actions", []),
            },
        )

        # 市场上下文与主线并行；结束状态（完成/跳过/失败）都放行估值。
        market_task: asyncio.Task | None = None
        if plan_map["market_context_update"]["status"] == "pending":
            market_task = run.track_child_task(
                asyncio.create_task(self._step_market_context(), name=f"{run.run_id}:market_context")
            )

        main_line = [
            "collector_fetch",
            "processor_parse",
            "processor_digest",
            "processor_rag",
            "processor_compare",
            "financial_evidence_draft",
            "formal_financial_analysis",
        ]
        aborted = False
        for step_id in main_line:
            if plan_map[step_id]["status"] == "skipped":
                continue
            if await self._consume_skip_signal(step_id):
                continue
            outcome = await self._dispatch(step_id)
            if outcome == "failed" and step_id in _FATAL_COMPANY_STEPS:
                aborted = True
                break

        if aborted:
            if market_task and not market_task.done():
                market_task.cancel()
                try:
                    await market_task
                except (asyncio.CancelledError, Exception):
                    logger.info("Run %s market-context branch stopped with the main line", run.run_id)
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"

        if market_task:
            try:
                await market_task
            except Exception:
                logger.exception("Run %s market-context branch failed", run.run_id)
                self._mark("market_context_update", "failed")

        await self._step_valuation_gate(plan_map)
        await self._step_audit("final_audit")
        summary = await self._step_deliver()
        status = self._final_status()
        summary, status = await _freeze_company_decision_before_terminal(run, summary, status)
        await self._emit("run_completed", None, {"status": status, "summary": summary})
        return status

    def _final_status(self) -> str:
        """依据步骤终态计算 run 最终状态。

        功能：
            主线致命失败在上游已经返回 failed；到达交付说明链路走通：
            没有失败/运行期跳过/LLM 模式跳过则 completed，否则 partial
            （交付降级，但仍是一份完整可读的结论卡）。
        参数：
            无。
        返回值：
            completed 或 partial。
        """
        if (
            self.run.failed_steps
            or self.run.runtime_skipped
            or self.run.degraded_steps
            or self.run.llm_mode_skipped
        ):
            return "partial"
        return "completed"

    async def _dispatch(self, step_id: str) -> str:
        """按 step_id 分派到对应处理器。

        参数：
            step_id: 步骤标识。
        返回值：
            步骤终态字符串。
        """
        handlers: dict[str, Callable[[], Awaitable[str]]] = {
            "collector_fetch": self._step_collector,
            "processor_parse": self._step_parse,
            "processor_digest": self._step_digest,
            "processor_rag": self._step_rag,
            "processor_compare": self._step_compare,
            "financial_evidence_draft": self._step_draft,
            "formal_financial_analysis": self._step_formal,
        }
        return await handlers[step_id]()

    # -- 各步骤实现 ---------------------------------------------------------

    async def _step_audit(self, step_id: str, force_refresh: bool = False) -> dict[str, Any] | None:
        """执行研究状态盘点步骤（audit / final_audit 共用）。

        参数：
            step_id: audit 或 final_audit。
            force_refresh: 是否透传强制刷新。
        返回值：
            research_state 字典；失败返回 None。
        """
        params = self._audit_params(force_refresh=force_refresh)
        cmd = state_reader.build_audit_command(params)
        await self._emit("step_started", step_id, {"cmd": steps.cmd_display(cmd)})
        state, code, tail = await state_reader.run_audit(
            params, process_registry=self.run.procs
        )
        if code != 0 or state is None:
            await self._emit("step_failed", step_id, {"error": f"Audit execution failed: {tail[-500:]}", "exit_code": code})
            self._mark(step_id, "failed")
            return None
        self.state = state
        self._refresh_ctx()
        state_path = state_reader.state_file_path(state)
        layer_statuses = state.get("summary", {}).get("layer_statuses", {})
        # audit 的 stdout 是整份 JSON，逐行推送只会淹没前端，这里只发一行摘要。
        await self._emit(
            "step_log",
            step_id,
            {"line": "Layer status: " + ", ".join(f"{name}={status}" for name, status in layer_statuses.items())},
        )
        await self._artifact(step_id, state_path)
        await self._emit("step_completed", step_id, {"summary": "Research-state audit completed", "artifacts": [str(state_path)]})
        self._mark(step_id, "completed")
        if step_id == "final_audit":
            refresh_payload: dict[str, Any] = {
                "layer_statuses": layer_statuses,
                "reusable": state.get("reusable", {}),
                "next_actions": state.get("next_actions", []),
            }
            await self._emit("state_refreshed", None, refresh_payload)
        return state

    async def _run_script(
        self,
        step_id: str,
        cmd: list[str],
        progress_fn: Callable[[], tuple[int, int, str, str]] | None = None,
    ) -> tuple[int, list[str]]:
        """执行一个脚本步骤的子进程部分（step_started + 日志流 + 可选进度探针）。

        参数：
            step_id: 步骤标识。
            cmd: 命令参数列表。
            progress_fn: 进度探针，返回 (done, total, unit, detail)。
        返回值：
            (退出码, 尾部日志行)。
        """
        owner = steps.COMPANY_STEP_MAP[step_id].owner
        await self._emit("step_started", step_id, {"cmd": steps.cmd_display(cmd)})
        poller: asyncio.Task | None = None
        if progress_fn is not None:
            poller = asyncio.create_task(self._poll_progress(step_id, progress_fn))
        try:
            code, tail = await _stream_subprocess(self.run, step_id, owner, cmd)
        finally:
            if poller:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass
        return code, tail

    async def _poll_progress(self, step_id: str, progress_fn: Callable[[], tuple[int, int, str, str]]) -> None:
        """周期性采样进度探针并推送 step_progress（仅在数值变化时发送）。

        参数：
            step_id: 步骤标识。
            progress_fn: 进度探针函数。
        返回值：
            无。
        """
        last: tuple[int, int] | None = None
        while True:
            await asyncio.sleep(config.PROGRESS_POLL_INTERVAL_SECONDS)
            try:
                done, total, unit, detail = progress_fn()
            except Exception:
                continue
            if total <= 0:
                continue
            if (done, total) != last:
                last = (done, total)
                payload: dict[str, Any] = {"done": done, "total": total, "unit": unit}
                if detail:
                    payload["detail"] = detail
                await self._emit("step_progress", step_id, payload)

    async def _step_collector(self) -> str:
        """执行财报采集下载步骤。

        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "collector_fetch"
        stock_code = str(self.ctx.get("stock_code") or "")
        filing_policy = str(self.ctx.get("filing_policy") or "")
        if not stock_code:
            await self._emit("step_started", step_id, {})
            await self._emit("step_failed", step_id, {"error": "stock_code is missing, so filing collection cannot start."})
            self._mark(step_id, "failed")
            return "failed"

        if filing_policy == "recent_history":
            command_items = steps.build_recent_collector_cmds(
                stock_code,
                self.as_of_date,
                annual_lookback=int(self.run.params.get("annual_lookback") or 2),
            )
            total = len(command_items)
            for index, item in enumerate(command_items, start=1):
                await self._emit(
                    "step_log",
                    step_id,
                    {"line": f"Collecting {item['report_type']} fiscal year {item['report_year']} within {item['disclosure_start']}..{item['disclosure_end']}"},
                )
                code, tail = await self._run_script(step_id, item["cmd"])
                if code != 0:
                    await self._emit(
                        "step_failed",
                        step_id,
                        {
                            "error": _tail_text(tail),
                            "exit_code": code,
                            "report_type": item["report_type"],
                            "report_year": item["report_year"],
                        },
                    )
                    self._mark(step_id, "failed")
                    return "failed"
                await self._emit("step_progress", step_id, {"done": index, "total": total, "unit": "filing windows"})
        else:
            report_year = str(self.ctx.get("report_year") or "")
            report_type = str(self.ctx.get("report_type") or "annual")
            if not report_year:
                await self._emit("step_started", step_id, {})
                await self._emit(
                    "step_failed",
                    step_id,
                    {"error": "report_year is missing, so the single-filing disclosure window cannot be derived."},
                )
                self._mark(step_id, "failed")
                return "failed"
            cmd = steps.build_collector_cmd(stock_code, report_type, report_year, cutoff=self.as_of_date)
            code, tail = await self._run_script(step_id, cmd)
            if code != 0:
                await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
                self._mark(step_id, "failed")
                return "failed"

        await self._refresh_state()
        filings = self.state.get("filings") if isinstance(self.state.get("filings"), list) else []
        for filing in filings:
            for key in ("main_pdf", "summary_pdf"):
                info = ((filing.get("collector") or {}).get("artifacts") or {}).get(key) or {}
                path = str(info.get("path") or "") if isinstance(info, dict) else ""
                if path and Path(path).exists():
                    await self._artifact(step_id, path)
        if str((self.state.get("layers", {}).get("collector") or {}).get("status") or "") != "ready":
            await self._emit(
                "step_failed",
                step_id,
                {"error": "Collection finished, but one or more required cutoff-eligible filings are still missing."},
            )
            self._mark(step_id, "failed")
            return "failed"
        await self._emit("step_completed", step_id, {"summary": f"{len(filings)} required financial filings are ready"})
        self._mark(step_id, "completed")
        return "completed"

    async def _step_parse(self) -> str:
        """执行 PDF 解析步骤（完成信号 = content.json 存在）。

        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "processor_parse"
        filings = self.state.get("filings") if isinstance(self.state.get("filings"), list) else []
        if str(self.ctx.get("filing_policy") or "") == "recent_history":
            pending = []
            for filing in filings:
                artifacts = (filing.get("processor") or {}).get("artifacts") or {}
                content_info = artifacts.get("content_json") or {}
                if self.force_refresh or not bool(content_info.get("exists")):
                    pending.append(filing)
            total = len(pending)
            for index, filing in enumerate(pending, start=1):
                record = filing.get("selected_record") or {}
                main_pdf_info = (((filing.get("collector") or {}).get("artifacts") or {}).get("main_pdf") or {})
                announcement_id = str(record.get("announcement_id") or "")
                cmd = steps.build_processor_parse_cmd(
                    str(self.ctx.get("stock_code") or ""),
                    str(filing.get("report_type") or "annual"),
                    str(filing.get("report_year") or ""),
                    overwrite=self.force_refresh,
                    announcement_id=announcement_id,
                    pdf_path=str(main_pdf_info.get("path") or "") if not announcement_id else "",
                )
                code, tail = await self._run_script(step_id, cmd)
                if code != 0:
                    await self._emit(
                        "step_failed",
                        step_id,
                        {"error": _tail_text(tail), "exit_code": code, "filing_id": filing.get("filing_id")},
                    )
                    self._mark(step_id, "failed")
                    return "failed"
                await self._emit("step_progress", step_id, {"done": index, "total": total, "unit": "filings"})
            await self._refresh_state()
            refreshed = self.state.get("filings") if isinstance(self.state.get("filings"), list) else []
            missing = []
            for filing in refreshed:
                info = (((filing.get("processor") or {}).get("artifacts") or {}).get("content_json") or {})
                path = str(info.get("path") or "")
                if not info.get("exists"):
                    missing.append(f"{filing.get('report_type')} {filing.get('report_year')}")
                elif path:
                    await self._artifact(step_id, path)
            if missing:
                await self._emit("step_failed", step_id, {"error": "Parsing did not produce content.json for: " + ", ".join(missing)})
                self._mark(step_id, "failed")
                return "failed"
            await self._emit("step_completed", step_id, {"summary": f"PDF parsing is ready for {len(refreshed)} filings"})
            self._mark(step_id, "completed")
            return "completed"

        content_before_path = str(self.ctx.get("content_json") or "")
        content_before = _artifact_signature(content_before_path) if content_before_path else None
        cmd = steps.build_processor_parse_cmd(
            str(self.ctx.get("stock_code") or ""),
            str(self.ctx.get("report_type") or "annual"),
            str(self.ctx.get("report_year") or ""),
            overwrite=self.force_refresh,
        )
        code, tail = await self._run_script(step_id, cmd)
        await self._refresh_state()
        content_json = str(self.ctx.get("content_json") or "")
        content_after = _artifact_signature(content_json) if content_json else None
        stale_force_refresh = self.force_refresh and content_after == content_before
        if code != 0 or content_after is None or stale_force_refresh:
            error = _tail_text(tail) if code != 0 else (
                "content.json did not change after force_refresh" if stale_force_refresh else "Parsing finished but content.json was not found"
            )
            await self._emit("step_failed", step_id, {"error": error, "exit_code": code})
            self._mark(step_id, "failed")
            return "failed"
        await self._artifact(step_id, content_json)
        await self._emit("step_completed", step_id, {"summary": "PDF parsing completed"})
        self._mark(step_id, "completed")
        return "completed"

    def _digest_progress(self) -> tuple[int, int, str, str]:
        """digest 实时进度探针：agent_results 结果数 / chunk 总数。

        参数：
            无。
        返回值：
            (done, total, unit, detail)。
        """
        pipeline_dir = Path(self.ctx.get("pipeline_dir") or "")
        manifest = state_reader.load_json_dict(pipeline_dir / "chunk_manifest.json")
        chunks = manifest.get("chunks")
        total = len(chunks) if isinstance(chunks, list) else 0
        done = len(list((pipeline_dir / "agent_results").glob("*.digest.json"))) if pipeline_dir.exists() else 0
        return min(done, total) if total else done, total, "chunks", ""

    async def _step_digest(self) -> str:
        """执行 LLM Digest 三连（prepare → auto-digest → merge）。

        功能：
            prepare/merge 在产物已存在时会抛错退出；此处捕获退出码后检查
            产物是否已在，已在则视为成功跳过。merge 缺 chunk 失败时用
            --allow-partial 重试一次。merge 后读 digest_audit.json，
            complete=false 时发 backflow（仅提示），后续草稿加宽容开关。
        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "processor_digest"
        if str(self.ctx.get("filing_policy") or "") == "recent_history":
            return await self._step_digest_recent()
        content_json = self.ctx.get("content_json") or ""
        if not content_json or not Path(content_json).exists():
            await self._emit("step_started", step_id, {})
            await self._emit("step_failed", step_id, {"error": "content.json is missing; the digest cannot be built."})
            self._mark(step_id, "failed")
            return "failed"
        pipeline_dir = self.ctx.get("pipeline_dir") or str(Path(content_json).parent / "digest_pipeline")
        report_dir = Path(content_json).parent
        digest_before = _artifact_signature(str(report_dir / "llm_digest.json"))
        audit_before = _artifact_signature(str(report_dir / "digest_audit.json"))

        prepare_cmd = steps.build_digest_prepare_cmd(content_json, overwrite=self.force_refresh)
        code, tail = await self._run_script(step_id, prepare_cmd, progress_fn=self._digest_progress)
        if code != 0 and (self.force_refresh or not (Path(pipeline_dir) / "chunk_manifest.json").exists()):
            await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
            self._mark(step_id, "failed")
            return "failed"

        auto_cmd = steps.build_digest_auto_cmd(pipeline_dir, overwrite=self.force_refresh)
        owner = steps.COMPANY_STEP_MAP[step_id].owner
        poller = asyncio.create_task(self._poll_progress(step_id, self._digest_progress))
        try:
            code, tail = await _stream_subprocess(self.run, step_id, owner, auto_cmd)
        finally:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
        done, total, _, _ = self._digest_progress()
        if code != 0 and (self.force_refresh or total == 0 or done < total):
            await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
            self._mark(step_id, "failed")
            return "failed"
        if total:
            await self._emit("step_progress", step_id, {"done": done, "total": total, "unit": "chunks"})

        merge_cmd = steps.build_digest_merge_cmd(pipeline_dir)
        code, tail = await _stream_subprocess(self.run, step_id, owner, merge_cmd)
        llm_digest = report_dir / "llm_digest.json"
        if code != 0 and (self.force_refresh or not llm_digest.exists()):
            # 缺 chunk 时 merge 抛 RuntimeError；降级为 --allow-partial 产出不完整 digest。
            await self._emit("step_log", step_id, {"line": "Merge failed; attempting --allow-partial to generate an incomplete digest"})
            code, tail = await _stream_subprocess(
                self.run, step_id, owner, steps.build_digest_merge_cmd(pipeline_dir, allow_partial=True)
            )
        digest_after = _artifact_signature(str(llm_digest))
        audit_after = _artifact_signature(str(report_dir / "digest_audit.json"))
        stale_force_refresh = self.force_refresh and (
            digest_after == digest_before or audit_after == audit_before
        )
        if code != 0 or digest_after is None or audit_after is None or stale_force_refresh:
            error = _tail_text(tail) if code != 0 else (
                "Not all digest artifacts were refreshed after force_refresh" if stale_force_refresh else "Digest artifacts are incomplete"
            )
            await self._emit("step_failed", step_id, {"error": error, "exit_code": code})
            self._mark(step_id, "failed")
            return "failed"

        await self._refresh_state()
        for name in ("llm_digest.json", "digest_audit.json"):
            path = report_dir / name
            if path.exists():
                await self._artifact(step_id, path)
        digest_audit = state_reader.load_json_dict(report_dir / "digest_audit.json")
        degraded = False
        if digest_audit and digest_audit.get("complete") is False:
            degraded = True
            self.allow_incomplete_digest = True
            await self._backflow(
                "digest_incomplete",
                step_id,
                steps.INFO_PROCESSOR,
                "digest_audit.complete=false: chunks are missing or invalid. The information processor should repair the digest; "
                "the workflow will continue and the financial evidence draft will run with --allow-incomplete-digest.",
            )
        payload: dict[str, Any] = {"summary": "LLM digest completed"}
        if degraded:
            payload["degraded"] = True
        await self._emit("step_completed", step_id, payload)
        self._mark(step_id, "degraded" if degraded else "completed")
        return "completed"

    async def _step_digest_recent(self) -> str:
        """为近期财报集合逐份构建 digest，并保留成功期间的结果。"""
        step_id = "processor_digest"
        filings = self.state.get("filings") if isinstance(self.state.get("filings"), list) else []
        pending: list[tuple[dict[str, Any], str]] = []
        for filing in filings:
            artifacts = (filing.get("processor") or {}).get("artifacts") or {}
            content = artifacts.get("content_json") or {}
            digest = artifacts.get("llm_digest_json") or {}
            audit_info = artifacts.get("digest_audit_json") or {}
            if not content.get("exists"):
                await self._emit("step_failed", step_id, {"error": f"content.json is missing for {filing.get('filing_id')}"})
                self._mark(step_id, "failed")
                return "failed"
            if self.force_refresh or not digest.get("exists") or not audit_info.get("exists"):
                pending.append((filing, str(content.get("path") or "")))

        owner = steps.COMPANY_STEP_MAP[step_id].owner
        total = len(pending)
        for index, (filing, content_json) in enumerate(pending, start=1):
            report_dir = Path(content_json).parent
            pipeline_dir = report_dir / "digest_pipeline"
            prepare_cmd = steps.build_digest_prepare_cmd(content_json, overwrite=self.force_refresh)
            code, tail = await self._run_script(step_id, prepare_cmd)
            if code != 0 and (self.force_refresh or not (pipeline_dir / "chunk_manifest.json").exists()):
                await self._emit("step_failed", step_id, {"error": _tail_text(tail), "filing_id": filing.get("filing_id")})
                self._mark(step_id, "failed")
                return "failed"
            auto_cmd = steps.build_digest_auto_cmd(str(pipeline_dir), overwrite=self.force_refresh)
            code, tail = await _stream_subprocess(self.run, step_id, owner, auto_cmd)
            if code != 0:
                await self._emit("step_failed", step_id, {"error": _tail_text(tail), "filing_id": filing.get("filing_id")})
                self._mark(step_id, "failed")
                return "failed"
            code, tail = await _stream_subprocess(
                self.run,
                step_id,
                owner,
                steps.build_digest_merge_cmd(str(pipeline_dir)),
            )
            if code != 0 and not (report_dir / "llm_digest.json").exists():
                code, tail = await _stream_subprocess(
                    self.run,
                    step_id,
                    owner,
                    steps.build_digest_merge_cmd(str(pipeline_dir), allow_partial=True),
                )
            if code != 0 or not (report_dir / "llm_digest.json").exists() or not (report_dir / "digest_audit.json").exists():
                await self._emit("step_failed", step_id, {"error": _tail_text(tail), "filing_id": filing.get("filing_id")})
                self._mark(step_id, "failed")
                return "failed"
            digest_audit = state_reader.load_json_dict(report_dir / "digest_audit.json")
            if digest_audit.get("complete") is False:
                self.allow_incomplete_digest = True
            await self._artifact(step_id, report_dir / "llm_digest.json")
            await self._artifact(step_id, report_dir / "digest_audit.json")
            await self._emit("step_progress", step_id, {"done": index, "total": total, "unit": "filings"})

        await self._refresh_state()
        refreshed = self.state.get("filings") if isinstance(self.state.get("filings"), list) else []
        missing = []
        for filing in refreshed:
            artifacts = (filing.get("processor") or {}).get("artifacts") or {}
            if not (artifacts.get("llm_digest_json") or {}).get("exists") or not (artifacts.get("digest_audit_json") or {}).get("exists"):
                missing.append(f"{filing.get('report_type')} {filing.get('report_year')}")
        if missing:
            await self._emit("step_failed", step_id, {"error": "Digest artifacts remain missing for: " + ", ".join(missing)})
            self._mark(step_id, "failed")
            return "failed"
        degraded = self.allow_incomplete_digest
        await self._emit("step_completed", step_id, {"summary": f"LLM digest is ready for {len(refreshed)} filings", "degraded": degraded})
        self._mark(step_id, "degraded" if degraded else "completed")
        return "completed"

    async def _step_rag(self) -> str:
        """执行 RAG 索引构建（完成信号 = rag_chunks.jsonl + rag_index_meta.json）。

        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "processor_rag"
        if str(self.ctx.get("filing_policy") or "") == "recent_history":
            return await self._step_rag_recent()
        content_json = self.ctx.get("content_json") or ""
        if not content_json or not Path(content_json).exists():
            await self._emit("step_started", step_id, {})
            await self._emit("step_failed", step_id, {"error": "content.json is missing; the RAG index cannot be built."})
            self._mark(step_id, "failed")
            return "failed"
        rag_dir = Path(content_json).parent / "rag_index"
        chunks = rag_dir / "rag_chunks.jsonl"
        meta = rag_dir / "rag_index_meta.json"
        chunks_before = _artifact_signature(str(chunks))
        meta_before = _artifact_signature(str(meta))
        cmd = steps.build_rag_cmd(content_json, overwrite=self.force_refresh)
        code, tail = await self._run_script(step_id, cmd)
        chunks_after = _artifact_signature(str(chunks))
        meta_after = _artifact_signature(str(meta))
        stale_force_refresh = self.force_refresh and (
            chunks_after == chunks_before or meta_after == meta_before
        )
        if code != 0 or chunks_after is None or meta_after is None or stale_force_refresh:
            error = _tail_text(tail) if code != 0 else (
                "Not all RAG artifacts were refreshed after force_refresh" if stale_force_refresh else "RAG index artifacts are incomplete"
            )
            await self._emit("step_failed", step_id, {"error": error, "exit_code": code})
            self._mark(step_id, "failed")
            return "failed"
        await self._refresh_state()
        await self._artifact(step_id, chunks)
        await self._artifact(step_id, meta)
        await self._emit("step_completed", step_id, {"summary": "RAG index is ready"})
        self._mark(step_id, "completed")
        return "completed"

    async def _step_rag_recent(self) -> str:
        """为近期财报集合逐份构建文档隔离的 RAG 索引。"""
        step_id = "processor_rag"
        filings = self.state.get("filings") if isinstance(self.state.get("filings"), list) else []
        pending: list[tuple[dict[str, Any], str]] = []
        for filing in filings:
            artifacts = (filing.get("processor") or {}).get("artifacts") or {}
            content = artifacts.get("content_json") or {}
            rag = artifacts.get("rag_chunks_jsonl") or {}
            if not content.get("exists"):
                await self._emit("step_failed", step_id, {"error": f"content.json is missing for {filing.get('filing_id')}"})
                self._mark(step_id, "failed")
                return "failed"
            if self.force_refresh or not rag.get("exists"):
                pending.append((filing, str(content.get("path") or "")))
        total = len(pending)
        for index, (filing, content_json) in enumerate(pending, start=1):
            cmd = steps.build_rag_cmd(content_json, overwrite=self.force_refresh)
            code, tail = await self._run_script(step_id, cmd)
            rag_dir = Path(content_json).parent / "rag_index"
            if code != 0 or not (rag_dir / "rag_chunks.jsonl").exists():
                await self._emit("step_failed", step_id, {"error": _tail_text(tail), "filing_id": filing.get("filing_id")})
                self._mark(step_id, "failed")
                return "failed"
            await self._artifact(step_id, rag_dir / "rag_chunks.jsonl")
            if (rag_dir / "rag_index_meta.json").exists():
                await self._artifact(step_id, rag_dir / "rag_index_meta.json")
            await self._emit("step_progress", step_id, {"done": index, "total": total, "unit": "filings"})
        await self._refresh_state()
        missing = []
        for filing in self.state.get("filings") or []:
            rag = (((filing.get("processor") or {}).get("artifacts") or {}).get("rag_chunks_jsonl") or {})
            if not rag.get("exists"):
                missing.append(f"{filing.get('report_type')} {filing.get('report_year')}")
        if missing:
            await self._emit("step_failed", step_id, {"error": "RAG indexes remain missing for: " + ", ".join(missing)})
            self._mark(step_id, "failed")
            return "failed"
        await self._emit("step_completed", step_id, {"summary": f"RAG indexes are ready for {len(self.state.get('filings') or [])} filings"})
        self._mark(step_id, "completed")
        return "completed"

    async def _step_compare(self) -> str:
        """执行摘要交叉比对（摘要 PDF 缺失时降级完成，不阻塞主线）。

        参数：
            无。
        返回值：
            步骤终态（本步骤永不返回 failed）。
        """
        step_id = "processor_compare"
        if str(self.ctx.get("filing_policy") or "") == "recent_history":
            return await self._step_compare_recent()
        content_json = self.ctx.get("content_json") or ""
        comparison = Path(content_json).parent / "summary_comparison.json" if content_json else None
        if not content_json or not Path(content_json).exists():
            await self._emit("step_started", step_id, {})
            await self._emit("step_completed", step_id, {"summary": "content.json is missing; summary comparison was skipped with limitations", "degraded": True})
            self._mark(step_id, "degraded")
            return "completed"
        cmd = steps.build_compare_cmd(content_json)
        code, tail = await self._run_script(step_id, cmd)
        await self._refresh_state()
        if comparison and comparison.exists():
            await self._artifact(step_id, comparison)
            payload: dict[str, Any] = {"summary": "Summary cross-check completed"}
            if code != 0:
                payload["degraded"] = True
            await self._emit("step_completed", step_id, payload)
            self._mark(step_id, "completed" if code == 0 else "degraded")
            return "completed"
        # 摘要 PDF 找不到属于可接受的降级场景：发出 warning 语义的降级完成。
        await self._emit(
            "step_completed",
            step_id,
            {"summary": f"Summary comparison failed and was downgraded ({_tail_text(tail, limit=200)})", "degraded": True},
        )
        self._mark(step_id, "degraded")
        return "completed"

    async def _step_compare_recent(self) -> str:
        """只对存在正式摘要的年报执行摘要比对，中报明确记为不适用。"""
        step_id = "processor_compare"
        filings = self.state.get("filings") if isinstance(self.state.get("filings"), list) else []
        pending: list[tuple[dict[str, Any], str]] = []
        for filing in filings:
            if filing.get("summary_comparison") != "required":
                continue
            artifacts = (filing.get("processor") or {}).get("artifacts") or {}
            content = artifacts.get("content_json") or {}
            comparison = artifacts.get("summary_comparison_json") or {}
            if self.force_refresh or not comparison.get("exists"):
                pending.append((filing, str(content.get("path") or "")))
        total = len(pending)
        degraded = False
        for index, (filing, content_json) in enumerate(pending, start=1):
            if not content_json or not Path(content_json).exists():
                degraded = True
                continue
            cmd = steps.build_compare_cmd(content_json)
            code, tail = await self._run_script(step_id, cmd)
            comparison = Path(content_json).parent / "summary_comparison.json"
            if code != 0 or not comparison.exists():
                degraded = True
                await self._emit("step_log", step_id, {"line": f"Summary comparison was unavailable for {filing.get('filing_id')}: {_tail_text(tail, limit=200)}"})
            else:
                await self._artifact(step_id, comparison)
            await self._emit("step_progress", step_id, {"done": index, "total": total, "unit": "annual filings"})
        await self._refresh_state()
        await self._emit(
            "step_completed",
            step_id,
            {"summary": f"Summary comparison applicability was checked for {len(filings)} filings", "degraded": degraded},
        )
        self._mark(step_id, "degraded" if degraded else "completed")
        return "completed"

    async def _step_draft(self) -> str:
        """执行财务证据草稿，并按证据核验结果触发回流提示。

        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "financial_evidence_draft"
        if str(self.ctx.get("filing_policy") or "") == "recent_history":
            research_state_path = str(self.ctx.get("research_state_path") or "")
            if not research_state_path or not Path(research_state_path).exists():
                await self._emit("step_started", step_id, {})
                await self._emit("step_failed", step_id, {"error": "research_state.json is missing; the filing-set handoff cannot be generated."})
                self._mark(step_id, "failed")
                return "failed"
            code, tail = await self._run_script(step_id, steps.build_filing_set_cmd(research_state_path))
            if code != 0:
                await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
                self._mark(step_id, "failed")
                return "failed"
            await self._refresh_state()
            filing_set_path = str(self.ctx.get("filing_set_path") or "")
            if not filing_set_path or not Path(filing_set_path).exists():
                await self._emit("step_failed", step_id, {"error": "filing_set.json was not found after generation."})
                self._mark(step_id, "failed")
                return "failed"
            await self._artifact(step_id, filing_set_path)
            await self._emit("step_completed", step_id, {"summary": f"Multi-period filing-set handoff generated for {len(self.state.get('filings') or [])} filings"})
            self._mark(step_id, "completed")
            return "completed"

        report_dir = self.ctx.get("report_dir") or ""
        if not report_dir or not Path(report_dir).exists():
            await self._emit("step_started", step_id, {})
            await self._emit("step_failed", step_id, {"error": "The information-processor report directory is missing; the financial evidence draft cannot be generated."})
            self._mark(step_id, "failed")
            return "failed"
        digest_audit = state_reader.load_json_dict(Path(self.ctx.get("digest_audit_path") or ""))
        allow_incomplete = self.allow_incomplete_digest or digest_audit.get("complete") is False
        cmd = steps.build_financial_cmd(report_dir, self.depth, self.focus, allow_incomplete)
        code, tail = await self._run_script(step_id, cmd)
        if code != 0 and not allow_incomplete:
            # digest 不完整会让脚本直接抛 RuntimeError；补宽容开关重试一次。
            await self._emit("step_log", step_id, {"line": "Draft generation failed; retrying with --allow-incomplete-digest"})
            cmd = steps.build_financial_cmd(report_dir, self.depth, self.focus, True)
            code, tail = await self._run_script(step_id, cmd)
        if code != 0:
            await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
            self._mark(step_id, "failed")
            return "failed"
        await self._refresh_state()
        analyst_dir = Path(self.ctx.get("analyst_dir") or "")
        for name in ("analyst_report.json", "analyst_report.md", "evidence_check.json", "analyst_audit.json"):
            path = analyst_dir / name
            if path.exists():
                await self._artifact(step_id, path)
        # 证据覆盖率与阻塞性补证请求只提示回流，不自动重跑上游。
        evidence = state_reader.load_json_dict(analyst_dir / "evidence_check.json")
        summary = evidence.get("summary", {}) if isinstance(evidence, dict) else {}
        checked = float(summary.get("checked_total") or 0)
        verified = float(summary.get("verified_total") or 0)
        if checked > 0 and verified / checked < 0.6:
            await self._backflow(
                "evidence_low",
                step_id,
                steps.INFO_PROCESSOR,
                f"Evidence verification passed {verified:.0f}/{checked:.0f}, below 60%. The information processor should add evidence and rerun the draft.",
            )
        audit_info = state_reader.load_json_dict(analyst_dir / "analyst_audit.json")
        blocking = int(audit_info.get("upstream_requests_blocking") or 0)
        if blocking > 0:
            await self._backflow(
                "draft_blocking",
                step_id,
                steps.INFO_PROCESSOR,
                f"The financial evidence draft contains {blocking} blocking evidence requests. The information processor should handle them first.",
            )
        await self._emit("step_completed", step_id, {"summary": "Financial evidence draft completed"})
        self._mark(step_id, "completed")
        return "completed"

    async def _step_formal(self) -> str:
        """执行正式财务分析（LLM 步骤）。

        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "formal_financial_analysis"
        prompt_ctx = dict(self.ctx)
        prompt_ctx.update({"depth": self.depth, "focus": self.focus})
        pack = steps.build_formal_financial_analysis_prompt(prompt_ctx)
        groups = [list(pack["expected_artifacts"])]
        outcome = await self._run_llm_step(step_id, pack, groups)
        if outcome == "completed":
            await self._refresh_state()
        return outcome

    async def _step_market_context(self) -> str:
        """执行市场上下文采集（与主线并行）。

        功能：
            无 Bocha API key 时自动加 --dry-run 并标记 degraded；
            进度用 cache/queries/<as_of_date>/ 新增文件数近似；
            包状态为 missing 系列时同样降级但不失败。
        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "market_context_update"
        try:
            if await self._consume_skip_signal(step_id):
                return "skipped"
            key_present = config.bocha_key_present()
            dry_run = not key_present
            cmd = steps.build_market_context_cmd(
                target=self.run.params.get("target") or self.ctx.get("stock_code", ""),
                stock_code=self.ctx.get("stock_code", ""),
                company_name=self.ctx.get("company_name", ""),
                as_of_date=self.as_of_date,
                depth=self.depth,
                focus=self.focus,
                freshness=self.freshness,
                dry_run=dry_run,
                force_refresh=bool(self.run.params.get("force_refresh")),
                # 公司研究无论是当前日还是历史日都执行严格截止，避免目录日期正确、内容却含未来网页。
                strict_cutoff=True,
            )
            queries_dir = config.MARKET_CONTEXT_WORKSPACE / "cache" / "queries" / self.as_of_date
            baseline = len(list(queries_dir.glob("*"))) if queries_dir.exists() else 0
            estimate = config.MARKET_CONTEXT_QUERY_ESTIMATE.get(self.depth, 12)

            def progress() -> tuple[int, int, str, str]:
                count = len(list(queries_dir.glob("*"))) if queries_dir.exists() else 0
                done = max(0, count - baseline)
                # 查询数只是近似进度：新增数可能超过估计值，此时抬高分母避免超过 100%。
                return min(done, max(estimate, done)), max(estimate, done), "queries", "Approximate progress based on cached query count"

            code, tail = await self._run_script(step_id, cmd, progress_fn=progress)
            if dry_run:
                await self._emit(
                    "step_log", step_id, {"line": "Bocha API key was not detected; generated a query plan with --dry-run (limited delivery)"}
                )
            if code != 0:
                await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
                self._mark(step_id, "failed")
                return "failed"
            await self._refresh_state()
            package_dir = Path(self.ctx.get("market_package_dir") or "")
            package = state_reader.load_json_dict(package_dir / "market_context_package.json")
            for name in (
                "market_context_package.json",
                "market_context_package.md",
                "market_context_sources.json",
                "collection_audit.json",
            ):
                path = package_dir / name
                if path.exists():
                    await self._artifact(step_id, path)
            quality_gate = package.get("quality_gate") if isinstance(package.get("quality_gate"), dict) else {}
            usage_boundary = package.get("usage_boundary") if isinstance(package.get("usage_boundary"), dict) else {}
            degraded = bool(
                dry_run
                or str(package.get("status") or "") != _GOOD_MARKET_STATUS
                or quality_gate.get("can_support_market_expectation_proxy") is not True
                or usage_boundary.get("data_type") != "public_web_search_proxy"
            )
            payload: dict[str, Any] = {"summary": f"Market-context package status: {package.get('status') or 'unknown'}"}
            if degraded:
                payload["degraded"] = True
            await self._emit("step_completed", step_id, payload)
            self._mark(step_id, "degraded" if degraded else "completed")
            return "completed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Run %s market-context step failed", self.run.run_id)
            await self._emit("step_failed", step_id, {"error": str(exc)})
            self._mark(step_id, "failed")
            return "failed"

    async def _step_valuation_gate(self, plan_map: dict[str, dict[str, Any]]) -> None:
        """估值步骤的闸门：前置依赖满足才进入 LLM 等待。

        功能：
            估值依赖正式财务分析完成（或因复用跳过）；formal 运行期失败/
            被跳过时估值以 step_skipped 让路，避免让估值分析员在缺输入
            的情况下硬给目标价。
        参数：
            plan_map: step_id → 计划条目。
        返回值：
            无。
        """
        step_id = "valuation_update"
        if plan_map[step_id]["status"] == "skipped":
            return
        if await self._consume_skip_signal(step_id):
            return
        formal_status = self.run.step_status.get("formal_financial_analysis", "skipped_plan")
        if formal_status not in {"completed", "skipped_plan"}:
            await self._emit(
                "step_skipped", step_id, {"reason": f"Upstream formal financial analysis status is {formal_status}; valuation was skipped because required inputs are missing"}
            )
            self._mark(step_id, "skipped")
            return
        await self._step_valuation()

    def _valuation_groups(self) -> list[list[str]]:
        """构建估值期望产物的两组候选路径（新旧目录布局都监视）。

        参数：
            无。
        返回值：
            产物路径组列表。
        """
        primary = Path(self.ctx.get("valuation_dir") or "")
        legacy = Path(self.ctx.get("valuation_dir_legacy") or "")
        groups: list[list[str]] = []
        for base in (primary, legacy):
            if str(base):
                groups.append([str(base / name) for name in _VALUATION_FILES])
        return groups or [[str(Path(name)) for name in _VALUATION_FILES]]

    async def _check_valuation_backflow(self) -> None:
        """轮询估值目录中的 upstream_request.json 并发出回流事件。

        参数：
            无。
        返回值：
            无。
        """
        for key in ("valuation_dir", "valuation_dir_legacy"):
            base = self.ctx.get(key) or ""
            if not base:
                continue
            path = Path(base) / "upstream_request.json"
            if path.exists():
                payload = state_reader.load_json_dict(path)
                owners: list[str] = []
                for request in payload.get("requests", []) if isinstance(payload.get("requests"), list) else []:
                    owner = str(request.get("owner") or "").strip()
                    if owner and owner not in owners:
                        owners.append(owner)
                await self._backflow(
                    "valuation_upstream",
                    "valuation_update",
                    ", ".join(owners) or steps.FINANCIAL_ANALYST,
                    f"The valuation analyst produced upstream_request.json ({path}); upstream evidence is required before continuing.",
                )
                return

    async def _step_valuation(self) -> str:
        """执行估值更新（LLM 步骤，两处目录任一凑齐四件套即完成）。

        参数：
            无。
        返回值：
            步骤终态。
        """
        step_id = "valuation_update"
        prompt_ctx = dict(self.ctx)
        prompt_ctx.update(
            {
                "as_of_date": self.as_of_date,
                "market_context_package_path": (
                    str(Path(self.ctx.get("market_package_dir") or "") / "market_context_package.json")
                    if self.ctx.get("market_package_dir")
                    else ""
                ),
            }
        )
        pack = steps.build_valuation_prompt(prompt_ctx)
        outcome = await self._run_llm_step(
            step_id, pack, self._valuation_groups(), on_poll=self._check_valuation_backflow
        )
        if outcome == "completed":
            await self._refresh_state()
        return outcome

    async def _run_llm_step(
        self,
        step_id: str,
        pack: dict[str, Any],
        groups: list[list[str]],
        on_poll: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        """LLM 步骤统一执行器（manual / claude_cli 两种模式）。

        功能：
            - claude_cli：直接 spawn claude CLI，stdout 流入 step_log；
              进程退出后复查产物，不齐则转 step_waiting_llm 继续等待；
            - manual：先发 step_waiting_llm（含可复制提示词与期望产物），
              每 2s 轮询产物/手动信号；
            - 完成判定永远以期望产物组任一齐全为准。
        参数：
            step_id: 步骤标识。
            pack: {instructions, prompt, expected_artifacts}。
            groups: 期望产物路径组（任一组齐全即完成）。
            on_poll: 每次轮询附加检查（估值回流检测）。
        返回值：
            步骤终态：completed/skipped/failed。
        """
        run = self.run
        step_def = steps.COMPANY_STEP_MAP.get(step_id) or steps.INDUSTRY_STEP_MAP.get(step_id)
        owner = step_def.owner if step_def else None
        baseline = {path: _artifact_signature(path) for group in groups for path in group}
        await self.bus.publish(run.run_id, "step_started", step_id, owner, {})
        run.llm_artifact_groups[step_id] = groups
        run.llm_artifact_baselines[step_id] = baseline
        # LLM 执行/等待期间仍可消费手动跳过；这与确定性脚本仅允许“开始前跳过”不同。
        run.skip_accepting_steps.add(step_id)

        async def finalize(note: str, degraded: bool = False) -> str:
            group = _complete_group(groups, baseline) or []
            for path in group:
                await self._artifact(step_id, path)
            payload: dict[str, Any] = {"summary": note, "artifacts": group}
            if degraded:
                payload["degraded"] = True
            await self.bus.publish(run.run_id, "step_completed", step_id, owner, payload)
            self._mark(step_id, "degraded" if degraded else "completed")
            run.llm_artifact_groups.pop(step_id, None)
            run.llm_artifact_baselines.pop(step_id, None)
            run.skip_accepting_steps.discard(step_id)
            return "completed"

        waiting_payload = {
            "instructions": pack["instructions"],
            "prompt": pack["prompt"],
            "expected_artifacts": pack["expected_artifacts"],
            "claude_cmd": steps.format_claude_cmd(pack["prompt"]),
        }

        if run.llm_mode == "claude_cli" and config.ENABLE_CLAUDE_CLI:
            claude_path = _resolve_claude_executable()
            if not claude_path:
                await self.bus.publish(
                    run.run_id, "step_log", step_id, owner, {"line": "Claude CLI not found; falling back to manual waiting mode"}
                )
            else:
                cmd = [claude_path, "-p", pack["prompt"], "--permission-mode", "acceptEdits"]
                await self.bus.publish(
                    run.run_id, "step_log", step_id, owner, {"line": "Starting Claude CLI for the LLM step…"}
                )
                # 剥离 CLAUDE* 环境变量，避免嵌套 Claude Code 会话互相干扰。
                code, _tail = await _stream_subprocess(
                    run, step_id, owner, cmd, strip_claude_env=True, timeout=config.LLM_WAIT_TIMEOUT_SECONDS
                )
                await self.bus.publish(
                    run.run_id, "step_log", step_id, owner, {"line": f"Claude CLI exited with code {code}; rechecking expected artifacts"}
                )
                if _complete_group(groups, baseline):
                    return await finalize("Claude CLI artifacts are complete; step completed")

        await self.bus.publish(run.run_id, "step_waiting_llm", step_id, owner, waiting_payload)
        deadline = asyncio.get_running_loop().time() + config.LLM_WAIT_TIMEOUT_SECONDS
        while True:
            signal = run.manual_signals.pop(step_id, None)
            if signal == "skip":
                await self.bus.publish(run.run_id, "step_skipped", step_id, owner, {"reason": "Skipped manually by the user"})
                self._mark(step_id, "skipped")
                run.llm_artifact_groups.pop(step_id, None)
                run.llm_artifact_baselines.pop(step_id, None)
                run.skip_accepting_steps.discard(step_id)
                return "skipped"
            if signal == "complete_force":
                await self.bus.publish(
                    run.run_id,
                    "step_completed",
                    step_id,
                    owner,
                    {"summary": "User forced completion while expected artifacts were incomplete; delivery downgraded", "degraded": True},
                )
                # 依赖状态仍记 completed，以便用户强制放行估值；同时单独登记
                # degraded，确保最终 run 诚实汇总为 partial。
                self._mark(step_id, "completed")
                run.degraded_steps.add(step_id)
                run.llm_artifact_groups.pop(step_id, None)
                run.llm_artifact_baselines.pop(step_id, None)
                run.skip_accepting_steps.discard(step_id)
                return "completed"
            if signal == "complete" or _complete_group(groups, baseline):
                return await finalize("Expected artifacts were written; step completed automatically")
            if on_poll is not None:
                await on_poll()
            if asyncio.get_running_loop().time() > deadline:
                await self.bus.publish(
                    run.run_id, "step_failed", step_id, owner, {"error": "Timed out waiting for LLM artifacts. Complete manually or skip and retry."}
                )
                self._mark(step_id, "failed")
                run.llm_artifact_groups.pop(step_id, None)
                run.llm_artifact_baselines.pop(step_id, None)
                run.skip_accepting_steps.discard(step_id)
                return "failed"
            await asyncio.sleep(config.LLM_POLL_INTERVAL_SECONDS)

    async def _step_deliver(self) -> dict[str, Any]:
        """合成结论卡（deliver 步骤）。

        参数：
            无。
        返回值：
            run_completed.payload.summary 字典。
        """
        step_id = "deliver"
        await self._emit("step_started", step_id, {})
        state = self.state or {}
        layers = state.get("layers", {})

        def layer_artifact(layer: str, key: str, fallback: Path | None) -> Path | None:
            info = layers.get(layer, {}).get("artifacts", {}).get(key, {})
            if isinstance(info, dict) and info.get("exists"):
                return Path(str(info.get("path")))
            return fallback

        valuation_json = layer_artifact(
            "valuation",
            "valuation_report_json",
            Path(self.ctx["valuation_dir"]) / "valuation_report.json" if self.ctx.get("valuation_dir") else None,
        )
        formal_json = layer_artifact(
            "formal_financial_analysis",
            "formal_financial_analysis_json",
            Path(self.ctx["formal_json_path"]) if self.ctx.get("formal_json_path") else None,
        )
        market_json = layer_artifact(
            "market_context",
            "market_context_package_json",
            Path(self.ctx["market_package_dir"]) / "market_context_package.json"
            if self.ctx.get("market_package_dir")
            else None,
        )
        summary = await asyncio.to_thread(
            state_reader.build_company_summary,
            state,
            state_reader.load_json_dict(valuation_json) if valuation_json else {},
            state_reader.load_json_dict(formal_json) if formal_json else {},
            state_reader.load_json_dict(market_json) if market_json else {},
        )
        await self._emit("step_completed", step_id, {"summary": "Conclusion card generated"})
        self._mark(step_id, "completed")
        return summary


def _state_projection(state: dict[str, Any] | None) -> dict[str, Any]:
    """提取前端真正关心的 research_state 投影，用于语义去重。"""
    state = state or {}
    summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
    return {
        "layer_statuses": summary.get("layer_statuses") or {},
        "reusable": state.get("reusable") if isinstance(state.get("reusable"), dict) else {},
        "next_actions": state.get("next_actions") if isinstance(state.get("next_actions"), list) else [],
    }


def _state_signature(state: dict[str, Any] | None) -> str:
    """为状态投影生成稳定签名；generated_at/mtime 变化不会制造重复事件。"""
    return json.dumps(_state_projection(state), ensure_ascii=False, sort_keys=True, default=str)


def _state_artifacts(state: dict[str, Any] | None) -> list[tuple[str, str, str]]:
    """从六层 artifacts 中提取已存在文件的 (layer, key, path)。"""
    found: list[tuple[str, str, str]] = []
    layers = (state or {}).get("layers") if isinstance(state, dict) else {}
    if not isinstance(layers, dict):
        return found
    for layer_name, layer in layers.items():
        artifacts = layer.get("artifacts") if isinstance(layer, dict) else {}
        if not isinstance(artifacts, dict):
            continue
        for key, info in artifacts.items():
            if not isinstance(info, dict) or not info.get("exists"):
                continue
            path = str(info.get("path") or "").strip()
            if path:
                found.append((str(layer_name), str(key), path))
    return found


def _artifact_step_for_layer(layer: str, key: str) -> str | None:
    """把状态层产物映射到兼容的固定 step_id，仅用于前端 owner/分组展示。"""
    if layer == "collector":
        return "collector_fetch"
    if layer == "processor":
        lowered = key.lower()
        if "digest" in lowered:
            return "processor_digest"
        if "rag" in lowered:
            return "processor_rag"
        if "comparison" in lowered or "summary" in lowered:
            return "processor_compare"
        return "processor_parse"
    return {
        "financial_evidence_draft": "financial_evidence_draft",
        "formal_financial_analysis": "formal_financial_analysis",
        "valuation": "valuation_update",
        "market_context": "market_context_update",
    }.get(layer)

class _PythonAgentCoordinatorPipeline(_CompanyPipeline):
    """Python 主会话公司研究流水线：精确 agent I/O，事件实时进入控制台 SSE。

    调度权在 Python（``company_research_coordinator.run_company_research``）；
    每一步脚本/agent 的输入输出路径、校验结果通过 EventBus 推送；
    不启动 Claude Code 主会话 /rec。
    """

    async def execute(self) -> str:
        """执行 Python agent 协调器路径并冻结结论卡。"""
        run = self.run
        run.execution_mode = "python_agent_coordinator"
        run.persist_meta()
        await self._emit(
            "run_started",
            None,
            {
                "mode": run.mode,
                "params": run.params,
                "llm_mode": run.llm_mode,
                "execution_mode": run.execution_mode,
            },
        )
        if not config.AUDIT_SCRIPT.exists():
            error = f"Required orchestration scripts are missing: audit: {config.AUDIT_SCRIPT}"
            await self._emit("run_error", None, {"error": error})
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"

        await self._emit(
            "coordinator_message",
            None,
            {
                "text": (
                    "Starting Python-owned agent coordinator "
                    "(explicit plan + registered agents with validated I/O)"
                ),
                "partial": False,
            },
        )

        # 协调器诊断目录落在本次 console run 下，不污染正式研究工作区。
        coord_workspace = run.run_dir / "python_agent_coordinator"
        coord_workspace.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()
        # 线程 → 主循环：把同步协调器事件桥接到 async EventBus。
        pending_bridge: list[asyncio.Future[Any]] = []

        def event_sink(event: dict[str, Any]) -> None:
            """同步回调：把协调器事件调度到 EventBus（线程安全）。"""
            event_type = str(event.get("type") or "")
            if not event_type:
                return
            step_id = event.get("step_id")
            owner = event.get("owner")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

            async def _publish() -> None:
                # plan 同步到 run，便于前端路线图与 meta 恢复。
                if event_type == "plan_ready":
                    steps_payload = payload.get("steps")
                    if isinstance(steps_payload, list):
                        run.plan = steps_payload
                        for item in steps_payload:
                            if isinstance(item, dict) and item.get("status") == "skipped":
                                self._mark(str(item.get("step_id") or ""), "skipped_plan")
                if event_type in {"step_completed", "step_failed", "step_skipped"} and step_id:
                    status_map = {
                        "step_completed": "completed",
                        "step_failed": "failed",
                        "step_skipped": "skipped",
                    }
                    if payload.get("degraded"):
                        self._mark(str(step_id), "degraded")
                    else:
                        self._mark(str(step_id), status_map.get(event_type, "completed"))
                await self.bus.publish(
                    run.run_id,
                    event_type,
                    str(step_id) if step_id else None,
                    str(owner) if owner else None,
                    payload,
                )

            fut = asyncio.run_coroutine_threadsafe(_publish(), loop)
            pending_bridge.append(fut)

        try:
            from research_console import company_research_coordinator as coord_mod
        except Exception as exc:  # noqa: BLE001
            await self._emit("run_error", None, {"error": f"Failed to import python coordinator: {exc}"})
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"

        # Claude 可执行文件：params 覆盖 > 配置 > 本机 native 安装路径 > PATH。
        candidate_bins = [
            str(run.params.get("claude_bin") or "").strip(),
            str(getattr(config, "CLAUDE_BIN", "") or "").strip(),
            str(Path.home() / ".local" / "bin" / "claude.exe"),
            "claude",
        ]
        claude_bin = next((item for item in candidate_bins if item), "claude")

        def _run_sync():
            return coord_mod.run_company_research(
                dict(run.params),
                workspace=coord_workspace,
                claude_bin=claude_bin,
                tool_restriction="permission",
                llm_timeout_seconds=int(run.params.get("llm_timeout_seconds") or 1800),
                max_budget_usd=float(run.params.get("max_budget_usd") or 5.0),
                run_scripts=run.params.get("run_scripts", True) is not False,
                run_llm_agents=run.params.get("run_llm_agents", True) is not False,
                auto_fallback_tool_mode=True,
                event_sink=event_sink,
            )

        try:
            coord_result = await asyncio.to_thread(_run_sync)
        except Exception as exc:  # noqa: BLE001
            logger.exception("python_agent_coordinator failed")
            await self._emit("run_error", None, {"error": str(exc)})
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"
        finally:
            # 等待桥接事件全部落盘，避免 run_completed 抢在 step 事件之前。
            if pending_bridge:
                await asyncio.gather(
                    *[asyncio.wrap_future(item) for item in pending_bridge],
                    return_exceptions=True,
                )

        # 把协调器最终 research_state 灌回 pipeline 上下文，供 deliver 抽结论卡。
        if isinstance(coord_result.research_state, dict):
            self.state = coord_result.research_state
            self._refresh_ctx()
            self._mark("final_audit", "completed")
        if coord_result.research_state_path:
            await self._artifact("final_audit", coord_result.research_state_path)
        report_path = coord_workspace / "company_research_report.json"
        if report_path.exists():
            await self._artifact("deliver", str(report_path))

        summary = await self._step_deliver()
        status = str(coord_result.final_status or "failed")
        if status == "reused":
            # 控制台终态枚举用 completed/partial/failed；全复用视为 completed。
            status = "completed"
        if status not in {"completed", "partial", "failed", "cancelled"}:
            status = "partial" if coord_result.passed else "failed"
        if coord_result.errors and status == "completed":
            status = "partial"
        summary, frozen_status = await _freeze_company_decision_before_terminal(run, summary, status)
        status = frozen_status
        if coord_result.errors:
            await self._emit(
                "coordinator_message",
                None,
                {
                    "text": "Python coordinator finished with issues: " + "; ".join(coord_result.errors),
                    "partial": False,
                    "warning": True,
                },
            )
        if status == "failed":
            await self._emit(
                "run_error",
                None,
                {"error": "; ".join(coord_result.errors) or "Python agent coordinator failed"},
            )
        await self.bus.publish(
            run.run_id,
            "handoff",
            owner=_COORDINATOR_OWNER,
            payload={
                "kind": "final_delivery",
                "from_owner": _COORDINATOR_OWNER,
                "to_owner": _COORDINATOR_OWNER,
                "from_station": "dispatch",
                "to_station": "deliver",
                "label": "Complete Conclusion-First Report",
                "status": status,
                "execution_mode": "python_agent_coordinator",
            },
        )
        await self._emit("run_completed", None, {"status": status, "summary": summary})
        return status


async def _freeze_company_decision_before_terminal(
    run: Run,
    summary: dict[str, Any],
    status: str,
) -> tuple[dict[str, Any], str]:
    """在唯一 run_completed 之前冻结公司决策并发布 artifact_created。

    快照失败不能让已经形成的研究结论消失，因此保留 summary 并把 completed 降级为
    partial；原本就是 partial/failed 时维持更弱状态。review 不经过 EventBus，只有这份
    首次决策快照属于运行终态前的权威产物。
    """
    if not run.run_dir:
        # 纯内存单测与嵌入式调用没有持久化目录，不把测试夹具误判成产品冻结失败。
        return summary, status
    decision = copy.deepcopy(summary)
    synthetic_terminal = {
        "type": "run_completed",
        "ts": state_reader.now_iso(),
        "payload": {"status": status, "summary": decision},
    }
    try:
        snapshot = history.build_decision_snapshot(
            run.run_id,
            run.mode,
            run.params,
            [*run.bus.events, synthetic_terminal],
        )
        _authoritative, _created = await asyncio.to_thread(
            history.freeze_decision_snapshot,
            run.run_dir,
            snapshot,
        )
        path = history.snapshot_path(run.run_dir)
        await run.bus.publish(
            run.run_id,
            "artifact_created",
            step_id="deliver",
            owner=_COORDINATOR_OWNER,
            payload={
                "path": str(path),
                "name": path.name,
                "kind": "json",
                "producer_owner": _COORDINATOR_OWNER,
                "delivery_to": _COORDINATOR_OWNER,
                "source": "decision_freeze",
            },
        )
        return decision, status
    except Exception as exc:  # noqa: BLE001 - 冻结故障必须转为可交付降级，不能击穿终态
        warning = f"Historical decision snapshot freezing failed: {exc}"
        gaps = decision.get("gaps") if isinstance(decision.get("gaps"), list) else []
        if warning not in gaps:
            decision["gaps"] = [*gaps, warning]
        await run.bus.publish(
            run.run_id,
            "run_error",
            owner=_COORDINATOR_OWNER,
            payload={"error": warning},
        )
        return decision, "partial" if status == "completed" else status


def _tail_text(tail: list[str], limit: int = 600) -> str:
    """把子进程尾部日志压缩成一段错误描述。

    参数：
        tail: 尾部日志行。
        limit: 最大字符数。
    返回值：
        末尾若干行拼接的文本。
    """
    text = " | ".join(line for line in tail[-8:] if line.strip())
    return text[-limit:] if text else "No output"


async def _company_pipeline(run: Run) -> str:
    """公司流水线入口。

    - python_agent_coordinator：Python 主会话按 plan 调度脚本与注册表 agent（默认）；
    - 其他模式继续走原静态 DAG（manual / 分步 claude_cli / skip）。
    Claude Code 主会话 /rec（coordinator_cli）已移除。
    """
    if run.llm_mode == "python_agent_coordinator":
        return await _PythonAgentCoordinatorPipeline(run).execute()
    return await _CompanyPipeline(run).execute()


# ---------------------------------------------------------------------------
# 行业流水线
# ---------------------------------------------------------------------------

class _IndustryPipeline:
    """行业研究链路执行器（收集 → 校验 → LLM 研究 → 交付）。

    参数：
        run: 运行实例。
    返回值：
        实例；execute() 返回最终 run 状态。
    """

    def __init__(self, run: Run):
        self.run = run
        self.bus = run.bus
        self.package_json: Path | None = None
        self.package_dir: Path | None = None
        # 复用公司管线的通用小工具（回流、标记、LLM 执行器）以避免重复实现。
        self._proxy = _CompanyPipeline(run)

    async def _emit(self, event_type: str, step_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        """发布事件（owner 取行业步骤定义）。

        参数：
            event_type: 事件类型。
            step_id: 步骤标识。
            payload: 负载。
        返回值：
            无。
        """
        owner = steps.INDUSTRY_STEP_MAP[step_id].owner if step_id and step_id in steps.INDUSTRY_STEP_MAP else None
        await self.bus.publish(self.run.run_id, event_type, step_id, owner, payload or {})

    async def execute(self) -> str:
        """执行行业研究链路。

        参数：
            无。
        返回值：
            最终 run 状态：completed/partial/failed。
        """
        run = self.run
        await self._emit("run_started", None, {"mode": run.mode, "params": run.params, "llm_mode": run.llm_mode})
        broken = config.missing_scripts()
        if broken:
            await self._emit("run_error", None, {"error": "Required orchestration scripts are missing: " + "; ".join(broken)})
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"
        plan = steps.build_industry_plan(run.llm_mode)
        run.plan = plan
        for item in plan:
            if item["status"] == "skipped":
                run.step_status[item["step_id"]] = "skipped_plan"
                run.llm_mode_skipped = True
        run.skip_accepting_steps = {
            item["step_id"]
            for item in plan
            if item["status"] == "pending" and item["step_id"] in _INDUSTRY_SKIP_CONSUMERS
        }
        await self._emit("plan_ready", None, {"steps": plan})

        if not await self._step_collect():
            await self._emit("run_completed", None, {"status": "failed"})
            return "failed"
        await self._step_validate()
        plan_map = {item["step_id"]: item for item in plan}
        if plan_map["industry_research"]["status"] != "skipped":
            if not await self._proxy._consume_skip_signal("industry_research"):
                await self._step_research()
        summary = await self._step_deliver()
        status = self._proxy._final_status()
        await self._emit("run_completed", None, {"status": status, "summary": summary})
        return status

    async def _step_collect(self) -> bool:
        """执行行业输入包收集，并定位新产出的包目录。

        参数：
            无。
        返回值：
            成功返回 True；失败（含找不到包）返回 False。
        """
        step_id = "industry_collect"
        if await self._proxy._consume_skip_signal(step_id):
            # 收集被跳过时后续步骤只能基于已有包，尝试复用最新包。
            self._locate_package(since=None)
            return self.package_json is not None
        cmd = steps.build_industry_collect_cmd(self.run.params)
        await self._emit("step_started", step_id, {"cmd": steps.cmd_display(cmd)})
        started_at = _dt.datetime.now().timestamp() - 2.0
        code, tail = await _stream_subprocess(self.run, step_id, steps.INDUSTRY_STEP_MAP[step_id].owner, cmd)
        if code != 0:
            await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
            self.run.failed_steps.append(step_id)
            return False
        self._locate_package(since=started_at)
        if not self.package_json:
            await self._emit("step_failed", step_id, {"error": "Collection completed but industry_input_package.json was not found"})
            self.run.failed_steps.append(step_id)
            return False
        for name in [self.package_json.name, self.package_json.stem + ".md", "evidence_table.json"]:
            path = self.package_dir / name if self.package_dir else None
            if path and path.exists():
                await self._proxy._artifact(step_id, path)
        await self._emit("step_completed", step_id, {"summary": f"Industry input package: {self.package_dir}"})
        self.run.step_status[step_id] = "completed"
        return True

    def _locate_package(self, since: float | None) -> None:
        """在行业工作区定位输入包（slug 可能是中文，按 mtime 选最新）。

        参数：
            since: 只接受该时间戳之后修改的包；None 表示接受任何最新包。
        返回值：
            无（结果写入 self.package_json / self.package_dir）。
        """
        packages_root = config.INDUSTRY_COLLECTOR_WORKSPACE / "packages"
        candidates: list[tuple[float, Path]] = []
        if packages_root.exists():
            for found in packages_root.glob("*/*/*input_package.json"):
                try:
                    mtime = found.stat().st_mtime
                except OSError:
                    continue
                # 标准名 industry_input_package.json 优先：加一个极小的排序权重。
                weight = 0.001 if found.name == "industry_input_package.json" else 0.0
                candidates.append((mtime + weight, found))
        candidates.sort(reverse=True)
        for mtime, found in candidates:
            if since is None or mtime >= since:
                self.package_json = found
                self.package_dir = found.parent
                return
        if candidates and since is not None:
            # 没有比步骤开始更新的包时退回全局最新包，至少让链路可以继续。
            self.package_json = candidates[0][1]
            self.package_dir = candidates[0][1].parent

    async def _step_validate(self) -> None:
        """执行行业包校验（validation_result.json 存在即算有效产出）。

        参数：
            无。
        返回值：
            无。
        """
        step_id = "industry_validate"
        if await self._proxy._consume_skip_signal(step_id):
            return
        deliverable = str(self.run.params.get("deliverable_type") or "")
        cmd = steps.build_industry_validate_cmd(str(self.package_json), deliverable)
        await self._emit("step_started", step_id, {"cmd": steps.cmd_display(cmd)})
        code, tail = await _stream_subprocess(self.run, step_id, steps.INDUSTRY_STEP_MAP[step_id].owner, cmd)
        result = (self.package_dir / "validation_result.json") if self.package_dir else None
        if result and result.exists():
            await self._proxy._artifact(step_id, result)
            payload: dict[str, Any] = {"summary": "Industry package validation completed"}
            if code != 0:
                payload["degraded"] = True
            await self._emit("step_completed", step_id, payload)
            self._proxy._mark(step_id, "completed" if code == 0 else "degraded")
            return
        if code != 0:
            await self._emit("step_failed", step_id, {"error": _tail_text(tail), "exit_code": code})
            self.run.failed_steps.append(step_id)
            return
        await self._emit("step_completed", step_id, {"summary": "Validation script passed without producing validation_result.json", "degraded": True})
        self._proxy._mark(step_id, "degraded")

    async def _step_research(self) -> None:
        """执行行业研究 LLM 步骤（期望产物 industry_research_view.json）。

        参数：
            无。
        返回值：
            无。
        """
        params = self.run.params
        prompt_ctx = {
            "target": params.get("target") or "",
            "industry_name": params.get("industry_name") or "",
            "package_dir": str(self.package_dir or ""),
            "package_json": str(self.package_json or ""),
            "package_md": str(self.package_json.with_suffix(".md")) if self.package_json else "",
            "evidence_table": str(self.package_dir / "evidence_table.json") if self.package_dir else "",
        }
        pack = steps.build_industry_research_prompt(prompt_ctx)
        await self._proxy._run_llm_step("industry_research", pack, [list(pack["expected_artifacts"])])

    async def _step_deliver(self) -> dict[str, Any]:
        """汇总包质量 Gate 与校验结果为行业结论卡。

        参数：
            无。
        返回值：
            行业 summary 字典。
        """
        step_id = "industry_deliver"
        await self._emit("step_started", step_id, {})
        package = state_reader.load_json_dict(self.package_json) if self.package_json else {}
        validation = (
            state_reader.load_json_dict(self.package_dir / "validation_result.json") if self.package_dir else {}
        )
        view_path = self.package_dir / "industry_research_view.json" if self.package_dir else None
        research_view = state_reader.load_json_dict(view_path) if view_path else {}
        summary = {
            "target": str(self.run.params.get("target") or self.run.params.get("industry_name") or ""),
            "industry_name": str(self.run.params.get("industry_name") or package.get("industry_name") or ""),
            "package_dir": str(self.package_dir or ""),
            "package_status": str(package.get("status") or ""),
            "quality_gate": package.get("quality_gate") or package.get("collection_quality") or {},
            "validation": {
                "present": bool(validation),
                "status": str(validation.get("status") or validation.get("result") or ""),
                "issues": validation.get("issues") or validation.get("errors") or [],
            },
            "research_view_present": bool(research_view),
            "one_line_conclusion": str(
                research_view.get("one_line_conclusion") or research_view.get("conclusion") or ""
            ),
            "artifact_paths": {
                "industry_input_package_json": str(self.package_json or ""),
                "industry_research_view_json": str(view_path or "") if view_path and view_path.exists() else "",
            },
        }
        await self._emit("step_completed", step_id, {"summary": "Industry conclusion card generated"})
        self.run.step_status[step_id] = "completed"
        return summary


async def _industry_pipeline(run: Run) -> str:
    """行业流水线入口。

    参数：
        run: 运行实例。
    返回值：
        最终 run 状态。
    """
    return await _IndustryPipeline(run).execute()


# ---------------------------------------------------------------------------
# demo 模式：纯脚本化事件序列（不碰真实工作区、不起子进程）
# ---------------------------------------------------------------------------

def _demo_plan() -> list[dict[str, Any]]:
    """构造 demo 模式的计划（含 2 个 skipped 步骤，展示复用/休眠视觉）。

    参数：
        无。
    返回值：
        plan_ready.steps 列表。
    """
    skipped = {
        "audit": "Demo mode uses a preset audit result",
        "processor_compare": steps.SKIP_REASON_REUSE,
    }
    plan: list[dict[str, Any]] = []
    for step in steps.COMPANY_STEP_DEFS:
        entry: dict[str, Any] = {
            "step_id": step.step_id,
            "owner": step.owner,
            "kind": step.kind,
            "title": step.title,
            "status": "skipped" if step.step_id in skipped else "pending",
        }
        if step.layer:
            entry["layer"] = step.layer
        if step.step_id in skipped:
            entry["skip_reason"] = skipped[step.step_id]
        plan.append(entry)
    return plan


def build_demo_timeline(run_id: str) -> list[tuple[float, dict[str, Any]]]:
    """生成 demo 模式的脚本化事件时间轴。

    功能：
        覆盖契约全部关键事件类型（plan_ready 含 skipped、下载日志、
        digest 进度 0/25→25/25、回流、step_waiting_llm、市场上下文并行进度、
        估值完成、final_audit、带结论卡的 run_completed）。
        总时长约 60-70 秒；事件 ts 按构造时刻加累计延迟合成，
        与真实播放节奏一致。演示数据使用茅台样例并明确标注"演示数据"。
    参数：
        run_id: 运行标识。
    返回值：
        [(播放前延迟秒数, 事件字典)] 列表；事件已含单调递增 seq。
    """
    base = _dt.datetime.now().astimezone()
    pw = str(config.PROCESSOR_WORKSPACE / "parsed_reports" / "annual" / "2025" / "600519" / "600519-贵州茅台-2025年年报")
    aw = str(config.ANALYST_WORKSPACE / "reports" / "annual" / "2025" / "600519" / "600519-贵州茅台-2025年年报")
    cw = str(config.COLLECTOR_WORKSPACE / "reports" / "annual" / "2025" / "600519")
    mw = str(config.MARKET_CONTEXT_WORKSPACE / "packages" / "600519" / "2026-07-13")
    vw = str(config.VALUATION_WORKSPACE / "reports" / "600519" / "2026-07-13")
    demo_prompt = (
        "Use the financial-analyst agent to complete the formal financial analysis (demo prompt), "
        f"and write formal_financial_analysis.json and formal_financial_analysis.md to {aw}."
    )

    def artifact(step_id: str, path: str) -> tuple[str, str, dict[str, Any]]:
        return ("artifact_created", step_id, {"path": path, "name": Path(path).name, "kind": _kind_of(path)})

    layer_all_ready = {
        "collector": "ready",
        "processor": "ready",
        "financial_evidence_draft": "ready",
        "formal_financial_analysis": "ready",
        "valuation": "ready",
        "market_context": "ready",
    }
    demo_summary = {
        "company_name": "Kweichow Moutai",
        "stock_code": "600519",
        "report_year": "2025",
        "valuation_view": "undervalued",
        "one_line_conclusion": "Demo data: the current price is below the base fair-value point, indicating modest undervaluation with approximately 8.9% upside in the base case.",
        "current_price": 1488.0,
        "market_cap": 1869200000000.0,
        "price_source": "demo_fixture",
        "fair_value": {"bear": 1350.0, "base": 1620.0, "bull": 1850.0, "unit": "CNY/share"},
        "upside_downside": {"bear": -0.093, "base": 0.089, "bull": 0.243},
        "key_assumptions": [
            "Demo: 2026 revenue growth of 9%-11%",
            "Demo: direct-sales mix continues to rise while gross margin remains above 91%",
            "Demo: dividend payout ratio remains at or above 75%",
            "Demo: wholesale pricing stabilizes and channel inventory remains healthy",
        ],
        "valuation_falsifiers": [
            "Demo: wholesale pricing declines by more than 10% for two consecutive quarters",
            "Demo: direct-sales revenue growth turns negative",
            "Demo: dividend payout ratio falls below 60%",
        ],
        "market_context": {
            "status": "ready_public_proxy",
            "source_count": 73,
            "tier_counts": {"S": 0, "A": 17, "B": 20, "C": 36},
            "max_confidence": "medium_low",
        },
        "layer_statuses": layer_all_ready,
        "artifact_paths": {
            "valuation_report_md": f"{vw}\\valuation_report.md",
            "formal_financial_analysis_md": f"{aw}\\formal_financial_analysis.md",
            "market_context_package_md": f"{mw}\\market_context_package.md",
        },
        "confidence": "medium",
        "gaps": ["Demo data, not a real research conclusion"],
    }

    # (延迟, 事件类型, step_id, payload)；owner 由步骤定义补齐。
    script: list[tuple[float, str, str | None, dict[str, Any]]] = [
        (0.0, "run_started", None, {
            "mode": "demo",
            "params": {"stock_code": "600519", "company_name": "Kweichow Moutai", "report_year": "2025", "report_type": "annual"},
            "llm_mode": "manual",
        }),
        (0.6, "plan_ready", None, {
            "steps": _demo_plan(),
            "layer_statuses": {
                "collector": "partial", "processor": "partial", "financial_evidence_draft": "missing",
                "formal_financial_analysis": "missing", "valuation": "missing", "market_context": "missing",
            },
            "reusable": {key: False for key in layer_all_ready},
            "next_actions": [{"step": "collector_fetch", "owner": "information-collector", "reason": "Demo: financial-report PDF still needs to be downloaded."}],
        }),
        (0.8, "step_started", "collector_fetch", {"cmd": "python info_collector_scripts/run_cninfo_collection.py --start-date 2026-01-01 --end-date 2026-07-13 --report-types annual --keyword 600519 --download"}),
        (1.2, "step_log", "collector_fetch", {"line": "Queried disclosure window 2026-01-01 to 2026-07-13 and found 4 records"}),
        (1.4, "step_log", "collector_fetch", {"line": "Downloaded 600519-Kweichow-Moutai-2025-Annual-Report.pdf (3.2 MB)"}),
        (1.4, "step_log", "collector_fetch", {"line": "Downloaded 600519-Kweichow-Moutai-2025-Annual-Report-Summary.pdf (0.4 MB)"}),
        (1.0, *artifact("collector_fetch", f"{cw}\\600519-贵州茅台-2025年年报.pdf")),
        (0.8, "step_completed", "collector_fetch", {"summary": "Financial-report PDF is ready"}),
        (0.4, "state_refreshed", None, {"layer_statuses": {**layer_all_ready, "processor": "partial", "financial_evidence_draft": "missing", "formal_financial_analysis": "missing", "valuation": "missing", "market_context": "missing"}, "reusable": {"collector": True}}),
        (0.6, "step_started", "market_context_update", {"cmd": "python market_context_collector_scripts/run_market_context_collection.py --target 600519 --stock-code 600519 --company-name Kweichow-Moutai --as-of-date 2026-07-13 --depth standard --freshness oneMonth"}),
        (0.8, "step_started", "processor_parse", {"cmd": "python info_processor_scripts/run_pdf_processing.py --stock-code 600519 --report-type annual --report-year 2025"}),
        (1.4, "step_log", "processor_parse", {"line": "Parsed a 266-page PDF and extracted text, tables, and image summaries"}),
        (1.2, "step_progress", "market_context_update", {"done": 2, "total": 12, "unit": "queries", "detail": "Approximate progress based on cached query count"}),
        (1.6, *artifact("processor_parse", f"{pw}\\content.json")),
        (0.5, "step_completed", "processor_parse", {"summary": "PDF parsing completed"}),
        (0.4, "state_refreshed", None, {"layer_statuses": {**layer_all_ready, "processor": "partial", "financial_evidence_draft": "missing", "formal_financial_analysis": "missing", "valuation": "missing", "market_context": "missing"}, "reusable": {"collector": True}}),
        (0.8, "step_started", "processor_digest", {"cmd": "python info_processor_scripts/build_llm_digest.py auto-digest --pipeline-dir <digest_pipeline>"}),
        (0.6, "step_progress", "processor_digest", {"done": 0, "total": 25, "unit": "chunks"}),
        (1.6, "step_progress", "processor_digest", {"done": 5, "total": 25, "unit": "chunks"}),
        (1.4, "step_progress", "market_context_update", {"done": 5, "total": 12, "unit": "queries", "detail": "Approximate progress based on cached query count"}),
        (1.6, "step_progress", "processor_digest", {"done": 12, "total": 25, "unit": "chunks"}),
        (1.8, "step_progress", "processor_digest", {"done": 20, "total": 25, "unit": "chunks"}),
        (1.6, "step_progress", "processor_digest", {"done": 25, "total": 25, "unit": "chunks"}),
        (0.8, *artifact("processor_digest", f"{pw}\\llm_digest.json")),
        (0.4, "step_completed", "processor_digest", {"summary": "LLM digest completed"}),
        (0.4, "state_refreshed", None, {"layer_statuses": {**layer_all_ready, "processor": "partial", "financial_evidence_draft": "missing", "formal_financial_analysis": "missing", "valuation": "missing", "market_context": "missing"}, "reusable": {"collector": True}}),
        (0.7, "step_started", "processor_rag", {"cmd": "python info_processor_scripts/build_report_rag_index.py build --content-json <content.json>"}),
        (1.4, "step_log", "processor_rag", {"line": "Built rag_chunks.jsonl with 812 chunks"}),
        (0.9, *artifact("processor_rag", f"{pw}\\rag_index\\rag_chunks.jsonl")),
        (0.5, "step_completed", "processor_rag", {"summary": "RAG index is ready"}),
        (0.4, "state_refreshed", None, {"layer_statuses": {**layer_all_ready, "financial_evidence_draft": "missing", "formal_financial_analysis": "missing", "valuation": "missing", "market_context": "missing"}, "reusable": {"collector": True, "processor": True}}),
        (1.0, "step_progress", "market_context_update", {"done": 9, "total": 12, "unit": "queries", "detail": "Approximate progress based on cached query count"}),
        (0.8, "step_started", "financial_evidence_draft", {"cmd": "python financial_analyst_scripts/run_financial_analysis.py --report-dir <report_dir> --analysis-depth standard"}),
        (1.6, "step_log", "financial_evidence_draft", {"line": "Verified 24 evidence items; 13 passed; generated the verification checklist"}),
        (0.9, *artifact("financial_evidence_draft", f"{aw}\\analyst_report.json")),
        (0.5, "step_completed", "financial_evidence_draft", {"summary": "Financial evidence draft completed"}),
        (0.6, "backflow", "financial_evidence_draft", {
            "from_step": "financial_evidence_draft",
            "to_owner": "information-processor",
            "reason": "Demo: evidence verification passed 13/24, below 60%. The information processor should add evidence (notice only; no automatic rerun).",
        }),
        (0.4, "state_refreshed", None, {"layer_statuses": {**layer_all_ready, "formal_financial_analysis": "missing", "valuation": "missing", "market_context": "missing"}, "reusable": {"collector": True, "processor": True, "financial_evidence_draft": True}}),
        (0.8, "step_started", "formal_financial_analysis", {}),
        (0.5, "step_waiting_llm", "formal_financial_analysis", {
            "instructions": "Demo: run the prompt in Claude Code; this step completes automatically after the artifacts are written.",
            "prompt": demo_prompt,
            "expected_artifacts": [f"{aw}\\formal_financial_analysis.json", f"{aw}\\formal_financial_analysis.md"],
            "claude_cmd": steps.format_claude_cmd(demo_prompt),
        }),
        (2.0, "step_progress", "market_context_update", {"done": 12, "total": 12, "unit": "queries", "detail": "Approximate progress based on cached query count"}),
        (1.0, *artifact("market_context_update", f"{mw}\\market_context_package.json")),
        (0.5, "step_completed", "market_context_update", {"summary": "Market-context package status: ready_public_proxy"}),
        (0.5, "state_refreshed", None, {"layer_statuses": {**layer_all_ready, "formal_financial_analysis": "missing", "valuation": "missing"}, "reusable": {"collector": True, "processor": True, "financial_evidence_draft": True, "market_context": True}}),
        (3.0, *artifact("formal_financial_analysis", f"{aw}\\formal_financial_analysis.json")),
        (0.3, *artifact("formal_financial_analysis", f"{aw}\\formal_financial_analysis.md")),
        (0.5, "step_completed", "formal_financial_analysis", {"summary": "Expected artifacts were written; step completed automatically"}),
        (0.5, "state_refreshed", None, {"layer_statuses": {**layer_all_ready, "valuation": "missing"}, "reusable": {"collector": True, "processor": True, "financial_evidence_draft": True, "formal_financial_analysis": True, "market_context": True}}),
        (0.8, "step_started", "valuation_update", {}),
        (0.5, "step_waiting_llm", "valuation_update", {
            "instructions": "Demo: the valuation analyst is waiting for the four-file LLM artifact package.",
            "prompt": f"Use the valuation-analyst agent to complete the valuation update (demo prompt) and write the four-file package to {vw}.",
            "expected_artifacts": [f"{vw}\\{name}" for name in _VALUATION_FILES],
            "claude_cmd": steps.format_claude_cmd("Use the valuation-analyst agent to complete the valuation update (demo prompt)"),
        }),
        (4.0, *artifact("valuation_update", f"{vw}\\valuation_report.json")),
        (0.3, *artifact("valuation_update", f"{vw}\\valuation_report.md")),
        (0.3, *artifact("valuation_update", f"{vw}\\valuation_evidence_table.json")),
        (0.3, *artifact("valuation_update", f"{vw}\\valuation_audit.json")),
        (0.6, "step_completed", "valuation_update", {"summary": "Expected artifacts were written; step completed automatically"}),
        (0.5, "state_refreshed", None, {"layer_statuses": layer_all_ready, "reusable": {key: True for key in layer_all_ready}}),
        (0.8, "step_started", "final_audit", {"cmd": "python research_orchestrator_scripts/audit_company_research_state.py --stock-code 600519 --report-year 2025 --write-state"}),
        (1.4, "step_log", "final_audit", {"line": "Layer status: collector=ready, processor=ready, financial_evidence_draft=ready, formal_financial_analysis=ready, valuation=ready, market_context=ready"}),
        (0.6, "step_completed", "final_audit", {"summary": "Research-state audit completed"}),
        (0.4, "state_refreshed", None, {"layer_statuses": layer_all_ready, "reusable": {key: True for key in layer_all_ready}}),
        (0.8, "step_started", "deliver", {}),
        (1.2, "step_completed", "deliver", {"summary": "Conclusion card generated (demo data)"}),
        (0.6, "run_completed", None, {"status": "completed", "summary": demo_summary}),
    ]

    demo_plan = _demo_plan()
    completed_demo_steps: set[str] = set()

    def demo_milestones(layer_statuses: dict[str, str]) -> dict[str, dict[str, Any]]:
        """把演示层状态投影成与 coordinator 一致的交付里程碑。"""
        projected: dict[str, dict[str, Any]] = {}
        for item in demo_plan:
            layer = str(item.get("layer") or "")
            if not layer:
                continue
            readiness = str(layer_statuses.get(layer) or "missing")
            run_status = "skipped" if item.get("status") == "skipped" else (
                "completed" if item["step_id"] in completed_demo_steps or readiness == "ready"
                else ("failed" if readiness in {"failed", "blocked"} else "pending")
            )
            projected[item["step_id"]] = {
                "step_id": item["step_id"],
                "owner": item["owner"],
                "title": item["title"],
                "layer": layer,
                "readiness_status": readiness,
                "run_status": run_status,
                "source": "workspace_audit",
                "artifact_paths": [],
                "summary": "",
            }
        return projected

    # demo 同时覆盖 coordinator 的真实 Task/Agent/Tool/Handoff 事件，使离线模式即可
    # 验证左栏实时执行链和中央交付轨迹，而不需要真实 API 或 Claude 调用。
    expanded: list[tuple[float, str, str | None, dict[str, Any], str | None]] = []
    active_demo_jobs: set[str] = set()
    for delay, event_type, step_id, original_payload in script:
        payload = dict(original_payload)
        step_def = steps.COMPANY_STEP_MAP.get(step_id) if step_id else None
        owner = step_def.owner if step_def else None
        runtime_step = bool(step_def and owner != _COORDINATOR_OWNER and step_def.kind != "synthetic")
        invocation_id = f"demo:{step_id}" if step_id else ""
        if event_type == "step_completed" and step_id:
            completed_demo_steps.add(step_id)

        if event_type == "plan_ready":
            payload["trace_mode"] = "runtime"
            payload["display_only"] = True
            payload["milestone_states"] = demo_milestones(payload.get("layer_statuses") or {})
        elif event_type == "state_refreshed":
            payload.setdefault("next_actions", [])
            payload["milestone_states"] = demo_milestones(payload.get("layer_statuses") or {})
        elif event_type == "artifact_created" and owner:
            payload.update({"producer_owner": owner, "delivery_to": _COORDINATOR_OWNER, "source": "demo"})

        if event_type == "step_started" and runtime_step:
            active_demo_jobs.add(step_id or "")
            expanded.extend(
                [
                    (
                        delay,
                        "work_item_upsert",
                        None,
                        {
                            "work_item_id": step_id,
                            "title": step_def.title,
                            "description": f"Demo: complete {step_def.title}",
                            "active_form": f"Running {step_def.title}",
                            "status": "in_progress",
                            "blocked_by": [],
                        },
                        owner,
                    ),
                    (
                        0.0,
                        "agent_started",
                        None,
                        {
                            "agent_name": owner,
                            "description": step_def.title,
                            "invocation_id": invocation_id,
                            "tool_use_id": invocation_id,
                            "work_item_id": step_id,
                        },
                        owner,
                    ),
                    (
                        0.0,
                        "handoff",
                        None,
                        {
                            "kind": "delegation",
                            "from_owner": _COORDINATOR_OWNER,
                            "to_owner": owner,
                            "agent_name": owner,
                            "description": step_def.title,
                            "invocation_id": invocation_id,
                            "work_item_id": step_id,
                        },
                        _COORDINATOR_OWNER,
                    ),
                    (
                        0.0,
                        "tool_activity",
                        None,
                        {
                            "phase": "started",
                            "tool_name": "Bash" if step_def.kind == "script" else "Read/Write",
                            "tool_use_id": f"{invocation_id}:tool",
                            "invocation_id": invocation_id,
                            "work_item_id": step_id,
                            "agent_name": owner,
                        },
                        owner,
                    ),
                    (0.0, event_type, step_id, payload, owner),
                ]
            )
            continue

        if event_type == "step_completed" and runtime_step and step_id in active_demo_jobs:
            expanded.extend(
                [
                    (delay, event_type, step_id, payload, owner),
                    (
                        0.0,
                        "tool_activity",
                        None,
                        {
                            "phase": "completed",
                            "tool_name": "Bash" if step_def.kind == "script" else "Read/Write",
                            "tool_use_id": f"{invocation_id}:tool",
                            "invocation_id": invocation_id,
                            "work_item_id": step_id,
                            "agent_name": owner,
                            "status": "completed",
                            "is_error": False,
                        },
                        owner,
                    ),
                    (
                        0.0,
                        "agent_completed",
                        None,
                        {
                            "agent_name": owner,
                            "description": step_def.title,
                            "invocation_id": invocation_id,
                            "tool_use_id": invocation_id,
                            "work_item_id": step_id,
                            "status": "completed",
                            "is_error": False,
                            "summary": f"{step_def.title} completed",
                        },
                        owner,
                    ),
                    (
                        0.0,
                        "handoff",
                        None,
                        {
                            "kind": "delivery",
                            "from_owner": owner,
                            "to_owner": _COORDINATOR_OWNER,
                            "agent_name": owner,
                            "invocation_id": invocation_id,
                            "work_item_id": step_id,
                            "summary": f"{step_def.title} completed",
                        },
                        owner,
                    ),
                    (
                        0.0,
                        "work_item_upsert",
                        None,
                        {
                            "work_item_id": step_id,
                            "title": step_def.title,
                            "description": f"Demo: complete {step_def.title}",
                            "active_form": f"Running {step_def.title}",
                            "status": "completed",
                            "blocked_by": [],
                        },
                        owner,
                    ),
                ]
            )
            active_demo_jobs.discard(step_id or "")
            continue

        if event_type == "run_completed":
            expanded.append(
                (
                    delay,
                    "handoff",
                    None,
                    {
                        "kind": "final_delivery",
                        "from_owner": _COORDINATOR_OWNER,
                        "to_owner": _COORDINATOR_OWNER,
                        "from_station": "dispatch",
                        "to_station": "deliver",
                        "label": "Complete Conclusion-First Report",
                        "status": payload.get("status"),
                    },
                    _COORDINATOR_OWNER,
                )
            )
            expanded.append((0.0, event_type, step_id, payload, owner))
            continue

        expanded.append((delay, event_type, step_id, payload, owner))

    timeline: list[tuple[float, dict[str, Any]]] = []
    elapsed = 0.0
    for index, (delay, event_type, step_id, payload, owner) in enumerate(expanded, start=1):
        elapsed += delay
        event: dict[str, Any] = {
            "seq": index,
            "ts": (base + _dt.timedelta(seconds=elapsed)).isoformat(timespec="seconds"),
            "run_id": run_id,
            "type": event_type,
        }
        if step_id:
            event["step_id"] = step_id
        if owner:
            event["owner"] = owner
        event["payload"] = payload
        timeline.append((delay, event))
    return timeline


async def _demo_pipeline(run: Run) -> str:
    """demo 流水线：按时间轴节奏播放脚本化事件。

    参数：
        run: 运行实例。
    返回值：
        最终 run 状态（completed）。
    """
    for delay, event in build_demo_timeline(run.run_id):
        if delay > 0:
            await asyncio.sleep(delay)
        await run.bus.publish_prepared(dict(event))
    return "completed"


# ---------------------------------------------------------------------------
# replay 模式：从既有工作区产物合成事件序列
# ---------------------------------------------------------------------------

def _existing(paths: list[Path]) -> list[Path]:
    """过滤出真实存在的文件。

    参数：
        paths: 候选路径列表。
    返回值：
        存在的路径列表。
    """
    return [path for path in paths if path.exists() and path.is_file()]


def _latest_dated_dir(bases: list[Path]) -> Path | None:
    """在若干父目录下选择"日期目录名最大"的子目录（估值/市场上下文取最新日期）。

    参数：
        bases: 候选父目录。
    返回值：
        最新日期目录；没有时返回 None。
    """
    candidates: list[tuple[str, Path]] = []
    for base in bases:
        if base.exists():
            for child in base.iterdir():
                if child.is_dir():
                    candidates.append((child.name, child))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def build_replay_events(run_id: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """扫描真实工作区产物，按 mtime 升序合成完整事件序列。

    功能：
        对指定 stock_code/report_year 逐层定位产物：存在的层生成
        step_started → artifact_created（逐个文件，ts=mtime）→ step_completed；
        缺失的层生成 step_skipped。估值/市场上下文取该代码下最新日期目录。
        事件一次性全部产出，run 状态直接 completed，由前端按倍速播放。
    参数：
        run_id: 运行标识。
        params: {stock_code, report_year, report_type?}。
    返回值：
        (事件列表, 最终状态字符串)。
    """
    stock_code = str(params.get("stock_code") or "").strip()
    report_year = str(params.get("report_year") or "").strip()
    report_type = str(params.get("report_type") or "annual").strip() or "annual"

    # ---- 逐层定位产物 ----
    step_files: dict[str, list[Path]] = {step.step_id: [] for step in steps.COMPANY_STEP_DEFS}

    pdf_dir = config.COLLECTOR_WORKSPACE / "reports" / report_type / report_year / stock_code
    if pdf_dir.exists():
        step_files["collector_fetch"] = sorted(pdf_dir.glob("*.pdf"))

    processor_dir: Path | None = None
    parsed_base = config.PROCESSOR_WORKSPACE / "parsed_reports" / report_type / report_year / stock_code
    if parsed_base.exists():
        candidates = [child for child in parsed_base.iterdir() if (child / "content.json").exists()]
        if candidates:
            processor_dir = max(candidates, key=lambda child: (child / "content.json").stat().st_mtime)
    if processor_dir:
        step_files["processor_parse"] = _existing([processor_dir / "content.json", processor_dir / "content.md"])
        step_files["processor_digest"] = _existing([processor_dir / "llm_digest.json", processor_dir / "digest_audit.json"])
        step_files["processor_rag"] = _existing(
            [processor_dir / "rag_index" / "rag_chunks.jsonl", processor_dir / "rag_index" / "rag_index_meta.json"]
        )
        step_files["processor_compare"] = _existing([processor_dir / "summary_comparison.json"])

    analyst_dir: Path | None = None
    analyst_base = config.ANALYST_WORKSPACE / "reports" / report_type / report_year / stock_code
    if analyst_base.exists():
        candidates = [child for child in analyst_base.iterdir() if (child / "analyst_report.json").exists()]
        if candidates:
            analyst_dir = max(candidates, key=lambda child: (child / "analyst_report.json").stat().st_mtime)
    if analyst_dir:
        step_files["financial_evidence_draft"] = _existing(
            [analyst_dir / name for name in ("analyst_report.json", "analyst_report.md", "evidence_check.json", "analyst_audit.json")]
        )
        step_files["formal_financial_analysis"] = _existing(
            [analyst_dir / "formal_financial_analysis.json", analyst_dir / "formal_financial_analysis.md"]
        )

    market_dir = _latest_dated_dir([config.MARKET_CONTEXT_WORKSPACE / "packages" / stock_code]) if stock_code else None
    if market_dir:
        step_files["market_context_update"] = _existing(
            [market_dir / name for name in ("market_context_package.json", "market_context_package.md", "market_context_sources.json", "collection_audit.json")]
        )

    valuation_dir = (
        _latest_dated_dir([config.VALUATION_WORKSPACE / "reports" / stock_code, config.VALUATION_WORKSPACE / stock_code])
        if stock_code
        else None
    )
    if valuation_dir:
        step_files["valuation_update"] = _existing([valuation_dir / name for name in _VALUATION_FILES])

    state_file = config.ORCHESTRATOR_WORKSPACE / "company_state" / stock_code / report_year / "research_state.json"
    if state_file.exists():
        step_files["final_audit"] = [state_file]
    state = state_reader.load_research_state(state_file) if state_file.exists() else {}

    # ---- 组装事件 ----
    all_files = [path for files in step_files.values() for path in files]
    if all_files:
        earliest = min(all_files, key=lambda path: path.stat().st_mtime)
        latest = max(all_files, key=lambda path: path.stat().st_mtime)
        start_ts = state_reader.mtime_iso(earliest)
        end_ts = state_reader.mtime_iso(latest)
    else:
        start_ts = state_reader.now_iso()
        end_ts = start_ts

    events: list[dict[str, Any]] = []

    def emit(event_type: str, ts: str, step_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        event: dict[str, Any] = {"seq": len(events) + 1, "ts": ts, "run_id": run_id, "type": event_type}
        if step_id:
            event["step_id"] = step_id
            step_def = steps.COMPANY_STEP_MAP.get(step_id)
            if step_def:
                event["owner"] = step_def.owner
        event["payload"] = payload or {}
        events.append(event)

    plan = []
    for step in steps.COMPANY_STEP_DEFS:
        has_artifacts = bool(step_files.get(step.step_id))
        synthetic = step.step_id in {"audit", "deliver"}
        entry: dict[str, Any] = {
            "step_id": step.step_id,
            "owner": step.owner,
            "kind": step.kind,
            "title": step.title,
            "status": "pending" if (has_artifacts or synthetic) else "skipped",
        }
        if step.layer:
            entry["layer"] = step.layer
        if entry["status"] == "skipped":
            entry["skip_reason"] = "Replay: no artifact found for this layer"
        plan.append(entry)

    emit("run_started", start_ts, None, {"mode": "replay", "params": params, "llm_mode": "manual"})
    emit("plan_ready", start_ts, None, {"steps": plan, "research_state_path": str(state_file) if state_file.exists() else ""})
    emit("step_started", start_ts, "audit", {})
    emit("step_completed", start_ts, "audit", {"summary": "Replay mode: synthesized timeline from existing artifacts"})

    # 有产物的步骤按"最早产物 mtime"升序排列，与真实产出顺序一致。
    ordered = sorted(
        (step_id for step_id, files in step_files.items() if files),
        key=lambda step_id: min(path.stat().st_mtime for path in step_files[step_id]),
    )
    for step in steps.COMPANY_STEP_DEFS:
        if step.step_id in {"audit", "deliver"}:
            continue
        if not step_files.get(step.step_id):
            emit("step_skipped", start_ts, step.step_id, {"reason": "Replay: no artifact found for this layer"})
    for step_id in ordered:
        files = sorted(step_files[step_id], key=lambda path: path.stat().st_mtime)
        emit("step_started", state_reader.mtime_iso(files[0]), step_id, {})
        for path in files:
            emit(
                "artifact_created",
                state_reader.mtime_iso(path),
                step_id,
                {"path": str(path), "name": path.name, "kind": _kind_of(path)},
            )
        emit(
            "step_completed",
            state_reader.mtime_iso(files[-1]),
            step_id,
            {"summary": f"Replay: {len(files)} artifacts", "artifacts": [str(path) for path in files]},
        )
        if step_id == "valuation_update" and valuation_dir and (valuation_dir / "upstream_request.json").exists():
            upstream = valuation_dir / "upstream_request.json"
            emit(
                "backflow",
                state_reader.mtime_iso(upstream),
                "valuation_update",
                {"from_step": "valuation_update", "to_owner": steps.FINANCIAL_ANALYST, "reason": f"Replay: found supplemental-data request {upstream}"},
            )
        if step_id == "final_audit" and state:
            emit(
                "state_refreshed",
                state_reader.mtime_iso(state_file),
                None,
                {
                    "layer_statuses": state.get("summary", {}).get("layer_statuses", {}),
                    "reusable": state.get("reusable", {}),
                    "next_actions": state.get("next_actions", []),
                },
            )

    valuation_report = (
        state_reader.load_json_dict(valuation_dir / "valuation_report.json") if valuation_dir else {}
    )
    formal_report = (
        state_reader.load_json_dict(analyst_dir / "formal_financial_analysis.json") if analyst_dir else {}
    )
    market_package = (
        state_reader.load_json_dict(market_dir / "market_context_package.json") if market_dir else {}
    )
    summary = state_reader.build_company_summary(state, valuation_report, formal_report, market_package)
    if not summary.get("stock_code"):
        summary["stock_code"] = stock_code
    if not summary.get("report_year"):
        summary["report_year"] = report_year
    emit("step_started", end_ts, "deliver", {})
    emit("step_completed", end_ts, "deliver", {"summary": "Replay conclusion card generated"})
    emit("run_completed", end_ts, None, {"status": "completed", "summary": summary})
    return events, "completed"


async def _replay_pipeline(run: Run) -> str:
    """replay 流水线：一次性写入全部合成事件（前端负责倍速播放）。

    参数：
        run: 运行实例。
    返回值：
        最终 run 状态。
    """
    events, status = await asyncio.to_thread(build_replay_events, run.run_id, run.params)
    for event in events:
        await run.bus.publish_prepared(event)
    return status
