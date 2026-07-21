"""research_state 读取与工作区盘点。

功能：
- 组装并调用 audit 脚本（研究状态审计器），解析 stdout 中的 research_state JSON；
- 定位与读取 research_state.json；
- 扫描各工作区构建公司/年度目录（catalog），供前端选择研究对象；
- 在白名单约束下安全读取 artifact 文件；
- 对估值报告做宽容字段映射，抽取结论卡所需的三档估值与现价。

本模块不做任何研究判断，只做文件与结构化数据的读取。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import sys
from collections.abc import MutableSet
from pathlib import Path
from typing import Any

from research_console import config
from research_orchestrator_scripts.audit_company_research_state import default_state_output_path

logger = logging.getLogger("research_console.state_reader")

# 估值三档情景的宽容键名映射：估值报告由 LLM 生成，字段名存在中英文变体。
_SCENARIO_ALIASES: dict[str, tuple[str, ...]] = {
    "bear": ("bear", "悲观", "pessimistic", "low", "bear_case"),
    "base": ("base", "基准", "neutral", "中性", "mid", "base_case"),
    "bull": ("bull", "乐观", "optimistic", "high", "bull_case"),
}

# 估值报告来自不同版本的角色提示词，既出现冻结契约值，也出现 fairly_valued、
# reasonably_valued 或中文短语。结论卡对外统一成五个稳定枚举，同时保留 raw 便于审计。
_VALUATION_VIEW_ALIASES: dict[str, tuple[str, ...]] = {
    "undervalued": ("undervalued", "under_valued", "cheap", "低估", "偏低", "便宜"),
    "fair": ("fair", "fairly_valued", "fair_valued", "reasonable", "reasonably_valued", "合理", "中性"),
    "overvalued": ("overvalued", "over_valued", "expensive", "高估", "偏高", "昂贵"),
    "watch_only": ("watch_only", "watchlist_only", "observe", "observation", "仅观察", "观察"),
}


def now_iso() -> str:
    """生成带本地时区的 ISO 时间串。

    功能：
        统一事件与状态的时间戳格式（形如 2026-07-13T12:00:00+08:00）。
    参数：
        无。
    返回值：
        本地时区 ISO 8601 字符串。
    """
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def mtime_iso(path: Path) -> str:
    """把文件修改时间转成本地时区 ISO 串。

    功能：
        replay 模式用文件 mtime 合成事件时间轴。
    参数：
        path: 目标文件路径。
    返回值：
        ISO 8601 字符串；文件不存在时返回当前时间。
    """
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return now_iso()
    return _dt.datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# audit 脚本调用与 research_state 解析
# ---------------------------------------------------------------------------

def build_audit_command(params: dict[str, Any], write_state: bool = True) -> list[str]:
    """构建 audit 脚本命令行。

    功能：
        把研究请求参数翻译成 audit_company_research_state.py 的命令行参数；
        target 为 6 位数字时脚本会自动视作股票代码，这里原样透传。
    参数：
        params: 包含 target/stock_code/company_name/report_year/report_type/
                depth/focus/as_of_date/force_refresh 的参数字典，字段均可缺省。
        write_state: 是否附加 --write-state 让脚本落盘 research_state.json。
    返回值：
        subprocess 参数列表（不经 shell）。
    """
    cmd = [sys.executable, str(config.AUDIT_SCRIPT)]
    normalized = dict(params or {})
    # Console API 与 /rec 使用 fiscal_year，audit CLI 内部使用 report_year；在唯一
    # 适配边界统一别名，避免预检/租约观察 2025、协调器却研究 2024。
    if not normalized.get("report_year") and normalized.get("fiscal_year"):
        normalized["report_year"] = normalized["fiscal_year"]
    mapping = [
        ("target", "--target"),
        ("stock_code", "--stock-code"),
        ("company_name", "--company-name"),
        ("report_year", "--report-year"),
        ("report_type", "--report-type"),
        ("filing_policy", "--filing-policy"),
        ("annual_lookback", "--annual-lookback"),
        ("depth", "--depth"),
        ("focus", "--focus"),
        ("as_of_date", "--as-of-date"),
    ]
    for key, flag in mapping:
        value = str(normalized.get(key) or "").strip()
        if value:
            cmd.extend([flag, value])
    if params.get("force_refresh"):
        cmd.append("--force-refresh")
    if write_state:
        cmd.append("--write-state")
    return cmd


def subprocess_env(strip_claude: bool = False) -> dict[str, str]:
    """构建子进程环境变量。

    功能：
        在当前环境基础上强制 UTF-8 输入输出，避免 Windows 控制台编码问题；
        claude CLI 子进程还需剥离所有 CLAUDE 开头的变量，防止嵌套会话冲突。
    参数：
        strip_claude: 是否剥离 CLAUDE* 环境变量。
    返回值：
        环境变量字典。
    """
    env = dict(os.environ)
    if strip_claude:
        env = {k: v for k, v in env.items() if not k.upper().startswith("CLAUDE")}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def terminate_subprocess(proc: Any) -> None:
    """终止一个异步子进程，并在 Windows 上清理完整进程树。

    为什么统一在这里处理：audit、legacy 脚本和 Claude CLI 都可能再派生子进程；
    只终止最外层 ``cmd.exe`` 会让真实工作进程继续写共享工作区，造成 run 已取消
    但文件仍被改写的竞态。

    参数：
        proc: ``asyncio.create_subprocess_exec`` 返回的进程对象。
    返回值：
        无；函数返回时尽力保证目标进程已经退出。
    """
    if proc.returncode is not None:
        return
    if os.name == "nt" and getattr(proc, "pid", None):
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), config.SUBPROCESS_KILL_GRACE_SECONDS)
        except (OSError, asyncio.TimeoutError):
            logger.warning("taskkill failed to clean up the subprocess tree: pid=%s", proc.pid, exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), config.SUBPROCESS_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            pass
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), config.SUBPROCESS_KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            return


async def run_audit(
    params: dict[str, Any],
    write_state: bool = True,
    process_registry: MutableSet[Any] | None = None,
) -> tuple[dict[str, Any] | None, int, str]:
    """异步调用 audit 脚本并解析 research_state。

    功能：
        audit 脚本的 stdout 总是完整 research_state JSON（UTF-8），
        这里整体捕获后解析；失败时返回 stdout 尾部片段便于诊断。
        若调用方传入进程登记表，audit 会纳入所属 run 的取消生命周期。
    参数：
        params: 研究请求参数字典。
        write_state: 是否让脚本写入默认状态文件。
        process_registry: 可选的活动进程集合；用于 run 取消时统一清理。
    返回值：
        三元组 (research_state 或 None, 退出码, stdout 尾部文本)。
    """
    cmd = build_audit_command(params, write_state=write_state)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(config.PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=subprocess_env(),
        )
    except OSError as exc:
        return None, -1, f"Failed to start the audit script: {exc}"
    if process_registry is not None:
        process_registry.add(proc)
    try:
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=config.AUDIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await terminate_subprocess(proc)
            return None, -1, "Audit script timed out"
        except asyncio.CancelledError:
            # 必须先清理进程再把取消继续向上传播；否则外层 finally 移除登记后，
            # run 守护器再也找不到仍在扫描/写状态文件的 audit 子进程。
            await terminate_subprocess(proc)
            raise
        except Exception:
            await terminate_subprocess(proc)
            raise
        text = raw.decode("utf-8", errors="replace")
        state = parse_state_from_stdout(text)
        return state, proc.returncode or 0, text[-2000:]
    finally:
        if process_registry is not None:
            process_registry.discard(proc)


def parse_state_from_stdout(text: str) -> dict[str, Any] | None:
    """从脚本 stdout 提取 research_state JSON。

    功能：
        stdout 理论上是纯 JSON；为了容错（例如前置有告警行），
        从第一个 "{" 开始解析。
    参数：
        text: 完整 stdout 文本。
    返回值：
        research_state 字典；解析失败返回 None。
    """
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def state_file_path(state: dict[str, Any]) -> Path:
    """推导 research_state.json 的默认落盘路径。

    功能：
        与 audit 脚本的 default_state_output_path 规则保持一致：近期历史模式使用
        ``company_state/<code>/<as_of_date>/research_state.json``，单份兼容模式仍使用财年目录。
    参数：
        state: research_state 字典。
    返回值：
        状态文件路径。
    """
    # 路径写入端负责处理空格、路径分隔符和 ``..``。读取端必须复用同一实现，
    # 否则英文公司名会出现“实际写入 bank_of_china、事件却指向 bank of china”的分裂。
    return default_state_output_path(config.PROJECT_ROOT, state)


def load_research_state(path: Path) -> dict[str, Any]:
    """读取 research_state.json。

    功能：
        安全读取状态文件，供历史 run 查看与 catalog 构建复用。
    参数：
        path: 状态文件路径。
    返回值：
        状态字典；不存在或解析失败时返回空字典。
    """
    return load_json_dict(path)


def load_json_dict(path: Path) -> dict[str, Any]:
    """读取 JSON 文件并保证返回字典。

    功能：
        统一容错：文件缺失、编码错误、格式错误都返回空字典，
        因为调用方只关心"有没有可用结构"，不需要区分失败原因。
    参数：
        path: JSON 文件路径。
    返回值：
        字典；异常情况下为空字典。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# catalog 构建
# ---------------------------------------------------------------------------

# manifest 很大（上万条记录），按 (路径, mtime) 缓存 code→公司名映射，避免每次 catalog 都全量解析。
_manifest_name_cache: dict[str, Any] = {"key": None, "names": {}}


def _company_name_map() -> dict[str, str]:
    """从采集 manifest 构建股票代码到公司名的映射。

    功能：
        catalog 只展示少数本地已有产物的公司，但公司名以 manifest 为准；
        以 manifest 的 mtime 作为缓存键，文件未变化时直接复用上次解析结果。
    参数：
        无。
    返回值：
        {stock_code: company_name} 字典；manifest 缺失时为空字典。
    """
    manifest = config.COLLECTOR_WORKSPACE / "manifests" / "cninfo_all_reports.json"
    try:
        key = (str(manifest), manifest.stat().st_mtime)
    except OSError:
        return {}
    if _manifest_name_cache["key"] == key:
        return _manifest_name_cache["names"]
    names: dict[str, str] = {}
    try:
        records = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        records = []
    if isinstance(records, list):
        for record in records:
            if isinstance(record, dict):
                code = str(record.get("stock_code") or "")
                name = str(record.get("company_name") or "")
                if code and name and code not in names:
                    names[code] = name
    _manifest_name_cache["key"] = key
    _manifest_name_cache["names"] = names
    return names


def _name_from_stem(stem: str) -> str:
    """从产物目录名推断公司名。

    功能：
        产物目录名遵循 "<code>-<公司名>-<年报标题>" 约定，
        manifest 查不到时以此兜底。
    参数：
        stem: 目录或文件主干名。
    返回值：
        公司名；解析不出时返回空字符串。
    """
    parts = stem.split("-")
    return parts[1] if len(parts) >= 2 else ""


def build_catalog() -> dict[str, Any]:
    """扫描各工作区，构建公司/年度产物目录。

    功能：
        快速文件存在性扫描（不读取大文件内容），产出：
        - companies：每家公司按年度/报告类型列出六层产物是否存在；
        - states：已有 research_state.json 清单。
        公司集合取自采集 PDF 目录、处理器解析目录、财务分析目录与状态目录的并集，
        而不是整个 manifest（manifest 覆盖全市场，绝大多数没有本地产物）。
    参数：
        无。
    返回值：
        {"companies": [...], "states": [...]} 字典。
    """
    names = _company_name_map()
    # entries[(code, year, type)] = 各层布尔值
    entries: dict[tuple[str, str, str], dict[str, bool]] = {}
    stem_names: dict[str, str] = {}

    def entry(code: str, year: str, rtype: str) -> dict[str, bool]:
        return entries.setdefault(
            (code, year, rtype),
            {
                "collector": False,
                "processor": False,
                "financial_evidence_draft": False,
                "formal_financial_analysis": False,
                "valuation": False,
                "market_context": False,
            },
        )

    # 采集层：reports/<type>/<year>/<code>/*.pdf
    reports_root = config.COLLECTOR_WORKSPACE / "reports"
    if reports_root.exists():
        for pdf in reports_root.glob("*/*/*/*.pdf"):
            code_dir = pdf.parent
            rtype = code_dir.parent.parent.name
            year = code_dir.parent.name
            code = code_dir.name
            entry(code, year, rtype)["collector"] = True
            stem_names.setdefault(code, _name_from_stem(pdf.stem))

    # 处理层：parsed_reports/<type>/<year>/<code>/<stem>/content.json
    parsed_root = config.PROCESSOR_WORKSPACE / "parsed_reports"
    if parsed_root.exists():
        for content in parsed_root.glob("*/*/*/*/content.json"):
            report_dir = content.parent
            code = report_dir.parent.name
            year = report_dir.parent.parent.name
            rtype = report_dir.parent.parent.parent.name
            entry(code, year, rtype)["processor"] = True
            stem_names.setdefault(code, _name_from_stem(report_dir.name))

    # 财务分析层：analyst_workspace/reports/<type>/<year>/<code>/<stem>/
    analyst_root = config.ANALYST_WORKSPACE / "reports"
    if analyst_root.exists():
        for marker, layer in [
            ("analyst_report.json", "financial_evidence_draft"),
            ("formal_financial_analysis.json", "formal_financial_analysis"),
        ]:
            for found in analyst_root.glob(f"*/*/*/*/{marker}"):
                report_dir = found.parent
                code = report_dir.parent.name
                year = report_dir.parent.parent.name
                rtype = report_dir.parent.parent.parent.name
                entry(code, year, rtype)[layer] = True

    # 估值与市场上下文按股票代码组织（目录名是 as_of_date，不区分财报年度），
    # 因此先汇总每个代码是否有产物，再回填到该代码的所有年度行。
    valuation_codes: set[str] = set()
    for pattern in ["reports/*/*/valuation_report.json", "*/*/valuation_report.json"]:
        for found in config.VALUATION_WORKSPACE.glob(pattern):
            valuation_codes.add(found.parent.parent.name)
    market_codes: set[str] = set()
    packages_root = config.MARKET_CONTEXT_WORKSPACE / "packages"
    if packages_root.exists():
        for found in packages_root.glob("*/*/market_context_package.json"):
            market_codes.add(found.parent.parent.name)

    # research_state 清单
    states: list[dict[str, Any]] = []
    latest_state_by_code: dict[str, str] = {}
    state_root = config.ORCHESTRATOR_WORKSPACE / "company_state"
    if state_root.exists():
        for state_file in sorted(state_root.glob("*/*/research_state.json")):
            code = state_file.parent.parent.name
            scope = state_file.parent.name
            payload = load_json_dict(state_file)
            target = payload.get("target", {}) if isinstance(payload.get("target"), dict) else {}
            filing_policy = str(payload.get("filing_policy") or (payload.get("request") or {}).get("filing_policy") or "single_filing")
            states.append(
                {
                    "stock_code": code,
                    "report_year": str(target.get("report_year") or ""),
                    "as_of_date": str(payload.get("knowledge_cutoff") or ""),
                    "filing_policy": filing_policy,
                    "path": str(state_file),
                    "generated_at": str(payload.get("generated_at") or ""),
                }
            )
            # 近期历史状态以生成时间判断最新；旧状态没有生成时间时再使用目录作用域兜底。
            prev = latest_state_by_code.get(code)
            prev_payload = load_json_dict(Path(prev)) if prev else {}
            current_key = (str(payload.get("generated_at") or ""), scope)
            previous_key = (str(prev_payload.get("generated_at") or ""), Path(prev).parent.name if prev else "")
            if prev is None or current_key >= previous_key:
                latest_state_by_code[code] = str(state_file)
            filings = payload.get("filings") if isinstance(payload.get("filings"), list) else []
            if filings:
                for filing in filings:
                    entry(code, str(filing.get("report_year") or ""), str(filing.get("report_type") or "annual"))
            else:
                entry(code, str(target.get("report_year") or scope), str(target.get("report_type") or "annual"))

    companies: dict[str, dict[str, Any]] = {}
    for (code, year, rtype), layers in sorted(entries.items()):
        layers["valuation"] = code in valuation_codes
        layers["market_context"] = code in market_codes
        company = companies.setdefault(
            code,
            {
                "stock_code": code,
                "company_name": names.get(code) or stem_names.get(code, ""),
                "years": [],
            },
        )
        company["years"].append(
            {
                "report_year": year,
                "report_type": rtype,
                "has_pdf": layers["collector"],
                "layers": dict(layers),
            }
        )
    for code, company in companies.items():
        company["years"].sort(key=lambda item: (item["report_year"], item["report_type"]), reverse=True)
        if code in latest_state_by_code:
            company["latest_state_path"] = latest_state_by_code[code]

    return {"companies": list(companies.values()), "states": states}


# ---------------------------------------------------------------------------
# artifact 安全读取
# ---------------------------------------------------------------------------

def is_path_allowed(path_value: str | Path) -> bool:
    """判断路径是否落在 artifact 白名单根内。

    功能：
        相对路径先按项目根拼接，再 resolve() 归一化（消除 .. 与符号差异），
        最后逐一与白名单根做 is_relative_to 比较。纯路径判断，不要求文件存在。
    参数：
        path_value: 绝对或相对路径。
    返回值：
        在白名单内返回 True，否则 False。
    """
    try:
        raw = Path(str(path_value))
        if not raw.is_absolute():
            raw = config.PROJECT_ROOT / raw
        resolved = raw.resolve()
    except (OSError, ValueError):
        return False
    for denied in config.ARTIFACT_DENY_PATHS:
        try:
            if resolved == denied.resolve():
                return False
        except (OSError, ValueError):
            continue
    for root in config.ARTIFACT_WHITELIST_ROOTS:
        try:
            if resolved.is_relative_to(root.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def read_artifact(path_value: str) -> dict[str, Any]:
    """在白名单约束下读取 artifact 文件。

    功能：
        - 白名单外抛 PermissionError（路由层转 403）；
        - 文件缺失抛 FileNotFoundError（路由层转 404）；
        - json 解析为对象返回；md/其他文本原文返回；jsonl 只取前 200 行；
          pdf 只回元信息；超过 2MB 的 json/md 截断并标记 truncated。
    参数：
        path_value: 绝对或相对路径字符串。
    返回值：
        {kind, name, path, size, mtime, content, truncated?} 字典。
    """
    raw = Path(path_value)
    if not raw.is_absolute():
        raw = config.PROJECT_ROOT / raw
    resolved = raw.resolve()
    if not is_path_allowed(resolved):
        raise PermissionError(f"Path is outside the allowlisted workspaces: {resolved}")
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"File not found: {resolved}")

    stat = resolved.stat()
    suffix = resolved.suffix.lower()
    result: dict[str, Any] = {
        "name": resolved.name,
        "path": str(resolved),
        "size": stat.st_size,
        "mtime": mtime_iso(resolved),
        "truncated": False,
    }

    if suffix == ".pdf":
        # PDF 是二进制大文件，只回元信息，前端提示用户在本地打开。
        result.update({"kind": "pdf", "content": None})
        return result

    if suffix == ".jsonl":
        lines: list[Any] = []
        truncated = False
        with resolved.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= config.JSONL_PREVIEW_LINES:
                    truncated = True
                    break
                text = line.strip()
                if not text:
                    continue
                try:
                    lines.append(json.loads(text))
                except json.JSONDecodeError:
                    lines.append({"_raw": text[:2000]})
        result.update({"kind": "jsonl", "content": lines, "truncated": truncated})
        return result

    # 其余按文本处理；超限截断读取，避免一次性载入超大文件。
    truncated = stat.st_size > config.MAX_ARTIFACT_BYTES
    with resolved.open("r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(config.MAX_ARTIFACT_BYTES)
    if suffix == ".json":
        if truncated:
            # 截断后的 JSON 无法解析，返回原文片段并标记，让前端按文本展示。
            result.update({"kind": "json", "content": text, "truncated": True})
        else:
            try:
                result.update({"kind": "json", "content": json.loads(text)})
            except json.JSONDecodeError:
                result.update({"kind": "json", "content": text})
        return result
    kind = "md" if suffix in {".md", ".markdown"} else "text"
    result.update({"kind": kind, "content": text, "truncated": truncated})
    return result


# ---------------------------------------------------------------------------
# 估值报告宽容提取与结论卡组装
# ---------------------------------------------------------------------------

def _as_number(value: Any) -> float | None:
    """把任意值宽容地转成数字。

    功能：
        估值报告字段可能是 int/float/数字字符串，统一转 float；
        其他类型返回 None 表示取不到。
    参数：
        value: 任意值。
    返回值：
        float 或 None。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.replace(",", "").strip().rstrip("%")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _scenario_value(container: dict[str, Any], scenario: str) -> Any:
    """按情景别名从字典中取值。

    功能：
        三档估值的键名可能是 bear/base/bull 或 悲观/基准/乐观 等变体，
        逐一尝试别名命中。
    参数：
        container: 三档估值字典。
        scenario: 标准情景名（bear/base/bull）。
    返回值：
        命中的原始值；取不到返回 None。
    """
    for alias in _SCENARIO_ALIASES[scenario]:
        if alias in container:
            return container[alias]
    # 兜底：键名大小写或前后缀差异（如 bear_case_cny）。
    for key, value in container.items():
        lowered = str(key).lower()
        if any(alias in lowered for alias in _SCENARIO_ALIASES[scenario]):
            return value
    return None


def _fair_value_number(value: Any) -> float | None:
    """从单档估值取每股合理价值点值。

    功能：
        允许三种形态：直接数字；带 fair_value_cny/fair_value/value/point/target 的字典；
        [低, 高] 区间列表（取中值）。字典里若只有 range 键也取区间中值。
    参数：
        value: 单档估值原始值。
    返回值：
        每股合理价值 float；取不到返回 None。
    """
    number = _as_number(value)
    if number is not None:
        return number
    if isinstance(value, dict):
        # 新 agent 产物常用 fair_value_per_share；旧报告用 fair_value_cny/value/point。
        for key in (
            "fair_value_per_share",
            "fair_value_cny",
            "fair_value",
            "value",
            "point",
            "target_price",
            "price",
            "mid",
            "per_share",
        ):
            number = _as_number(value.get(key))
            if number is not None:
                return number
        for key, sub in value.items():
            if "range" in str(key).lower():
                mid = _range_mid(sub)
                if mid is not None:
                    return mid
        # 字典里任何数值字段作为最后兜底。
        for sub in value.values():
            number = _as_number(sub)
            if number is not None:
                return number
    if isinstance(value, (list, tuple)):
        return _range_mid(value)
    return None


def _range_mid(value: Any) -> float | None:
    """取区间列表的中值。

    参数：
        value: 形如 [低, 高] 的列表。
    返回值：
        中值 float；不是有效区间返回 None。
    """
    if isinstance(value, (list, tuple)) and value:
        numbers = [_as_number(item) for item in value]
        numbers = [n for n in numbers if n is not None]
        if numbers:
            return sum(numbers) / len(numbers)
    return None


def _extract_current_price(snapshot: dict[str, Any]) -> tuple[float | None, str]:
    """从 market_snapshot 宽容提取现价。

    功能：
        优先精确键名，再按"包含 price/close 的数值键"兜底
        （真实报告里出现过 intraday_price_cny、reference_close_2026_05_26_cny 等变体）。
    参数：
        snapshot: market_snapshot 字典。
    返回值：
        (现价或 None, price_source 描述)。
    """
    if not isinstance(snapshot, dict):
        return None, "missing"
    preferred = (
        "current_price",
        "intraday_price_cny",
        "current_price_cny",
        "latest_price",
        "price_cny",
        "price",
        "close",
        "last_close",
    )
    for key in preferred:
        number = _as_number(snapshot.get(key))
        if number is not None:
            return number, str(snapshot.get("price_source") or key)
    for key, value in snapshot.items():
        lowered = str(key).lower()
        if ("price" in lowered or "close" in lowered) and "cap" not in lowered:
            number = _as_number(value)
            if number is not None:
                return number, str(snapshot.get("price_source") or key)
    return None, str(snapshot.get("price_source") or "missing")


def _normalize_valuation_view(value: Any) -> tuple[str, str]:
    """把估值观点归一到稳定枚举，并原样保留报告值。

    参数：
        value: valuation_view 原始值。
    返回值：
        ``(normalized, raw)``；无法识别时 normalized 为 ``unknown``。
    """
    raw = str(value or "").strip()
    lowered = raw.lower().replace("-", "_").replace(" ", "_")
    for normalized, aliases in _VALUATION_VIEW_ALIASES.items():
        if lowered in aliases or any(alias in lowered for alias in aliases):
            return normalized, raw
    return "unknown", raw


def _date_only(value: Any) -> str:
    """从 ISO 时间、东方财富时间或 YYYYMMDD 中提取 ``YYYY-MM-DD``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 8 and text[:8].isdigit() and "-" not in text[:10]:
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return _dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def _extract_price_observation(
    snapshot: dict[str, Any],
    current_price: float | None,
    price_source: str,
    as_of_date: str,
) -> tuple[dict[str, Any], Any, str]:
    """提取价格观察日、口径说明与知识截止状态。

    为什么单独保留 cutoff_status：旧报告可能把基准日之后的盘中价格写入估值报告。
    价格仍作为原始事实展示，但下游历史冻结必须知道它是否越过知识截止日，而不能把
    ``price_source`` 文本误当成时点证明。
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    observation_date = ""
    for key in (
        "observation_date",
        "price_observation_date",
        "trade_date",
        "as_of_date",
        "market_date",
        "date",
    ):
        observation_date = _date_only(snapshot.get(key))
        if observation_date:
            break
    if not observation_date:
        # 真实报告常把日期编码进 reference_close_2026_05_26_cny 之类的键名。
        for key in snapshot:
            matched = re.search(r"(20\d{2})[_-](\d{2})[_-](\d{2})", str(key))
            if matched:
                observation_date = _date_only("-".join(matched.groups()))
                if observation_date:
                    break

    price_basis = (
        snapshot.get("price_basis")
        or snapshot.get("date_treatment")
        or snapshot.get("basis_note")
        or snapshot.get("price_source")
        or price_source
    )
    cutoff_date = _date_only(as_of_date)
    if not observation_date or not cutoff_date:
        cutoff_status = "unknown"
    elif observation_date == cutoff_date:
        cutoff_status = "at_cutoff"
    elif observation_date < cutoff_date:
        cutoff_status = "before_cutoff"
    else:
        cutoff_status = "after_cutoff"

    return (
        {
            "status": "available" if current_price is not None else "unavailable",
            "observation_date": observation_date or None,
            "price": current_price,
            "source": price_source if current_price is not None else "missing",
        },
        price_basis,
        cutoff_status,
    )


def _extract_market_cap(snapshot: dict[str, Any]) -> float | None:
    """从 market_snapshot 宽容提取总市值。

    参数：
        snapshot: market_snapshot 字典。
    返回值：
        市值（元）或 None。
    """
    if not isinstance(snapshot, dict):
        return None
    for key in ("market_cap", "total_market_cap_cny", "market_cap_cny", "total_market_cap"):
        number = _as_number(snapshot.get(key))
        if number is not None:
            return number
    for key, value in snapshot.items():
        lowered = str(key).lower()
        # 排除"亿元"口径字段，避免与"元"口径混用导致量级错误。
        if "market_cap" in lowered and "100m" not in lowered and "yi" not in lowered:
            number = _as_number(value)
            if number is not None:
                return number
    return None


def _extract_upside(report: dict[str, Any], fair_value: dict[str, Any], current_price: float | None) -> dict[str, Any]:
    """宽容提取三档上下行空间（相对现价的小数比例）。

    功能：
        优先读 upside_downside_vs_current_price：
        - 直接含 bear/base/bull 数值时直接用；
        - 真实报告可能嵌套（vs_intraday_xx → bear_point_percent），逐层下钻；
        - 数值绝对值大于 1.5 视为百分数并除以 100（12.7 → 0.127）。
        取不到时用三档合理价值与现价反推。
    参数：
        report: 估值报告字典。
        fair_value: 已提取的三档合理价值。
        current_price: 现价。
    返回值：
        {"bear": x, "base": y, "bull": z}，取不到的档位为 None。
    """

    def normalize(value: Any, *, explicit_percent: bool = False) -> float | None:
        number = _as_number(value)
        if number is None:
            return None
        if explicit_percent:
            return number / 100.0
        return number / 100.0 if abs(number) > 1.5 else number

    def scenario_entry(container: dict[str, Any], scenario: str) -> tuple[Any, bool]:
        aliases = _SCENARIO_ALIASES[scenario]
        for key, value in container.items():
            lowered = str(key).lower()
            if str(key) in aliases or any(alias in lowered for alias in aliases):
                return value, "percent" in lowered or "pct" in lowered
        return None, False

    def from_container(container: dict[str, Any]) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for scenario in ("bear", "base", "bull"):
            value, explicit_percent = scenario_entry(container, scenario)
            if isinstance(value, dict):
                if value.get("percent") is not None:
                    value = value.get("percent")
                    explicit_percent = True
                else:
                    value = value.get("point") or _fair_value_number(value)
            number = normalize(value, explicit_percent=explicit_percent)
            if number is not None:
                found[scenario] = number
        return found

    result: dict[str, Any] = {"bear": None, "base": None, "bull": None}
    raw = report.get("upside_downside_vs_current_price") or report.get("upside_downside") or {}
    if isinstance(raw, dict):
        found = from_container(raw)
        if not found:
            # 嵌套形态：{"vs_intraday_36_83": {"bear_point_percent": -11.8, ...}}
            for sub in raw.values():
                if isinstance(sub, dict):
                    inner = {}
                    for scenario in ("bear", "base", "bull"):
                        for key, value in sub.items():
                            lowered = str(key).lower()
                            if scenario in lowered and "range" not in lowered:
                                number = normalize(
                                    value,
                                    explicit_percent="percent" in lowered or "pct" in lowered,
                                )
                                if number is not None:
                                    inner[scenario] = number
                                break
                    if inner:
                        found = inner
                        break
        result.update(found)
    if current_price:
        # 缺档时用合理价值反推，保证结论卡三档尽量完整。
        for scenario in ("bear", "base", "bull"):
            if result[scenario] is None:
                point = fair_value.get(scenario)
                if isinstance(point, (int, float)) and current_price > 0:
                    result[scenario] = round(point / current_price - 1.0, 4)
    return result


def _string_list(value: Any, limit: int = 5) -> list[str]:
    """把 LLM 生成的任意嵌套值稳定规整成可读字符串列表。

    为什么需要递归：估值报告中的假设和证伪条件既可能是字符串数组，也可能是
    ``{类别: 描述}``、对象数组或 ``{condition, revision_action}``。直接 ``str(dict)``
    不仅难读，还会让顶层对象被静默丢弃。

    参数：
        value: 字符串、标量、列表、元组或嵌套字典。
        limit: 最多保留条数。
    返回值：
        去空、去重且保持输入顺序的字符串列表。
    """
    result: list[str] = []
    seen: set[str] = set()
    preferred_labels = {
        "assumption": "Assumption",
        "description": "Description",
        "text": "Text",
        "trigger": "Trigger",
        "condition": "Condition",
        "revision_action": "Revision Action",
        "action": "Action",
    }

    def add(text: Any, label: str = "") -> None:
        if len(result) >= limit:
            return
        normalized = str(text).strip()
        if not normalized:
            return
        item = f"{label}: {normalized}" if label else normalized
        if item not in seen:
            seen.add(item)
            result.append(item)

    def walk(node: Any, label: str = "") -> None:
        if node is None or len(result) >= limit:
            return
        if isinstance(node, str):
            add(node, label)
            return
        if isinstance(node, (int, float, bool)):
            add(node, label)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item, label)
                if len(result) >= limit:
                    break
            return
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key).strip()
                child_label = preferred_labels.get(key_text.lower(), key_text)
                # 字典键往往携带“收入增速/库存/下修”等类别信息，保留它比只取值
                # 更利于结论卡理解；嵌套容器继续递归展开。
                walk(item, child_label)
                if len(result) >= limit:
                    break
            return
        add(node, label)

    walk(value)
    return result


def extract_valuation_summary(report: dict[str, Any], as_of_date: str = "") -> dict[str, Any]:
    """从估值报告宽容提取结论卡字段。

    功能：
        对 valuation_report.json 的关键字段做宽容映射（中英文键名变体、
        数字/字典/区间三种取值形态），取不到的字段置 None，绝不抛异常；
        估值观点输出稳定枚举并保留原始值，同时明确价格观察日和截止状态。
        同时兼容旧 /rec 产物与 python_agent_coordinator 新 agent 产物：
        ``fair_value_scenarios`` / ``price_context`` / ``executive_summary.thesis`` 等。
    参数：
        report: valuation_report.json 解析后的字典。
        as_of_date: run 的知识截止日；旧调用方可不传。
    返回值：
        含 valuation_view/valuation_view_raw/price_observation/price_basis/
        cutoff_status 及既有结论卡字段的字典。
    """
    if not isinstance(report, dict):
        report = {}
    executive = report.get("executive_summary") if isinstance(report.get("executive_summary"), dict) else {}
    # 现价：旧 market_snapshot；新 agent 常用 price_context / current_market_snapshot。
    snapshot_candidates = [
        report.get("market_snapshot"),
        report.get("current_market_snapshot"),
        report.get("price_context"),
        report.get("market_price_snapshot"),
    ]
    snapshot: dict[str, Any] = {}
    current_price: float | None = None
    price_source = "missing"
    for candidate in snapshot_candidates:
        if not isinstance(candidate, dict):
            continue
        if not snapshot:
            snapshot = candidate
        price, source = _extract_current_price(candidate)
        if price is not None:
            snapshot = candidate
            current_price = price
            price_source = source
            break
    if current_price is None:
        # 顶层或 executive 里偶发直接给 current_price。
        for container in (report, executive):
            if not isinstance(container, dict):
                continue
            price = _as_number(container.get("current_price"))
            if price is not None:
                current_price = price
                price_source = str(container.get("price_source") or "report.current_price")
                break
    market_cap = _extract_market_cap(snapshot)
    if market_cap is None:
        market_cap = _as_number(snapshot.get("market_cap_proxy_cny")) or _as_number(
            snapshot.get("market_cap")
        )

    raw_view_value = report.get("valuation_view") or executive.get("valuation_view")
    normalized_view, raw_view = _normalize_valuation_view(raw_view_value)
    price_observation, price_basis, cutoff_status = _extract_price_observation(
        snapshot,
        current_price,
        price_source,
        as_of_date or str(report.get("as_of_date") or ""),
    )

    # 三档合理价值容器：新旧键名一并尝试。
    fair_container: Any = {}
    for key in (
        "fair_value_scenarios",
        "scenario_fair_values",
        "scenario_analysis",
        "fair_value_range_per_share",
        "fair_value_per_share",
        "fair_value_range",
        "fair_value",
        "overall_fair_value_range_per_share",
    ):
        candidate = report.get(key)
        if isinstance(candidate, dict) and candidate:
            fair_container = candidate
            break
    fair_value: dict[str, Any] = {"bear": None, "base": None, "bull": None, "unit": "CNY/share"}
    if isinstance(fair_container, dict):
        for scenario in ("bear", "base", "bull"):
            fair_value[scenario] = _fair_value_number(_scenario_value(fair_container, scenario))
    if fair_value["base"] is None:
        # base 目标价：数字 / {value: n} / executive_summary.base_target_price。
        for raw in (
            report.get("base_case_target_price"),
            report.get("base_target_price"),
            executive.get("base_target_price"),
        ):
            number = _fair_value_number(raw)
            if number is not None:
                fair_value["base"] = number
                break

    # 若报告未写 valuation_view，用现价相对基准合理价值推断，避免卡片 Unknown。
    if normalized_view == "unknown" and current_price and fair_value.get("base"):
        base_fv = float(fair_value["base"])
        if base_fv > 0:
            ratio = current_price / base_fv
            if ratio <= 0.90:
                normalized_view, raw_view = "undervalued", "inferred_from_price_vs_base_fv"
            elif ratio >= 1.10:
                normalized_view, raw_view = "overvalued", "inferred_from_price_vs_base_fv"
            else:
                normalized_view, raw_view = "fair", "inferred_from_price_vs_base_fv"

    confidence = report.get("confidence")
    if isinstance(confidence, dict):
        confidence = confidence.get("level") or confidence.get("overall") or confidence.get("value")
    confidence_text = str(confidence).strip() if confidence else None

    one_line = (
        report.get("one_sentence_conclusion")
        or report.get("one_line_conclusion")
        or report.get("conclusion")
        or executive.get("thesis")
        or executive.get("one_sentence_conclusion")
        or executive.get("summary")
        or ""
    )

    # upside：优先报告显式字段，再回退到 executive_summary 百分比。
    upside = _extract_upside(report, fair_value, current_price)
    if all(upside.get(name) is None for name in ("bear", "base", "bull")):
        exec_upside = executive.get("upside_downside_vs_current_price")
        if isinstance(exec_upside, dict):
            for scenario, alias in (
                ("bear", "bear_pct"),
                ("base", "base_pct"),
                ("bull", "bull_pct"),
            ):
                number = _as_number(exec_upside.get(alias) or exec_upside.get(scenario))
                if number is not None:
                    # 报告若给的是百分比点数（44.5），统一成小数 0.445。
                    upside[scenario] = number / 100.0 if abs(number) > 2 else number

    return {
        "valuation_view": normalized_view,
        "valuation_view_raw": raw_view,
        "one_line_conclusion": str(one_line),
        "current_price": current_price,
        "market_cap": market_cap,
        "price_source": price_source if current_price is not None else "missing",
        "price_observation": price_observation,
        "price_basis": price_basis,
        "cutoff_status": cutoff_status,
        "fair_value": fair_value,
        "upside_downside": upside,
        "key_assumptions": _string_list(
            report.get("key_assumptions") or executive.get("key_assumptions"),
            limit=5,
        ),
        # 新旧报告字段并存时合并后去重，输出仍沿用冻结的 valuation_falsifiers 契约。
        "valuation_falsifiers": _string_list(
            [
                report.get("valuation_falsifiers"),
                report.get("valuation_falsifiers_and_revision_triggers"),
            ],
            limit=6,
        ),
        "status": str(report.get("status") or report.get("valuation_status") or ""),
        "confidence": confidence_text,
    }


def extract_market_context_summary(package: dict[str, Any]) -> dict[str, Any]:
    """从市场上下文包提取结论卡的市场上下文小节。

    参数：
        package: market_context_package.json 解析后的字典。
    返回值：
        {status, source_count, tier_counts, max_confidence} 字典。
    """
    if not isinstance(package, dict):
        package = {}
    gate = package.get("quality_gate") or {}
    tier_counts = gate.get("source_tier_counts") or {}
    if not isinstance(tier_counts, dict):
        tier_counts = {}
    source_table = package.get("source_table")
    if isinstance(source_table, list):
        source_count = len(source_table)
    else:
        source_count = sum(v for v in tier_counts.values() if isinstance(v, (int, float)))
    return {
        "status": str(package.get("status") or ""),
        "source_count": int(source_count),
        "tier_counts": {tier: int(tier_counts.get(tier, 0) or 0) for tier in ("S", "A", "B", "C")},
        "max_confidence": str(gate.get("max_confidence") or ""),
    }


def build_company_summary(
    state: dict[str, Any] | None,
    valuation_report: dict[str, Any] | None,
    formal_analysis: dict[str, Any] | None,
    market_package: dict[str, Any] | None,
) -> dict[str, Any]:
    """组装公司研究结论卡（run_completed.payload.summary）。

    功能：
        以估值报告为主、正式财务分析与市场上下文包为辅，尽力提取结论卡字段；
        字段缺失时置 None/空，不阻塞交付。
    参数：
        state: 终局 research_state（可为 None）。
        valuation_report: valuation_report.json 内容。
        formal_analysis: formal_financial_analysis.json 内容。
        market_package: market_context_package.json 内容。
    返回值：
        契约定义的 summary 字典。
    """
    state = state or {}
    target = state.get("target", {}) if isinstance(state, dict) else {}
    layers = state.get("layers", {}) if isinstance(state, dict) else {}
    summary_block = state.get("summary", {}) if isinstance(state, dict) else {}

    request = state.get("request", {}) if isinstance(state.get("request"), dict) else {}
    valuation_payload = valuation_report if isinstance(valuation_report, dict) else {}
    valuation_market_snapshot = (
        valuation_payload.get("market_snapshot")
        if isinstance(valuation_payload.get("market_snapshot"), dict)
        else {}
    )
    as_of_date = _date_only(
        request.get("as_of_date")
        or target.get("as_of_date")
        or valuation_payload.get("as_of_date")
        or valuation_market_snapshot.get("as_of_date")
        or valuation_market_snapshot.get("observation_date")
    )
    valuation = extract_valuation_summary(valuation_report or {}, as_of_date=as_of_date)
    one_line = valuation["one_line_conclusion"]
    if not one_line and isinstance(formal_analysis, dict):
        one_line = str(
            formal_analysis.get("one_line_conclusion")
            or formal_analysis.get("one_sentence_conclusion")
            or formal_analysis.get("conclusion")
            or ""
        )

    def artifact_path(layer: str, key: str) -> str:
        info = layers.get(layer, {}).get("artifacts", {}).get(key, {})
        return str(info.get("path") or "") if isinstance(info, dict) and info.get("exists") else ""

    gaps: list[str] = []
    layer_statuses = summary_block.get("layer_statuses") or {
        name: str(layer.get("status") or "") for name, layer in layers.items() if isinstance(layer, dict)
    }
    for name, status in layer_statuses.items():
        if status and status != "ready":
            gaps.append(f"{name} layer status is {status}")
    if valuation["current_price"] is None:
        gaps.append("Current-price snapshot is missing; interpret the valuation using substitute anchors")
    if valuation["cutoff_status"] == "after_cutoff":
        gaps.append("The price observation date is later than the knowledge cutoff; historical freezing requires a downgraded interpretation")

    confidence = valuation["confidence"] or ("low" if gaps else "medium")

    return {
        "company_name": str(target.get("company_name") or ""),
        "stock_code": str(target.get("stock_code") or ""),
        "report_year": str(target.get("report_year") or ""),
        "as_of_date": as_of_date,
        "valuation_view": valuation["valuation_view"],
        "valuation_view_raw": valuation["valuation_view_raw"],
        "one_line_conclusion": one_line,
        "current_price": valuation["current_price"],
        "market_cap": valuation["market_cap"],
        "price_source": valuation["price_source"],
        "price_observation": valuation["price_observation"],
        "price_basis": valuation["price_basis"],
        "cutoff_status": valuation["cutoff_status"],
        "fair_value": valuation["fair_value"],
        "upside_downside": valuation["upside_downside"],
        "key_assumptions": valuation["key_assumptions"],
        "valuation_falsifiers": valuation["valuation_falsifiers"],
        "market_context": extract_market_context_summary(market_package or {}),
        "layer_statuses": layer_statuses,
        "artifact_paths": {
            "valuation_report_md": artifact_path("valuation", "valuation_report_md"),
            "formal_financial_analysis_md": artifact_path("formal_financial_analysis", "formal_financial_analysis_md"),
            "market_context_package_md": artifact_path("market_context", "market_context_package_md"),
        },
        "confidence": confidence,
        "gaps": gaps[:8],
    }
