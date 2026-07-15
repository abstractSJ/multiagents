"""公司历史决策快照与现在回看。

功能：
- 从公司 run 的最后一个 ``run_completed.payload.summary`` 构建不可变决策快照；
- 以同目录临时文件、文件同步与硬链接实现“原子且不可覆盖”的首次冻结；
- 对旧 run 支持只派生不落盘，并在首次创建 review 时物化快照；
- 追加保存、宽容读取 review JSONL，损坏行只形成 warnings；
- 从本地腾讯 ``qfqday`` 与东方财富 ``TRADE_DATE/CLOSE_PRICE`` 数据提取
  不晚于请求日的最近交易日收盘价，并生成描述性回看指标。

本模块只做历史记录与描述性比较，不进行投资判断，也不把股价变化解释为因果结果。
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import math
import os
import secrets
from pathlib import Path
from typing import Any, Iterable

from research_console import config, state_reader

SNAPSHOT_FILENAME = "decision_snapshot.json"
REVIEWS_FILENAME = "reviews.jsonl"
SNAPSHOT_SCHEMA_VERSION = "1.0"
REVIEW_SCHEMA_VERSION = "1.0"

# 固定限制语必须随每一份 review 保存，避免前端或调用方遗漏关键解释边界。
REVIEW_LIMITATIONS = [
    "spot_price_change 仅为股价变动，不含分红、送转、税费与再投资，因此不是股东总回报（TSR）。",
    "回看结果是描述性对照，不构成因果归因、策略有效性证明或投资建议。",
]

_PROVIDER_ORDER = ("tencent_qfqday", "eastmoney_trade_close")


class HistoryValidationError(ValueError):
    """历史快照或回看请求不满足数据契约。"""


class SnapshotUnavailableError(HistoryValidationError):
    """run 无法提供可冻结的公司决策摘要。"""


def snapshot_path(run_dir: Path) -> Path:
    """返回单次 run 的决策快照路径。

    参数：
        run_dir: ``console_workspace/runs/<run_id>`` 目录。
    返回值：
        ``decision_snapshot.json`` 路径。
    """
    return Path(run_dir) / SNAPSHOT_FILENAME


def reviews_path(run_dir: Path) -> Path:
    """返回单次 run 的 reviews JSONL 路径。"""
    return Path(run_dir) / REVIEWS_FILENAME


def _date_text(value: Any) -> str:
    """从日期或时间值中宽容提取 ISO 日期文本。"""
    text = str(value or "").strip()
    if not text:
        return ""
    # 同时兼容 ISO 时间、东方财富空格时间和 YYYYMMDD。
    candidate = text[:10]
    if len(text) >= 8 and text[:8].isdigit() and "-" not in text[:10]:
        candidate = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return _dt.date.fromisoformat(candidate).isoformat()
    except ValueError:
        return ""


def parse_review_date(value: Any, *, cutoff: str = "", today: _dt.date | None = None) -> _dt.date:
    """校验 review_date 是不早于知识截止日且不晚于今天的 ISO 日期。

    为什么拒绝未来日期：本地行情读取遵守“不晚于请求日”的原则，但未来请求会让
    “现在回看”在语义上变成预测，和本模块的描述性历史用途冲突。

    参数：
        value: 请求日期，必须为 ``YYYY-MM-DD``。
        cutoff: 可选知识截止日；有效时 review_date 不得更早。
        today: 测试可注入的当前日期。
    返回值：
        校验后的 ``date``。
    异常：
        HistoryValidationError: 日期为空、格式错误、早于 cutoff 或晚于今天。
    """
    text = str(value or "").strip()
    try:
        parsed = _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise HistoryValidationError("review_date 必须是 YYYY-MM-DD 格式") from exc
    today_value = today or _dt.date.today()
    if parsed > today_value:
        raise HistoryValidationError("review_date 不能晚于今天")
    cutoff_text = _date_text(cutoff)
    if cutoff_text and parsed < _dt.date.fromisoformat(cutoff_text):
        raise HistoryValidationError("review_date 不能早于 decision snapshot 的 knowledge_cutoff")
    return parsed


def _manual_observation_input(
    *,
    stock_code: str,
    price: Any,
    observation_date: Any,
    source: Any,
    label: str,
    earliest_date: _dt.date | None,
    latest_date: _dt.date,
) -> dict[str, Any] | None:
    """校验一组可选手工价格字段，并转换成统一 observation。

    只要价格、日期、来源任一字段出现，就要求三者同时齐全。这样 review 不会保存一条
    无法复核时点或来源的裸价格。日期必须落在该 observation 允许的历史窗口内。
    """
    supplied = any(value not in (None, "") for value in (price, observation_date, source))
    if not supplied:
        return None
    number = _positive_number(price)
    if number is None:
        raise HistoryValidationError(f"{label} 必须是有限正数")
    date_text = str(observation_date or "").strip()
    try:
        parsed_date = _dt.date.fromisoformat(date_text)
    except ValueError as exc:
        raise HistoryValidationError(f"{label}日期必须是 YYYY-MM-DD 格式") from exc
    if parsed_date.isoformat() != date_text:
        raise HistoryValidationError(f"{label}日期必须是 YYYY-MM-DD 格式")
    if parsed_date > latest_date:
        raise HistoryValidationError(f"{label}日期不能晚于 {latest_date.isoformat()}")
    if earliest_date is not None and parsed_date < earliest_date:
        raise HistoryValidationError(f"{label}日期不能早于 {earliest_date.isoformat()}")
    source_text = str(source or "").strip()
    if not source_text:
        raise HistoryValidationError(f"{label}来源不能为空")
    return {
        "status": "available",
        "stock_code": str(stock_code or "").strip(),
        "requested_date": latest_date.isoformat(),
        "observation_date": date_text,
        "close_price": number,
        "source": source_text,
        "source_kind": "manual_input",
    }


def validate_review_inputs(
    snapshot: dict[str, Any],
    review_date: str,
    *,
    current_price: Any = None,
    current_price_date: Any = None,
    current_price_source: Any = None,
    benchmark_code: str = "",
    benchmark_baseline_price: Any = None,
    benchmark_baseline_date: Any = None,
    benchmark_baseline_source: Any = None,
    benchmark_current_price: Any = None,
    benchmark_current_date: Any = None,
    benchmark_current_source: Any = None,
    falsification_status: str = "unknown",
    falsification_notes: str = "",
    today: _dt.date | None = None,
) -> dict[str, Any]:
    """在任何不可逆快照物化前校验 review 的全部用户输入。

    返回值包含规范化 review 日期、三组可选手工 observation 和证伪状态，供 API
    预检与 ``build_review`` 共用，避免两处规则漂移。
    """
    cutoff_text = _date_text(snapshot.get("knowledge_cutoff"))
    if not cutoff_text:
        raise HistoryValidationError("decision snapshot 缺少有效 knowledge_cutoff")
    review_day = parse_review_date(review_date, cutoff=cutoff_text, today=today)
    cutoff_day = _dt.date.fromisoformat(cutoff_text)
    target = snapshot.get("target") if isinstance(snapshot.get("target"), dict) else {}
    stock_code = str(target.get("stock_code") or "").strip()
    benchmark_code_text = str(benchmark_code or "").strip()

    manual_current = _manual_observation_input(
        stock_code=stock_code,
        price=current_price,
        observation_date=current_price_date,
        source=current_price_source,
        label="current_price",
        earliest_date=cutoff_day,
        latest_date=review_day,
    )
    manual_benchmark_baseline = _manual_observation_input(
        stock_code=benchmark_code_text,
        price=benchmark_baseline_price,
        observation_date=benchmark_baseline_date,
        source=benchmark_baseline_source,
        label="benchmark_baseline_price",
        earliest_date=None,
        latest_date=cutoff_day,
    )
    manual_benchmark_current = _manual_observation_input(
        stock_code=benchmark_code_text,
        price=benchmark_current_price,
        observation_date=benchmark_current_date,
        source=benchmark_current_source,
        label="benchmark_current_price",
        earliest_date=cutoff_day,
        latest_date=review_day,
    )
    if (manual_benchmark_baseline or manual_benchmark_current) and not benchmark_code_text:
        raise HistoryValidationError("提供 benchmark 手工价格时 benchmark_code 不能为空")

    status = str(falsification_status or "unknown").strip().lower()
    if status not in {"unknown", "held", "breached"}:
        raise HistoryValidationError("falsification_status 必须是 unknown、held 或 breached")
    return {
        "review_date": review_day.isoformat(),
        "cutoff": cutoff_text,
        "manual_current": manual_current,
        "manual_benchmark_baseline": manual_benchmark_baseline,
        "manual_benchmark_current": manual_benchmark_current,
        "benchmark_code": benchmark_code_text,
        "falsification_status": status,
        "falsification_notes": str(falsification_notes or "").strip(),
    }


def _terminal_summary(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """从事件序列取最后一个 run_completed 的 summary 与终态时间。"""
    summary: dict[str, Any] | None = None
    terminal_ts = ""
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "run_completed":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        candidate = payload.get("summary")
        summary = candidate if isinstance(candidate, dict) else None
        terminal_ts = str(event.get("ts") or "")
    return summary, terminal_ts


def _artifact_signature(path_value: Any) -> dict[str, Any]:
    """生成来源产物的可复核签名。

    签名同时保存大小、纳秒 mtime 与 SHA-256。mtime/size 便于快速比较，SHA-256
    用于确认内容身份；文件缺失或不可读时返回明确状态，而不是让快照冻结失败。
    """
    path = Path(str(path_value or ""))
    result: dict[str, Any] = {"path": str(path), "status": "unavailable"}
    if not str(path_value or "").strip():
        return result
    try:
        stat = path.stat()
        if not path.is_file():
            return result
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        result["warning"] = str(exc)
        return result
    result.update(
        {
            "status": "available",
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }
    )
    return result


def _source_artifacts(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """为 summary.artifact_paths 中的来源文件生成签名表。"""
    paths = summary.get("artifact_paths") if isinstance(summary.get("artifact_paths"), dict) else {}
    return {
        str(name): _artifact_signature(path)
        for name, path in paths.items()
        if str(path or "").strip()
    }


def build_decision_snapshot(
    run_id: str,
    mode: str,
    params: dict[str, Any] | None,
    events: Iterable[dict[str, Any]],
    *,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    """从 run 的最后一个完成事件构建公司决策快照。

    ``decision`` 使用 ``copy.deepcopy`` 与事件内 summary 脱离引用。后续调用方即使
    修改原 summary、前端状态或测试夹具，也不会改变已经构建的快照对象。

    参数：
        run_id: 运行标识。
        mode: run 模式；必须为 company。
        params: 原始运行参数。
        events: 完整事件序列。
        frozen_at: 可选冻结时间；缺省取当前本地时间。
    返回值：
        决策快照字典。
    异常：
        SnapshotUnavailableError: 非公司 run、没有 run_completed 或没有 summary。
    """
    if mode != "company":
        raise SnapshotUnavailableError("仅 company run 支持 decision snapshot")
    summary, terminal_ts = _terminal_summary(events)
    if summary is None:
        raise SnapshotUnavailableError("run 没有可用的 run_completed.summary")

    params = dict(params or {})
    decision = copy.deepcopy(summary)
    as_of_date = _date_text(decision.get("as_of_date") or params.get("as_of_date"))
    knowledge_cutoff = as_of_date or _date_text(terminal_ts) or _date_text(frozen_at) or _dt.date.today().isoformat()
    target = {
        "company_name": str(decision.get("company_name") or params.get("company_name") or ""),
        "stock_code": str(decision.get("stock_code") or params.get("stock_code") or ""),
        "report_year": str(decision.get("report_year") or params.get("report_year") or params.get("fiscal_year") or ""),
        "report_type": str(params.get("report_type") or "annual"),
        "as_of_date": as_of_date or knowledge_cutoff,
    }
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "artifact_type": "company_decision_snapshot",
        "run_id": str(run_id),
        "frozen_at": frozen_at or state_reader.now_iso(),
        "knowledge_cutoff": knowledge_cutoff,
        "target": target,
        "decision": decision,
        "source_artifacts": _source_artifacts(decision),
    }


def load_decision_snapshot(run_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """宽容读取已经物化的决策快照。

    返回值：
        ``(snapshot, warnings)``。文件不存在时二者分别为 None、空列表；损坏时
        snapshot 为 None，并返回可展示 warning。
    """
    path = snapshot_path(run_dir)
    if not path.exists():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"decision snapshot 损坏或不可读: {exc}"]
    if not isinstance(payload, dict):
        return None, ["decision snapshot 顶层不是 JSON 对象"]
    return payload, []


def _write_json_no_replace(path: Path, payload: dict[str, Any]) -> bool:
    """原子写入 JSON 且绝不覆盖已有目标。

    实现先在同目录完整写入并 fsync 临时文件，再用硬链接把完整 inode 原子发布到
    目标名。若目标已经存在，``os.link`` 会以 ``FileExistsError`` 失败，因此不存在
    读后检查与覆盖之间的竞态窗口。最后删除临时名字，目标内容保持不变。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with temp_path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            return False
        # 目录 fsync 在 Windows 不可用；POSIX 上尽力同步目录项，但失败不影响已发布文件。
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return True
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def freeze_decision_snapshot(run_dir: Path, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """首次冻结快照；已有文件永不覆盖。

    返回值：
        ``(权威快照, created)``。并发或重复调用命中已有文件时返回磁盘中的原快照。
    """
    path = snapshot_path(run_dir)
    created = _write_json_no_replace(path, snapshot)
    if created:
        return copy.deepcopy(snapshot), True
    existing, warnings = load_decision_snapshot(run_dir)
    if existing is None:
        raise HistoryValidationError("decision snapshot 已存在但无法读取：" + "；".join(warnings))
    return existing, False


def derive_or_load_snapshot(
    run_dir: Path,
    run_id: str,
    mode: str,
    params: dict[str, Any] | None,
    events: Iterable[dict[str, Any]],
    *,
    materialize: bool,
) -> tuple[dict[str, Any], str, list[str]]:
    """读取新 run 快照，或从旧 run 最后 summary 派生。

    GET decision 使用 ``materialize=False``，保证旧 run 只读派生不落盘；首次 POST
    review 使用 ``materialize=True``，把同一派生结果永久冻结后再创建 review。
    """
    existing, warnings = load_decision_snapshot(run_dir)
    if existing is not None:
        return existing, "frozen", warnings
    if warnings:
        # 已有但损坏的快照不能被静默覆盖，否则违反“不可覆盖”原则。
        raise HistoryValidationError("；".join(warnings))
    derived = build_decision_snapshot(run_id, mode, params, events)
    if not materialize:
        return derived, "derived", []
    authoritative, _created = freeze_decision_snapshot(run_dir, derived)
    return authoritative, "frozen", []


def read_reviews(run_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """读取 reviews.jsonl，损坏行跳过并返回 warnings。"""
    path = reviews_path(run_dir)
    if not path.exists():
        return [], []
    reviews: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"reviews 读取失败: {exc}"]
    for line_number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"reviews.jsonl 第 {line_number} 行损坏，已跳过: {exc.msg}")
            continue
        if not isinstance(payload, dict):
            warnings.append(f"reviews.jsonl 第 {line_number} 行不是 JSON 对象，已跳过")
            continue
        reviews.append(payload)
    return reviews, warnings


def append_review(run_dir: Path, review: dict[str, Any]) -> None:
    """以单次追加写保存一条 review，并 fsync 保证返回前尽力落盘。"""
    path = reviews_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(review, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        # O_APPEND + 单次 os.write 避免多个请求把同一 JSON 行交叉写入。
        written = os.write(fd, line)
        if written != len(line):
            raise OSError("reviews.jsonl 未完整写入")
        os.fsync(fd)
    finally:
        os.close(fd)


def _positive_number(value: Any) -> float | None:
    """仅接受有限正数价格；零、负数、布尔值和 NaN 均视为不可用。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _walk_json(node: Any) -> Iterable[Any]:
    """深度遍历任意 JSON 结构，供两类行情格式宽容识别。"""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value)


def _parse_tencent_rows(payload: Any) -> list[tuple[str, float]]:
    """从任意嵌套位置提取腾讯 qfqday 的日期与收盘价。"""
    found: dict[str, float] = {}
    for node in _walk_json(payload):
        if not isinstance(node, dict):
            continue
        for key, rows in node.items():
            if str(key).lower() != "qfqday" or not isinstance(rows, list):
                continue
            for row in rows:
                date_text = ""
                close = None
                if isinstance(row, (list, tuple)) and len(row) >= 3:
                    date_text = _date_text(row[0])
                    close = _positive_number(row[2])
                elif isinstance(row, dict):
                    date_text = _date_text(row.get("date") or row.get("trade_date"))
                    close = _positive_number(row.get("close") or row.get("close_price"))
                if date_text and close is not None:
                    found[date_text] = close
    return sorted(found.items())


def _parse_eastmoney_rows(payload: Any) -> list[tuple[str, float]]:
    """从任意嵌套对象提取东方财富 TRADE_DATE/CLOSE_PRICE。"""
    found: dict[str, float] = {}
    for node in _walk_json(payload):
        if not isinstance(node, dict):
            continue
        normalized = {str(key).upper(): value for key, value in node.items()}
        if "TRADE_DATE" not in normalized or "CLOSE_PRICE" not in normalized:
            continue
        date_text = _date_text(normalized.get("TRADE_DATE"))
        close = _positive_number(normalized.get("CLOSE_PRICE"))
        if date_text and close is not None:
            found[date_text] = close
    return sorted(found.items())


def _price_files(stock_code: str, roots: Iterable[Path] | None = None) -> list[Path]:
    """定位可能包含目标代码行情的本地 JSON 文件。

    默认只扫描估值市场数据工作区，避免遍历 PDF、RAG 等大型目录。文件名通常含代码，
    同时保留 supplement 这类聚合包作为兜底。
    """
    code = str(stock_code or "").strip()
    search_roots = list(roots or [config.COLLECTOR_WORKSPACE / "valuation_market_data"])
    files: list[Path] = []
    for root in search_roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            name = path.name.lower()
            if code and code not in str(path) and "supplement" not in name:
                continue
            files.append(path)
    return sorted(set(files), key=lambda item: str(item))


def load_local_price_series(
    stock_code: str,
    roots: Iterable[Path] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """读取目标股票的本地腾讯/东方财富价格序列。

    返回值按 provider 分组，每条记录含 date/close/source_path/source_signature。损坏文件
    直接跳过；review 的可用性不能因单个缓存文件异常而整体失败。
    """
    series: dict[str, dict[str, dict[str, Any]]] = {provider: {} for provider in _PROVIDER_ORDER}
    for path in _price_files(stock_code, roots):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        signature: dict[str, Any] | None = None
        for provider, parser in (
            ("tencent_qfqday", _parse_tencent_rows),
            ("eastmoney_trade_close", _parse_eastmoney_rows),
        ):
            rows = parser(payload)
            if not rows:
                continue
            if signature is None:
                signature = _artifact_signature(path)
            for date_text, close in rows:
                # 同日重复时后读取文件覆盖先读取文件；路径排序让结果稳定可复现。
                series[provider][date_text] = {
                    "date": date_text,
                    "close": close,
                    "source": provider,
                    "source_path": str(path),
                    "source_signature": signature,
                }
    return {
        provider: [records[key] for key in sorted(records)]
        for provider, records in series.items()
    }


def _nearest_observation(records: list[dict[str, Any]], requested_date: str, stock_code: str) -> dict[str, Any]:
    """选取不晚于 requested_date 的最近一条正价格记录。"""
    eligible = [item for item in records if str(item.get("date") or "") <= requested_date]
    if not eligible:
        return {
            "status": "unavailable",
            "stock_code": stock_code,
            "requested_date": requested_date,
        }
    item = eligible[-1]
    close = _positive_number(item.get("close"))
    if close is None:
        return {
            "status": "unavailable",
            "stock_code": stock_code,
            "requested_date": requested_date,
            "warning": "最近记录价格不是有限正数",
        }
    return {
        "status": "available",
        "stock_code": stock_code,
        "requested_date": requested_date,
        "observation_date": item.get("date"),
        "close_price": close,
        "source": item.get("source"),
        "source_path": item.get("source_path"),
        "source_signature": item.get("source_signature"),
    }


def _snapshot_baseline(snapshot: dict[str, Any], requested_date: str, stock_code: str) -> dict[str, Any]:
    """从冻结 decision 的价格字段构造最低优先级基准观察。"""
    decision = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else {}
    observation = decision.get("price_observation") if isinstance(decision.get("price_observation"), dict) else {}
    price = _positive_number(observation.get("price") or decision.get("current_price"))
    date_text = _date_text(observation.get("observation_date") or decision.get("as_of_date") or requested_date)
    if price is None or not date_text or date_text > requested_date:
        return {"status": "unavailable", "stock_code": stock_code, "requested_date": requested_date}
    return {
        "status": "available",
        "stock_code": stock_code,
        "requested_date": requested_date,
        "observation_date": date_text,
        "close_price": price,
        "source": "decision_snapshot",
        "source_path": "",
        "price_basis": decision.get("price_basis") or decision.get("price_source") or "",
    }


def resolve_price_pair(
    stock_code: str,
    baseline_date: str,
    current_date: str,
    *,
    snapshot: dict[str, Any] | None = None,
    manual_baseline: dict[str, Any] | None = None,
    manual_current: dict[str, Any] | None = None,
    prefer_snapshot_baseline: bool = False,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """解析 baseline/current 价格，并按本地同源、冻结价、手工值顺序回退。

    公司股票使用 ``prefer_snapshot_baseline=True``：若本地日线无法以同一 provider
    同时覆盖两端，baseline 优先回退到冻结 decision price；benchmark 则允许本地和
    手工 observation 分别补齐缺失端。所有口径切换都会进入 ``basis_warnings``。
    """
    code = str(stock_code or "").strip()
    baseline_request = _date_text(baseline_date)
    current_request = _date_text(current_date)
    unavailable_baseline = {"status": "unavailable", "requested_date": baseline_request, "stock_code": code}
    unavailable_current = {"status": "unavailable", "requested_date": current_request, "stock_code": code}
    if not code or not baseline_request or not current_request:
        return {
            "status": "unavailable",
            "same_source": False,
            "baseline": unavailable_baseline,
            "current": unavailable_current,
            "basis_warnings": [],
        }

    series = load_local_price_series(code, roots)
    observations: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for provider in _PROVIDER_ORDER:
        records = series.get(provider) or []
        observations[provider] = (
            _nearest_observation(records, baseline_request, code),
            _nearest_observation(records, current_request, code),
        )

    # 最高优先级始终是同一 provider 覆盖两端，避免复权方式和数据口径混杂。
    for provider in _PROVIDER_ORDER:
        baseline, current = observations[provider]
        if baseline.get("status") == current.get("status") == "available":
            return {
                "status": "available",
                "same_source": True,
                "source": provider,
                "baseline": baseline,
                "current": current,
                "basis_warnings": [],
            }

    def first_local(position: int) -> dict[str, Any]:
        for provider in _PROVIDER_ORDER:
            candidate = observations[provider][position]
            if candidate.get("status") == "available":
                return candidate
        return unavailable_baseline if position == 0 else unavailable_current

    warnings: list[str] = []
    current = first_local(1)
    if current.get("status") != "available" and manual_current is not None:
        current = copy.deepcopy(manual_current)
        warnings.append("current 未取得本地价格，使用用户手工输入价格、日期与来源。")

    if prefer_snapshot_baseline:
        current_source = str(current.get("source") or "")
        matching_local = observations.get(current_source, (unavailable_baseline, unavailable_current))[0]
        if matching_local.get("status") == "available":
            baseline = matching_local
        else:
            baseline = _snapshot_baseline(snapshot or {}, baseline_request, code)
            if baseline.get("status") == "available":
                warnings.append(
                    "baseline 未取得与 current 同源的本地日线，回退到冻结 decision price；"
                    "该价格口径可能与 current 不一致。"
                )
            else:
                baseline = first_local(0)
                if baseline.get("status") == "available":
                    warnings.append("冻结 decision price 不可用，baseline 使用非同源本地价格。")
                elif manual_baseline is not None:
                    baseline = copy.deepcopy(manual_baseline)
                    warnings.append("baseline 本地与冻结价格均不可用，使用用户手工输入。")
    else:
        baseline = first_local(0)
        if baseline.get("status") != "available" and manual_baseline is not None:
            baseline = copy.deepcopy(manual_baseline)
            warnings.append("benchmark baseline 未取得本地价格，使用用户手工输入。")

    available_count = sum(item.get("status") == "available" for item in (baseline, current))
    same_source = bool(
        available_count == 2
        and baseline.get("source")
        and baseline.get("source") == current.get("source")
    )
    if available_count == 2 and not same_source:
        warnings.append("baseline/current 来源不同，变化率属于混合口径描述性结果。")
    return {
        "status": "available" if available_count == 2 else ("partial" if available_count else "unavailable"),
        "same_source": same_source,
        "baseline": baseline,
        "current": current,
        "basis_warnings": list(dict.fromkeys(warnings)),
    }


def _price_change(pair: dict[str, Any]) -> float | None:
    """计算价格变化率；任一端无有效正价格时返回 None。"""
    baseline = pair.get("baseline") if isinstance(pair.get("baseline"), dict) else {}
    current = pair.get("current") if isinstance(pair.get("current"), dict) else {}
    start = _positive_number(baseline.get("close_price"))
    end = _positive_number(current.get("close_price"))
    if start is None or end is None:
        return None
    return round(end / start - 1.0, 6)


def _valuation_bucket(snapshot: dict[str, Any], current_price: float | None) -> dict[str, Any]:
    """按三档合理价值点构造四个价格区间，并返回到每个点的距离。

    三档必须全部是有限正数且严格满足 ``bear < base < bull``。缺档或非单调时，
    区间本身没有稳定含义，必须返回 unavailable，不能退化成“最近点”分类。
    """
    if current_price is None or current_price <= 0:
        return {"status": "unavailable", "bucket": "unavailable", "reason": "current_price_unavailable"}
    decision = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else {}
    fair_value = decision.get("fair_value") if isinstance(decision.get("fair_value"), dict) else {}
    points = {name: _positive_number(fair_value.get(name)) for name in ("bear", "base", "bull")}
    if any(points[name] is None for name in ("bear", "base", "bull")):
        return {"status": "unavailable", "bucket": "unavailable", "reason": "fair_value_incomplete"}
    bear = float(points["bear"])
    base = float(points["base"])
    bull = float(points["bull"])
    if not bear < base < bull:
        return {"status": "unavailable", "bucket": "unavailable", "reason": "fair_value_non_monotonic"}

    if current_price < bear:
        bucket = "below_bear"
    elif current_price < base:
        bucket = "bear_to_base"
    elif current_price <= bull:
        bucket = "base_to_bull"
    else:
        bucket = "above_bull"

    distances: dict[str, dict[str, float]] = {}
    for name, point in (("bear", bear), ("base", base), ("bull", bull)):
        signed = current_price - point
        distances[name] = {
            "signed": round(signed, 6),
            "absolute": round(abs(signed), 6),
            "pct": round(current_price / point - 1.0, 6),
        }
    return {
        "status": "available",
        "bucket": bucket,
        "fair_value_points": {"bear": bear, "base": base, "bull": bull},
        "distances_to_points": distances,
        "unit": fair_value.get("unit") or "元/股",
    }


def build_review(
    snapshot: dict[str, Any],
    review_date: str,
    *,
    current_price: Any = None,
    current_price_date: Any = None,
    current_price_source: Any = None,
    benchmark_code: str = "",
    benchmark_baseline_price: Any = None,
    benchmark_baseline_date: Any = None,
    benchmark_baseline_source: Any = None,
    benchmark_current_price: Any = None,
    benchmark_current_date: Any = None,
    benchmark_current_source: Any = None,
    falsification_status: str = "unknown",
    falsification_notes: str = "",
    note: str = "",
    roots: Iterable[Path] | None = None,
    created_at: str | None = None,
    today: _dt.date | None = None,
) -> dict[str, Any]:
    """基于冻结决策、本地行情与可选手工补数构建描述性 review。"""
    validated = validate_review_inputs(
        snapshot,
        review_date,
        current_price=current_price,
        current_price_date=current_price_date,
        current_price_source=current_price_source,
        benchmark_code=benchmark_code,
        benchmark_baseline_price=benchmark_baseline_price,
        benchmark_baseline_date=benchmark_baseline_date,
        benchmark_baseline_source=benchmark_baseline_source,
        benchmark_current_price=benchmark_current_price,
        benchmark_current_date=benchmark_current_date,
        benchmark_current_source=benchmark_current_source,
        falsification_status=falsification_status,
        falsification_notes=falsification_notes,
        today=today,
    )
    cutoff = validated["cutoff"]
    review_date_text = validated["review_date"]
    parsed_review_date = _dt.date.fromisoformat(review_date_text)
    target = snapshot.get("target") if isinstance(snapshot.get("target"), dict) else {}
    stock_code = str(target.get("stock_code") or "").strip()
    if not stock_code:
        raise HistoryValidationError("decision snapshot 缺少 target.stock_code")

    stock_prices = resolve_price_pair(
        stock_code,
        cutoff,
        review_date_text,
        snapshot=snapshot,
        manual_current=validated["manual_current"],
        prefer_snapshot_baseline=True,
        roots=roots,
    )
    spot_change = _price_change(stock_prices)
    current_observation = stock_prices.get("current") if isinstance(stock_prices.get("current"), dict) else {}
    resolved_current_price = _positive_number(current_observation.get("close_price"))

    benchmark = None
    benchmark_change = None
    benchmark_code_text = validated["benchmark_code"]
    if benchmark_code_text:
        benchmark = resolve_price_pair(
            benchmark_code_text,
            cutoff,
            review_date_text,
            manual_baseline=validated["manual_benchmark_baseline"],
            manual_current=validated["manual_benchmark_current"],
            roots=roots,
        )
        benchmark_change = _price_change(benchmark)
    excess = None
    if spot_change is not None and benchmark_change is not None:
        excess = round(spot_change - benchmark_change, 6)

    cutoff_date = _dt.date.fromisoformat(cutoff)
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_type": "company_decision_review",
        "review_id": f"rv_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}",
        "run_id": str(snapshot.get("run_id") or ""),
        "created_at": created_at or state_reader.now_iso(),
        "review_date": review_date_text,
        "knowledge_cutoff": cutoff,
        "target": copy.deepcopy(target),
        "benchmark_code": benchmark_code_text or None,
        "falsification_status": validated["falsification_status"],
        "falsification_notes": validated["falsification_notes"],
        "note": str(note or "").strip(),
        "prices": {
            "stock": stock_prices,
            "benchmark": benchmark or {"status": "unavailable", "reason": "benchmark_code 未提供"},
        },
        "metrics": {
            "elapsed_days": (parsed_review_date - cutoff_date).days,
            "spot_price_change": spot_change,
            "valuation_bucket": _valuation_bucket(snapshot, resolved_current_price),
            "benchmark_change": benchmark_change,
            "excess_return": excess,
        },
        "limitations": list(REVIEW_LIMITATIONS),
    }


def create_and_append_review(
    run_dir: Path,
    snapshot: dict[str, Any],
    review_date: str,
    *,
    current_price: Any = None,
    current_price_date: Any = None,
    current_price_source: Any = None,
    benchmark_code: str = "",
    benchmark_baseline_price: Any = None,
    benchmark_baseline_date: Any = None,
    benchmark_baseline_source: Any = None,
    benchmark_current_price: Any = None,
    benchmark_current_date: Any = None,
    benchmark_current_source: Any = None,
    falsification_status: str = "unknown",
    falsification_notes: str = "",
    note: str = "",
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """构建并追加保存 review，返回保存的完整对象。"""
    review = build_review(
        snapshot,
        review_date,
        current_price=current_price,
        current_price_date=current_price_date,
        current_price_source=current_price_source,
        benchmark_code=benchmark_code,
        benchmark_baseline_price=benchmark_baseline_price,
        benchmark_baseline_date=benchmark_baseline_date,
        benchmark_baseline_source=benchmark_baseline_source,
        benchmark_current_price=benchmark_current_price,
        benchmark_current_date=benchmark_current_date,
        benchmark_current_source=benchmark_current_source,
        falsification_status=falsification_status,
        falsification_notes=falsification_notes,
        note=note,
        roots=roots,
    )
    append_review(run_dir, review)
    return review


def run_history_brief(
    mode: str,
    params: dict[str, Any] | None,
    events: Iterable[dict[str, Any]],
    run_dir: Path | None,
) -> dict[str, Any]:
    """生成 run 列表使用的基准日、冻结状态与回看数量。"""
    if mode != "company":
        return {"decision_status": "unsupported", "baseline_date": None, "review_count": 0}
    existing = None
    if run_dir:
        existing, _warnings = load_decision_snapshot(run_dir)
    summary, terminal_ts = _terminal_summary(events)
    baseline = ""
    if existing:
        baseline = _date_text(existing.get("knowledge_cutoff"))
    elif summary:
        baseline = _date_text(summary.get("as_of_date") or (params or {}).get("as_of_date") or terminal_ts)
    reviews, _warnings = read_reviews(run_dir) if run_dir else ([], [])
    return {
        "decision_status": "frozen" if existing else ("derived" if summary else "unavailable"),
        "baseline_date": baseline or None,
        "review_count": len(reviews),
    }
