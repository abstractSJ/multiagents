"""research_console 集中配置。

功能：
- 以本文件位置向上推导项目根目录，避免依赖启动时的工作目录；
- 集中定义五大工作区、行业工作区、编排器工作区与控制台工作区路径；
- 集中定义所有被编排脚本的路径，供健康检查与计划构建做存在性校验；
- 定义 artifact 读取白名单根、端口、超时与特性开关。

所有路径都是 pathlib.Path 绝对路径；比较时统一使用 resolve() 后的结果。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 项目根 = research_console/ 的父目录。用文件自身位置推导，
# 使 `python research_console/app.py` 与 `python -m research_console.app` 都能定位到同一根目录。
CONSOLE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONSOLE_DIR.parent

# ---------------------------------------------------------------------------
# 工作区路径（与 CLAUDE.md 的工作区地图一一对应）
# ---------------------------------------------------------------------------
COLLECTOR_WORKSPACE = PROJECT_ROOT / "info_collector_scripts" / "collector_workspace"
PROCESSOR_WORKSPACE = PROJECT_ROOT / "info_processor_scripts" / "processor_workspace"
ANALYST_WORKSPACE = PROJECT_ROOT / "financial_analyst_scripts" / "analyst_workspace"
VALUATION_WORKSPACE = PROJECT_ROOT / "valuation_analyst_scripts" / "valuation_workspace"
MARKET_CONTEXT_WORKSPACE = PROJECT_ROOT / "market_context_collector_scripts" / "collector_workspace"
INDUSTRY_COLLECTOR_WORKSPACE = PROJECT_ROOT / "industry_info_collector_scripts" / "collector_workspace"
ORCHESTRATOR_WORKSPACE = PROJECT_ROOT / "research_orchestrator_scripts" / "orchestrator_workspace"

# 控制台自身的运行持久化工作区（meta.json + events.jsonl 落在这里）。
CONSOLE_WORKSPACE = CONSOLE_DIR / "console_workspace"
RUNS_DIR = CONSOLE_WORKSPACE / "runs"

# 前端静态目录由前端开发者负责；后端只做存在性判断，不得写入。
STATIC_DIR = CONSOLE_DIR / "static"

# artifact 安全读取白名单根：五大工作区 + 行业工作区 + 编排器工作区 + 控制台工作区。
# 白名单之外的一切路径（包括项目根本身的其他文件）一律 403。
ARTIFACT_WHITELIST_ROOTS: list[Path] = [
    COLLECTOR_WORKSPACE,
    PROCESSOR_WORKSPACE,
    ANALYST_WORKSPACE,
    VALUATION_WORKSPACE,
    MARKET_CONTEXT_WORKSPACE,
    INDUSTRY_COLLECTOR_WORKSPACE,
    ORCHESTRATOR_WORKSPACE,
    CONSOLE_WORKSPACE,
]

# ---------------------------------------------------------------------------
# 被编排脚本路径（全部集中在此，缺失时健康检查与计划构建会给出明确错误）
# ---------------------------------------------------------------------------
AUDIT_SCRIPT = PROJECT_ROOT / "research_orchestrator_scripts" / "audit_company_research_state.py"
CNINFO_SCRIPT = PROJECT_ROOT / "info_collector_scripts" / "run_cninfo_collection.py"
PDF_PROCESS_SCRIPT = PROJECT_ROOT / "info_processor_scripts" / "run_pdf_processing.py"
DIGEST_SCRIPT = PROJECT_ROOT / "info_processor_scripts" / "build_llm_digest.py"
RAG_SCRIPT = PROJECT_ROOT / "info_processor_scripts" / "build_report_rag_index.py"
COMPARE_SCRIPT = PROJECT_ROOT / "info_processor_scripts" / "compare_digest_with_summary.py"
FINANCIAL_SCRIPT = PROJECT_ROOT / "financial_analyst_scripts" / "run_financial_analysis.py"
MARKET_CONTEXT_SCRIPT = PROJECT_ROOT / "market_context_collector_scripts" / "run_market_context_collection.py"
INDUSTRY_COLLECT_SCRIPT = PROJECT_ROOT / "industry_info_collector_scripts" / "run_industry_collection.py"
INDUSTRY_VALIDATE_SCRIPT = PROJECT_ROOT / "industry_info_collector_scripts" / "validate_industry_package.py"

REQUIRED_SCRIPTS: dict[str, Path] = {
    "audit": AUDIT_SCRIPT,
    "collector_fetch": CNINFO_SCRIPT,
    "processor_parse": PDF_PROCESS_SCRIPT,
    "processor_digest": DIGEST_SCRIPT,
    "processor_rag": RAG_SCRIPT,
    "processor_compare": COMPARE_SCRIPT,
    "financial_evidence_draft": FINANCIAL_SCRIPT,
    "market_context_update": MARKET_CONTEXT_SCRIPT,
    "industry_collect": INDUSTRY_COLLECT_SCRIPT,
    "industry_validate": INDUSTRY_VALIDATE_SCRIPT,
}

# Bocha Web Search 本地配置文件（key 值不得写入日志或事件，只判断是否存在）。
MARKET_CONTEXT_LOCAL_CONFIG = MARKET_CONTEXT_WORKSPACE / "local_config.json"
BOCHA_KEY_ENV = "BOCHA_WEB_SEARCH_API_KEY"

# 白名单工作区中仍可能存在本机凭据配置。路径守卫先检查拒绝清单，再检查根目录，
# 防止 /api/artifact 因“目录合法”而返回 Bocha API Key。
ARTIFACT_DENY_PATHS: list[Path] = [MARKET_CONTEXT_LOCAL_CONFIG]

# ---------------------------------------------------------------------------
# 服务参数
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8600

# ---------------------------------------------------------------------------
# 超时与节奏（单位：秒）
# ---------------------------------------------------------------------------
# 普通确定性脚本的单步兜底超时。采集下载可能较慢，故给足冗余。
SCRIPT_TIMEOUT_SECONDS = 3600.0
# audit 是纯标准库文件扫描，秒级完成；超时说明环境异常。
AUDIT_TIMEOUT_SECONDS = 180.0
# LLM 步骤期望产物轮询间隔。
LLM_POLL_INTERVAL_SECONDS = 2.0
# LLM 步骤最长等待时长；等待用户在 Claude Code 里手工完成可能很久，默认给 6 小时。
LLM_WAIT_TIMEOUT_SECONDS = 6 * 3600.0
# 完整 /rec 协调会话沿用同一硬超时，防止 CLI 参数或上游服务异常时永久悬挂。
COORDINATOR_TIMEOUT_SECONDS = LLM_WAIT_TIMEOUT_SECONDS
# Claude CLI 的 print 模式默认只等待后台 Agent 600 秒，超过后会主动停止仍在运行的
# 财务分析或估值任务。设为 0 表示取消这层内部上限，统一由上面的六小时协调会话
# 超时负责最终兜底，避免长时间研究任务被 CLI 提前截断。
COORDINATOR_PRINT_BG_WAIT_CEILING_MS = 0
# 协调会话运行期间重跑 research_state audit 的间隔。该观察器只读状态并发布事件，
# 不依据状态自行调度 Agent；真实调度权始终属于同一个 /rec 主会话。
COORDINATOR_AUDIT_POLL_INTERVAL_SECONDS = 4.0
# stream-json 文本增量的最短发布间隔。完整 assistant 消息不受该限制；
# partial 仅作为 SSE 瞬态预览、内存只保留最新一条且不写 events.jsonl。
COORDINATOR_PARTIAL_MESSAGE_INTERVAL_SECONDS = 0.5
# 原始 Claude Code NDJSON 事件文件名，固定落在单次 run 目录，便于审计与故障排查。
COORDINATOR_EVENTS_FILENAME = "claude_events.jsonl"
# assistant/tool_result 单个顶层事件可能包含长报告或大段工具结果，远超 asyncio
# StreamReader 默认 64 KiB 行限制。reader 使用分块读自行组装 NDJSON 行，并把单行
# 上界设为 128 MiB：足以容纳真实大工具结果，同时避免损坏流导致无界内存增长。
COORDINATOR_STREAM_LIMIT_BYTES = 128 * 1024 * 1024
# coordinator_cli 必须能在无人值守的本机控制台里执行完整 /rec。auto 会继续遵守
# Claude Code 自身安全边界，但不会像 legacy acceptEdits 那样拒绝只读/脚本工具调用。
COORDINATOR_PERMISSION_MODE = "auto"
# 进度探针轮询间隔（digest 结果文件数、市场上下文查询缓存数）。
PROGRESS_POLL_INTERVAL_SECONDS = 1.0
# SSE 心跳注释行间隔。
HEARTBEAT_SECONDS = 15.0
# 取消运行时先 terminate、等待该秒数后再 kill。
SUBPROCESS_KILL_GRACE_SECONDS = 5.0

# ---------------------------------------------------------------------------
# 事件与 artifact 读取限额
# ---------------------------------------------------------------------------
# 每步 step_log 最多推送的行数；超出后仅保留关键行并折叠计数，避免 SSE 洪泛。
STEP_LOG_MAX_LINES = 200
# json/md 文本超过该字节数时截断并标记 truncated。
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
# jsonl 只返回前 N 行。
JSONL_PREVIEW_LINES = 200

# ---------------------------------------------------------------------------
# 特性开关
# ---------------------------------------------------------------------------
# API 未显式传 llm_mode 时仍保持 manual，避免行业链路或旧客户端行为突变；
# 公司前端会显式提交 DEFAULT_COMPANY_LLM_MODE。
DEFAULT_LLM_MODE = "manual"
DEFAULT_COMPANY_LLM_MODE = "coordinator_cli"
ENABLE_CLAUDE_CLI = True
ENABLE_DEMO = True
ENABLE_REPLAY = True

# 市场上下文进度总量按深度估算（cache/queries 新增文件数只是近似进度，总量并非精确值）。
MARKET_CONTEXT_QUERY_ESTIMATE = {"quick": 6, "standard": 12, "deep": 20}


def missing_scripts() -> list[str]:
    """检查被编排脚本是否齐全。

    功能：
        逐一检查 REQUIRED_SCRIPTS 中的脚本文件是否存在，用于健康检查与
        计划构建阶段给出明确错误，而不是在启动或执行途中崩溃。
    参数：
        无。
    返回值：
        缺失脚本的描述列表，形如 ["collector_fetch: <路径>"]；全部存在时为空列表。
    """
    return [f"{name}: {path}" for name, path in REQUIRED_SCRIPTS.items() if not path.exists()]


def bocha_key_present() -> bool:
    """判断 Bocha Web Search API key 是否可用。

    功能：
        解析顺序与采集脚本一致：先看环境变量 BOCHA_WEB_SEARCH_API_KEY，
        再读 market_context_collector_scripts/collector_workspace/local_config.json，
        只判断是否存在非空 key 字段，绝不返回或记录 key 的值。
    参数：
        无。
    返回值：
        存在可用 key 返回 True，否则 False。
    """
    if os.environ.get(BOCHA_KEY_ENV, "").strip():
        return True
    try:
        payload = json.loads(MARKET_CONTEXT_LOCAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    # 字段名可能是 BOCHA_WEB_SEARCH_API_KEY 或其他包含 key 的变体，这里做宽容匹配。
    for name, value in payload.items():
        if "key" in str(name).lower() and str(value or "").strip():
            return True
    return False
