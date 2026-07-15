"""research_console FastAPI 应用。

功能：
- REST 路由：health / catalog / audit / runs 增删查 / 步骤手动完成与跳过 / artifact 读取；
- SSE 事件流：断线重连补发历史（?after=N）+ 持续推送 + 15s 心跳；
- 静态资源挂载：static/ 存在时挂载，缺失时 GET / 返回占位 HTML；
- 启动时恢复历史 run，进程退出时取消仍在运行的任务。

契约是唯一事实源：事件 schema、REST 形态、字段名均与 CONTRACT.md 一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # 以脚本方式直接运行（python research_console/app.py）时 sys.path[0] 是本目录，
    # 需要把项目根补进 sys.path 才能使用 research_console 包的绝对导入。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from research_console import config, engine, history, state_reader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("research_console.app")

# 全局引擎单例：持有所有 run 与其后台任务。
ENGINE = engine.Engine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子。

    功能：
        启动时创建工作区目录并恢复历史 run；关闭时取消仍在运行的任务，
        避免遗留子进程或悬挂协程。
    参数：
        app: FastAPI 实例。
    返回值：
        异步上下文管理器。
    """
    config.CONSOLE_WORKSPACE.mkdir(parents=True, exist_ok=True)
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ENGINE.load_persisted_runs()
    logger.info("research_console 启动，项目根: %s，已恢复 %d 个历史 run", config.PROJECT_ROOT, len(ENGINE.runs))
    try:
        yield
    finally:
        active_run_ids = [
            run.run_id for run in ENGINE.runs.values() if run.status == "running" and run.task
        ]
        if active_run_ids:
            # 统一走 cancel_run，连“任务首次调度前即被取消”的极短窗口也能补齐
            # terminal/meta/租约清理，而不是只向 Task 发送取消后立刻关闭事件循环。
            await asyncio.gather(
                *(ENGINE.cancel_run(run_id) for run_id in active_run_ids),
                return_exceptions=True,
            )
        logger.info("research_console 关闭")


app = FastAPI(title="research_console", version="1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class AuditRequest(BaseModel):
    """POST /api/audit 请求体。"""

    target: str | None = None
    stock_code: str | None = None
    company_name: str | None = None
    report_year: str | int | None = None
    report_type: str | None = "annual"
    depth: str | None = "standard"
    focus: str | None = ""
    as_of_date: str | None = ""
    force_refresh: bool = False


class RunRequest(BaseModel):
    """POST /api/runs 请求体。"""

    mode: str = Field(..., description="company/industry/demo/replay")
    llm_mode: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class StepCompleteRequest(BaseModel):
    """步骤手动完成请求体。"""

    force: bool = False


class DecisionReviewRequest(BaseModel):
    """POST /api/runs/{run_id}/reviews 请求体。

    手工价格只在对应本地价格缺失时回退使用；价格、日期、来源必须成组提供。
    falsification_status 用于记录当时证伪条件目前是未知、仍成立还是已经触发。
    """

    review_date: str = Field(..., description="回看日期，YYYY-MM-DD")
    current_price: float | None = None
    current_price_date: str | None = None
    current_price_source: str | None = Field(default=None, max_length=500)
    benchmark_code: str | None = Field(default=None, max_length=32)
    benchmark_baseline_price: float | None = None
    benchmark_baseline_date: str | None = None
    benchmark_baseline_source: str | None = Field(default=None, max_length=500)
    benchmark_current_price: float | None = None
    benchmark_current_date: str | None = None
    benchmark_current_source: str | None = Field(default=None, max_length=500)
    falsification_status: str = "unknown"
    falsification_notes: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=2000)


def _decision_review_kwargs(req: DecisionReviewRequest) -> dict[str, Any]:
    """把 Pydantic 请求稳定映射为 history 层关键字参数。"""
    return {
        "current_price": req.current_price,
        "current_price_date": req.current_price_date,
        "current_price_source": req.current_price_source,
        "benchmark_code": req.benchmark_code or "",
        "benchmark_baseline_price": req.benchmark_baseline_price,
        "benchmark_baseline_date": req.benchmark_baseline_date,
        "benchmark_baseline_source": req.benchmark_baseline_source,
        "benchmark_current_price": req.benchmark_current_price,
        "benchmark_current_date": req.benchmark_current_date,
        "benchmark_current_source": req.benchmark_current_source,
        "falsification_status": req.falsification_status,
        "falsification_notes": req.falsification_notes or "",
        "note": req.note or "",
    }


def _run_request_error(mode: str, params: dict[str, Any]) -> str | None:
    """校验不同运行模式启动真实流水线所需的最小参数。

    参数：
        mode: company/industry/demo/replay。
        params: 调用方提交的运行参数。
    返回值：
        参数合法时返回 None；否则返回可直接展示给 API 调用方的错误信息。

    为什么在 API 边界校验：
        浏览器表单虽然会拦截空目标，但程序化调用可以绕过前端。若把空请求交给
        后台任务，company 模式会真的启动一个无目标的完整协调会话，既浪费资源，
        又会短暂占用 unresolved 工作区租约，因此必须在创建 run 之前拒绝。
    """
    params = params if isinstance(params, dict) else {}
    if mode == "company":
        has_target = any(str(params.get(key) or "").strip() for key in ("target", "stock_code", "company_name"))
        if not has_target:
            return "company 模式需要 params.target、params.stock_code 或 params.company_name"
        as_of_text = str(params.get("as_of_date") or date.today().isoformat()).strip()
        try:
            parsed_as_of = date.fromisoformat(as_of_text)
        except ValueError:
            return "company 模式 params.as_of_date 必须使用 YYYY-MM-DD 格式"
        if parsed_as_of.isoformat() != as_of_text:
            return "company 模式 params.as_of_date 必须使用 YYYY-MM-DD 格式"
        if parsed_as_of > date.today():
            return "company 模式 params.as_of_date 不能晚于今天"
        # 在 API 边界补齐默认值，确保 run meta、协调器提示词和各脚本看到同一个知识截止日。
        params["as_of_date"] = as_of_text
    elif mode == "industry":
        has_industry_target = any(str(params.get(key) or "").strip() for key in ("target", "industry_name"))
        anchor_values = [
            str(params.get("stock_code") or "").strip(),
            str(params.get("company_name") or "").strip(),
            str(params.get("fiscal_year") or params.get("report_year") or "").strip(),
        ]
        has_any_anchor = any(anchor_values)
        has_full_anchor = all(anchor_values)
        if has_any_anchor and not has_full_anchor:
            return "industry 公司验证模式需要同时提供 stock_code、company_name 和 fiscal_year/report_year"
        if not has_industry_target and not has_full_anchor:
            return "industry 模式需要 params.target/industry_name，或完整的公司验证参数"
    elif mode == "replay" and not str(params.get("stock_code") or "").strip():
        return "replay 模式需要 params.stock_code"
    return None


# ---------------------------------------------------------------------------
# 健康检查与目录
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict[str, Any]:
    """健康检查。

    功能：
        返回项目根、Bocha key 是否存在（只返回布尔）、claude CLI 版本、
        活跃 run 数量与缺失脚本清单。
    参数：
        无。
    返回值：
        健康状态字典。
    """
    active = sum(1 for run in ENGINE.runs.values() if run.status == "running")
    # claude --version 是同步进程调用，必须移出事件循环；否则异常 CLI 最多会让
    # REST、SSE 心跳和取消请求一起冻结 15 秒。
    claude_version = await asyncio.to_thread(_claude_cli_version)
    return {
        "ok": True,
        "project_root": str(config.PROJECT_ROOT),
        "bocha_key_present": config.bocha_key_present(),
        "claude_cli_version": claude_version,
        "active_runs": active,
        "missing_scripts": config.missing_scripts(),
    }


def _claude_cli_version() -> str | None:
    """探测本机 claude CLI 版本。

    功能：
        找不到 claude 或调用失败时返回 None，不阻塞健康检查。
    参数：
        无。
    返回值：
        版本串或 None。
    """
    path = shutil.which("claude")
    if not path:
        return None
    try:
        import subprocess

        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


@app.get("/api/catalog")
async def catalog() -> dict[str, Any]:
    """公司/年度产物目录。

    功能：
        扫描各工作区产出六层存在性快照与 research_state 清单，供前端选择研究对象。
        扫描可能涉及较多文件，放到线程池避免阻塞事件循环。
    参数：
        无。
    返回值：
        {companies, states} 字典。
    """
    return await asyncio.to_thread(state_reader.build_catalog)


@app.post("/api/audit")
async def audit(req: AuditRequest) -> JSONResponse:
    """直接运行状态盘点（不建 run）。

    功能：
        只读调用 audit 脚本，返回完整状态与规范化的默认状态路径，供前端在建 run
        之前预览复用情况。正式状态文件只由实际 run 的初始/final audit 写入，避免
        预览请求与活动协调会话竞争覆盖 research_state.json。
    参数：
        req: 审计请求体。
    返回值：
        research_state JSON + state_path。
    """
    params = req.model_dump()
    if params.get("report_year") is not None:
        params["report_year"] = str(params["report_year"])
    as_of_text = str(params.get("as_of_date") or date.today().isoformat()).strip()
    try:
        parsed_as_of = date.fromisoformat(as_of_text)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "as_of_date 必须使用 YYYY-MM-DD 格式"})
    if parsed_as_of.isoformat() != as_of_text or parsed_as_of > date.today():
        return JSONResponse(status_code=400, content={"error": "as_of_date 必须是今天或更早的 YYYY-MM-DD 日期"})
    params["as_of_date"] = as_of_text
    state, code, tail = await state_reader.run_audit(params, write_state=False)
    if code != 0 or state is None:
        return JSONResponse(status_code=500, content={"error": "audit 执行失败", "detail": tail[-800:]})
    state["state_path"] = str(state_reader.state_file_path(state))
    return JSONResponse(content=state)


# ---------------------------------------------------------------------------
# run 管理
# ---------------------------------------------------------------------------

@app.post("/api/runs")
async def create_run(req: RunRequest) -> JSONResponse:
    """创建一次运行。

    参数：
        req: 运行请求体。
    返回值：
        {run_id} 或 400 错误。
    """
    if req.mode not in engine.RUN_MODES:
        return JSONResponse(status_code=400, content={"error": f"未知 mode: {req.mode}"})
    llm_mode = req.llm_mode or (
        config.DEFAULT_COMPANY_LLM_MODE if req.mode == "company" else config.DEFAULT_LLM_MODE
    )
    if llm_mode not in engine.LLM_MODES:
        return JSONResponse(status_code=400, content={"error": f"未知 llm_mode: {llm_mode}"})
    if llm_mode == "coordinator_cli" and req.mode != "company":
        return JSONResponse(status_code=400, content={"error": "coordinator_cli 仅支持 company 模式"})
    params = dict(req.params or {})
    request_error = _run_request_error(req.mode, params)
    if request_error:
        return JSONResponse(status_code=400, content={"error": request_error})
    try:
        run = await ENGINE.create_run_checked(req.mode, params, llm_mode)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except engine.WorkspaceLeaseConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "同一公司已有运行中的研究，已拒绝并发写共享正式工作区",
                "existing_run_id": exc.run_id,
                "lease_key": exc.lease_key,
            },
        )
    return JSONResponse(content={"run_id": run.run_id})


@app.get("/api/runs")
async def list_runs() -> dict[str, Any]:
    """列出全部运行（按创建时间倒序）。

    参数：
        无。
    返回值：
        {runs:[...]} 字典。
    """
    runs = sorted(ENGINE.runs.values(), key=lambda run: run.created_at, reverse=True)
    return {"runs": [run.brief() for run in runs]}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> JSONResponse:
    """查看单个运行的全部已发生事件。

    参数：
        run_id: 运行标识。
    返回值：
        run 详情或 404。
    """
    run = ENGINE.runs.get(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": "run 不存在"})
    return JSONResponse(
        content={
            "run_id": run.run_id,
            "mode": run.mode,
            "status": run.status,
            "params": run.params,
            "llm_mode": run.llm_mode,
            "execution_mode": run.execution_mode,
            "claude_session_id": run.claude_session_id,
            "history": history.run_history_brief(
                run.mode,
                run.params,
                run.bus.events,
                run.run_dir,
            ),
            "events": run.bus.snapshot(0),
        }
    )


def _history_run_or_response(run_id: str) -> tuple[engine.Run | None, JSONResponse | None]:
    """统一校验历史决策 API 的 run 存在性、模式与持久化目录。"""
    run = ENGINE.runs.get(run_id)
    if not run:
        return None, JSONResponse(status_code=404, content={"status": "run_not_found", "error": "run 不存在"})
    if run.mode != "company":
        return None, JSONResponse(
            status_code=409,
            content={"status": "unsupported_run_mode", "error": "仅 company run 支持历史决策回看"},
        )
    if not run.run_dir:
        return None, JSONResponse(
            status_code=409,
            content={"status": "history_storage_unavailable", "error": "run 没有持久化目录"},
        )
    return run, None


@app.get("/api/runs/{run_id}/decision")
async def get_decision(run_id: str) -> JSONResponse:
    """读取冻结决策；旧 run 仅从最后 summary 派生，不在 GET 时落盘。"""
    run, error = _history_run_or_response(run_id)
    if error is not None:
        return error
    assert run is not None and run.run_dir is not None
    try:
        snapshot, status, warnings = await asyncio.to_thread(
            history.derive_or_load_snapshot,
            run.run_dir,
            run.run_id,
            run.mode,
            run.params,
            run.bus.events,
            materialize=False,
        )
    except history.SnapshotUnavailableError as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "summary_unavailable", "error": str(exc)},
        )
    except history.HistoryValidationError as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "snapshot_corrupt", "error": str(exc)},
        )
    return JSONResponse(
        content={
            "status": status,
            "materialized": status == "frozen",
            "snapshot": snapshot,
            "warnings": warnings,
        }
    )


@app.get("/api/runs/{run_id}/reviews")
async def get_reviews(run_id: str) -> JSONResponse:
    """读取追加保存的回看记录；损坏 JSONL 行通过 warnings 降级返回。"""
    run, error = _history_run_or_response(run_id)
    if error is not None:
        return error
    assert run is not None and run.run_dir is not None
    try:
        await asyncio.to_thread(
            history.derive_or_load_snapshot,
            run.run_dir,
            run.run_id,
            run.mode,
            run.params,
            run.bus.events,
            materialize=False,
        )
    except history.SnapshotUnavailableError as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "summary_unavailable", "error": str(exc)},
        )
    except history.HistoryValidationError as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "snapshot_corrupt", "error": str(exc)},
        )
    reviews, warnings = await asyncio.to_thread(history.read_reviews, run.run_dir)
    return JSONResponse(
        content={
            "status": "available",
            "run_id": run.run_id,
            "reviews": reviews,
            "warnings": warnings,
        }
    )


@app.post("/api/runs/{run_id}/reviews")
async def create_review(run_id: str, req: DecisionReviewRequest) -> JSONResponse:
    """首次物化旧 run 决策快照，并追加一条现在回看记录。"""
    run, error = _history_run_or_response(run_id)
    if error is not None:
        return error
    assert run is not None and run.run_dir is not None
    review_kwargs = _decision_review_kwargs(req)
    validation_kwargs = {key: value for key, value in review_kwargs.items() if key != "note"}
    try:
        preview_snapshot, _preview_status, preview_warnings = await asyncio.to_thread(
            history.derive_or_load_snapshot,
            run.run_dir,
            run.run_id,
            run.mode,
            run.params,
            run.bus.events,
            materialize=False,
        )
    except history.SnapshotUnavailableError as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "summary_unavailable", "error": str(exc)},
        )
    except history.HistoryValidationError as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "snapshot_corrupt", "error": str(exc)},
        )

    try:
        await asyncio.to_thread(
            history.validate_review_inputs,
            preview_snapshot,
            req.review_date,
            **validation_kwargs,
        )
    except history.HistoryValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "invalid_review_request", "error": str(exc)},
        )

    try:
        snapshot, snapshot_status, materialize_warnings = await asyncio.to_thread(
            history.derive_or_load_snapshot,
            run.run_dir,
            run.run_id,
            run.mode,
            run.params,
            run.bus.events,
            materialize=True,
        )
        snapshot_warnings = [*preview_warnings, *materialize_warnings]
    except history.HistoryValidationError as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "snapshot_corrupt", "error": str(exc)},
        )
    except OSError as exc:
        logger.warning("物化 decision snapshot 失败: run=%s", run_id, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "snapshot_write_failed", "error": str(exc)},
        )

    try:
        review = await asyncio.to_thread(
            history.create_and_append_review,
            run.run_dir,
            snapshot,
            req.review_date,
            **review_kwargs,
        )
        reviews, review_warnings = await asyncio.to_thread(history.read_reviews, run.run_dir)
    except history.HistoryValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "invalid_review_request", "error": str(exc)},
        )
    except OSError as exc:
        logger.warning("保存 decision review 失败: run=%s", run_id, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "review_write_failed", "error": str(exc)},
        )
    return JSONResponse(
        status_code=201,
        content={
            "status": "created",
            "materialized": snapshot_status == "frozen",
            "review": review,
            "review_count": len(reviews),
            "warnings": [*snapshot_warnings, *review_warnings],
        },
    )


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request, after: int = Query(0)) -> Any:
    """SSE 事件流。

    功能：
        先补发 seq>after 的历史事件，再持续推送新事件；每 15s 发一条
        `: ping` 注释行保活；客户端断开时退出循环，清理由框架完成。
        已结束的历史 run 也支持一次性拉取全部事件用于只读回看。
    参数：
        run_id: 运行标识。
        request: 请求对象（用于探测断连）。
        after: 断线重连游标。
    返回值：
        StreamingResponse（text/event-stream）。
    """
    run = ENGINE.runs.get(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": "run 不存在"})

    async def generator():
        # 先补发历史：以 after 之后的事件为起点。
        cursor = after
        for event in run.bus.snapshot(after):
            yield _sse_format(event)
            cursor = max(cursor, event.get("seq", 0))
        while True:
            if await request.is_disconnected():
                break
            # run 已结束且没有新增事件时结束流，避免空转。
            if run.status != "running" and run.bus.max_seq <= cursor:
                break
            has_new = await run.bus.wait_beyond(cursor, config.HEARTBEAT_SECONDS)
            if has_new:
                for event in run.bus.snapshot(cursor):
                    yield _sse_format(event)
                    cursor = max(cursor, event.get("seq", 0))
            else:
                # 心跳注释行不占 seq，仅用于保持连接与探测断开。
                yield ": ping\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _sse_format(event: dict[str, Any]) -> str:
    """把事件字典格式化成一条 SSE data 帧。

    参数：
        event: 事件字典。
    返回值：
        `data: {...}\n\n` 文本。
    """
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> JSONResponse:
    """取消一次运行。

    参数：
        run_id: 运行标识。
    返回值：
        {ok} 或 404/409。
    """
    if run_id not in ENGINE.runs:
        return JSONResponse(status_code=404, content={"error": "run 不存在"})
    ok = await ENGINE.cancel_run(run_id)
    if not ok:
        return JSONResponse(status_code=409, content={"ok": False, "error": "run 未在运行中"})
    return JSONResponse(content={"ok": True})


@app.post("/api/runs/{run_id}/steps/{step_id}/complete")
async def complete_step(run_id: str, step_id: str, req: StepCompleteRequest | None = None) -> JSONResponse:
    """手动标记 LLM 步骤完成。

    参数：
        run_id: 运行标识。
        step_id: 步骤标识。
        req: {force} 请求体（可空）。
    返回值：
        {ok} 或 404/409（含缺失产物清单）。
    """
    if run_id not in ENGINE.runs:
        return JSONResponse(status_code=404, content={"error": "run 不存在"})
    force = bool(req.force) if req else False
    ok, missing = ENGINE.manual_complete(run_id, step_id, force)
    if not ok:
        return JSONResponse(status_code=409, content={"ok": False, "missing": missing})
    return JSONResponse(content={"ok": True})


@app.post("/api/runs/{run_id}/steps/{step_id}/skip")
async def skip_step(run_id: str, step_id: str) -> JSONResponse:
    """手动跳过一个步骤。

    参数：
        run_id: 运行标识。
        step_id: 步骤标识。
    返回值：
        {ok} 或 404/409。
    """
    if run_id not in ENGINE.runs:
        return JSONResponse(status_code=404, content={"error": "run 不存在"})
    ok = ENGINE.manual_skip(run_id, step_id)
    if not ok:
        return JSONResponse(status_code=409, content={"ok": False, "error": "步骤不可跳过或已结束"})
    return JSONResponse(content={"ok": True})


# ---------------------------------------------------------------------------
# artifact 读取
# ---------------------------------------------------------------------------

@app.get("/api/artifact")
async def get_artifact(path: str = Query(...)) -> JSONResponse:
    """在白名单约束下读取 artifact。

    参数：
        path: 绝对或相对路径。
    返回值：
        artifact 内容；白名单外 403，缺失 404。
    """
    try:
        result = await asyncio.to_thread(state_reader.read_artifact, path)
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"error": str(exc)})
    except FileNotFoundError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - 兜底：读取异常统一转 500，避免服务崩溃
        logger.warning("读取 artifact 失败: %s", path, exc_info=True)
        return JSONResponse(status_code=500, content={"error": f"读取失败: {exc}"})
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# 前端静态资源
# ---------------------------------------------------------------------------

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>research_console</title>
<style>
 body{font-family:system-ui,"Segoe UI",sans-serif;margin:0;padding:2.5rem;background:#0f1115;color:#e6e6e6;line-height:1.7}
 code{background:#1d2027;padding:.15rem .4rem;border-radius:4px;color:#ffd479}
 a{color:#5aa9ff}
 .card{max-width:760px;margin:0 auto;background:#171a21;border:1px solid #262b34;border-radius:12px;padding:1.5rem 2rem}
 h1{font-size:1.4rem} li{margin:.35rem 0}
</style>
</head>
<body>
<div class="card">
<h1>research_console 后端已就绪</h1>
<p>前端静态资源（<code>research_console/static/</code>）尚未就位。后端 API 可直接调用：</p>
<ul>
 <li><a href="/api/health">GET /api/health</a> — 健康检查</li>
 <li><a href="/api/catalog">GET /api/catalog</a> — 公司/年度产物目录</li>
 <li><a href="/docs">GET /docs</a> — OpenAPI 交互文档</li>
 <li><code>POST /api/runs</code> — 创建运行（company / industry / demo / replay）</li>
 <li><code>GET /api/runs/&lt;id&gt;/events</code> — SSE 事件流</li>
</ul>
<p>把前端产物放入 <code>static/</code> 后刷新本页即可加载游戏化界面。</p>
</div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """站点首页。

    功能：
        static/index.html 存在时返回真实前端页面，否则返回占位说明页。
        以存在性判断避免 static/ 缺失导致启动或访问失败。
    参数：
        无。
    返回值：
        HTMLResponse。
    """
    index_file = config.STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content=_PLACEHOLDER_HTML)


def _mount_static() -> None:
    """挂载前端静态目录（仅在其存在时）。

    功能：
        static/ 由前端提供，index.html 以相对路径引用同目录的 js/css，
        因此必须把整个目录挂载到根路径 "/"，浏览器请求 /app.js、/style.css
        才能命中。API 路由先于本挂载注册，匹配优先级更高，不会被遮蔽；
        static/ 缺失时跳过挂载，保证后端可独立启动（GET / 走占位页）。
    参数：
        无。
    返回值：
        无。
    """
    if config.STATIC_DIR.exists() and config.STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
        logger.info("已挂载静态目录到根路径: %s", config.STATIC_DIR)
    else:
        logger.info("静态目录不存在，跳过挂载: %s", config.STATIC_DIR)


_mount_static()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
