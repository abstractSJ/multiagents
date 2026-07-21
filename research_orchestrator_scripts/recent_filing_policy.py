"""公司研究的近期财报集合策略。

本模块只负责确定性规划与身份计算，不读取工作区、不发起网络请求，也不做研究判断。
这样既便于审计器和 research console 复用，也能通过固定日期进行稳定单元测试。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable


VALID_FILING_POLICIES = {"recent_history", "single_filing"}
SUPPORTED_REPORT_TYPES = ("annual", "q1", "semiannual", "q3")
INTERIM_REPORT_TYPES = ("q1", "semiannual", "q3")
PERIOD_MONTHS = {"annual": 12, "q1": 3, "semiannual": 6, "q3": 9}


@dataclass(frozen=True)
class FilingPlanItem:
    """单个候选财报采集窗口。

    参数：
        report_type: annual/q1/semiannual/q3。
        report_year: 财报所属年度。
        disclosure_start: 巨潮查询使用的披露日起点。
        disclosure_end: 巨潮查询使用的披露日终点，绝不晚于知识截止日。
        role: 该候选在多期研究中的用途。
        required_if_available: 命中正式披露记录后是否纳入研究集合。
        period_months: 利润表和现金流量表的累计月份，用于防止跨期直接相加。
    """

    report_type: str
    report_year: str
    disclosure_start: str
    disclosure_end: str
    role: str
    required_if_available: bool = True
    expected_by_cutoff: bool = True
    period_months: int = 12

    def to_dict(self) -> dict[str, Any]:
        """返回可直接写入 JSON 的字典。"""
        return asdict(self)


def parse_cutoff(value: str | date) -> date:
    """严格解析知识截止日。

    为什么严格限制十位日期：财报是否可见由截止日直接决定，宽松日期解析可能让
    历史回测错误纳入未来披露，因此这里不接受时间戳或省略前导零的格式。
    """
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"as_of_date must use strict YYYY-MM-DD format; received: {text or '<empty>'}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"as_of_date is not a valid date: {text}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"as_of_date must use strict YYYY-MM-DD format; received: {text}")
    return parsed


def normalize_filing_policy(
    filing_policy: str | None,
    *,
    report_type: str = "",
    report_year: str = "",
) -> str:
    """确定本次请求使用单份模式还是近期历史模式。

    显式同时指定报告类型和财年代表用户要研究一份确定财报，此时默认保持旧行为；
    其余公司研究默认使用近期历史集合，避免“默认 annual”再次遮蔽更新的一季报。
    """
    raw = str(filing_policy or "").strip().lower()
    if raw:
        if raw not in VALID_FILING_POLICIES:
            allowed = ", ".join(sorted(VALID_FILING_POLICIES))
            raise ValueError(f"filing_policy must be one of: {allowed}")
        return raw
    if str(report_type or "").strip() or str(report_year or "").strip():
        # 任一显式单份筛选都沿用旧语义：只给财年时默认 annual，只给类型时选该类型最新财年。
        return "single_filing"
    return "recent_history"


def natural_disclosure_window(report_type: str, report_year: int) -> tuple[date, date]:
    """返回某财报所属期对应的自然披露查询窗口。"""
    if report_type == "annual":
        return date(report_year + 1, 1, 1), date(report_year + 1, 8, 31)
    if report_type == "q1":
        return date(report_year, 4, 1), date(report_year, 8, 31)
    if report_type == "semiannual":
        return date(report_year, 7, 1), date(report_year, 12, 31)
    if report_type == "q3":
        return date(report_year, 10, 1), date(report_year, 12, 31)
    raise ValueError(f"Unsupported report_type: {report_type}")


def statutory_due_date(report_type: str, report_year: int) -> date:
    """返回常规法定披露截止日，用于区分“尚未披露”与“应当已经可得”。"""
    if report_type == "annual":
        return date(report_year + 1, 4, 30)
    if report_type == "q1":
        return date(report_year, 4, 30)
    if report_type == "semiannual":
        return date(report_year, 8, 31)
    if report_type == "q3":
        return date(report_year, 10, 31)
    raise ValueError(f"Unsupported report_type: {report_type}")


def derive_recent_filing_plan(
    as_of_date: str | date,
    *,
    annual_lookback: int = 2,
) -> list[FilingPlanItem]:
    """生成近期财报集合的候选采集计划。

    策略：
    - 年报查询 ``annual_lookback + 1`` 个候选年度；多出来的一年用于年初尚未披露
      上一年度年报时，仍能从实际 manifest 中选出两个完整年报基线。
    - 中报查询当前年度和上一年度的 q1/半年报/q3；只有自然披露窗口已经开始的
      候选才进入计划，绝不把未来窗口压缩成某个伪造的一日查询。
    - 本函数只生成“可能需要查询”的窗口，最终是否可用必须由 manifest 中截止日前
      的正式披露记录决定。
    """
    cutoff = parse_cutoff(as_of_date)
    if isinstance(annual_lookback, bool) or not isinstance(annual_lookback, int):
        raise ValueError("annual_lookback must be an integer")
    if annual_lookback < 1 or annual_lookback > 5:
        raise ValueError("annual_lookback must be between 1 and 5")

    items: list[FilingPlanItem] = []
    current_year = cutoff.year

    # 多查一个候选年不等于多纳入一个年报；审计器最终只选择 annual_lookback 份。
    for offset in range(1, annual_lookback + 2):
        report_year = current_year - offset
        start, natural_end = natural_disclosure_window("annual", report_year)
        if start > cutoff:
            continue
        items.append(
            FilingPlanItem(
                report_type="annual",
                report_year=str(report_year),
                disclosure_start=start.isoformat(),
                disclosure_end=min(natural_end, cutoff).isoformat(),
                role="annual_baseline_candidate",
                expected_by_cutoff=cutoff >= statutory_due_date("annual", report_year),
                period_months=PERIOD_MONTHS["annual"],
            )
        )

    # 先放上一年度，再放当前年度；最终输出会按财年和期间长度排序，顺序本身不承载优先级。
    for report_year in (current_year - 1, current_year):
        for report_type in INTERIM_REPORT_TYPES:
            start, natural_end = natural_disclosure_window(report_type, report_year)
            if start > cutoff:
                continue
            role = "prior_year_interim" if report_year == current_year - 1 else "current_year_interim"
            items.append(
                FilingPlanItem(
                    report_type=report_type,
                    report_year=str(report_year),
                    disclosure_start=start.isoformat(),
                    disclosure_end=min(natural_end, cutoff).isoformat(),
                    role=role,
                    expected_by_cutoff=cutoff >= statutory_due_date(report_type, report_year),
                    period_months=PERIOD_MONTHS[report_type],
                )
            )

    return sorted(items, key=filing_plan_sort_key)


def filing_plan_sort_key(item: FilingPlanItem | dict[str, Any]) -> tuple[int, int, str]:
    """按财年、累计月份和类型稳定排序候选财报。"""
    if isinstance(item, FilingPlanItem):
        report_year = item.report_year
        period_months = item.period_months
        report_type = item.report_type
    else:
        report_year = str(item.get("report_year") or "0")
        report_type = str(item.get("report_type") or "")
        period_months = int(item.get("period_months") or PERIOD_MONTHS.get(report_type, 0))
    return int(report_year or 0), period_months, report_type


def build_filing_identity(record: dict[str, Any], *, pdf_sha256: str = "") -> dict[str, str]:
    """从 manifest 记录构造可审计的稳定财报身份。"""
    return {
        "stock_code": str(record.get("stock_code") or ""),
        "report_type": str(record.get("report_type") or ""),
        "report_year": str(record.get("report_year") or ""),
        "announcement_id": str(record.get("announcement_id") or ""),
        "published_at": str(record.get("published_at") or ""),
        "local_relative_path": str(record.get("local_relative_path") or ""),
        "pdf_sha256": str(pdf_sha256 or record.get("pdf_sha256") or ""),
    }


def filing_id(identity: dict[str, Any]) -> str:
    """把财报身份编码成适合日志和状态引用的稳定短 ID。"""
    components = [
        str(identity.get("stock_code") or "unknown"),
        str(identity.get("report_type") or "unknown"),
        str(identity.get("report_year") or "unknown"),
        str(identity.get("announcement_id") or identity.get("published_at") or "unversioned"),
    ]
    return ":".join(components)


def calculate_financial_input_fingerprint(identities: Iterable[dict[str, Any]]) -> str:
    """计算有序财报集合指纹，用于阻止财务分析和估值错误复用。"""
    normalized = [build_filing_identity(dict(identity), pdf_sha256=str(identity.get("pdf_sha256") or "")) for identity in identities]
    normalized.sort(
        key=lambda item: (
            int(item.get("report_year") or 0),
            PERIOD_MONTHS.get(item.get("report_type") or "", 0),
            item.get("announcement_id") or "",
            item.get("published_at") or "",
            item.get("local_relative_path") or "",
        )
    )
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
