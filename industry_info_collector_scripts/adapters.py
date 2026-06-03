"""行业信息收集员2的本地数据适配器。

本模块只负责读取用户或上游系统提供的本地 JSON/CSV 文件，并把不同来源
标准化为统一记录。它不做网络请求、不读取凭证、不形成分析结论。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AdapterResult:
    """本地适配器读取结果。

    Args:
        source_type: 标准化来源类型。
        source_path: 输入文件路径。
        records: 标准化记录列表。
        warnings: 读取或字段规范化过程中的警告。
        metadata: 文件级元数据。
    """

    source_type: str
    source_path: Path | None
    records: list[dict[str, Any]]
    warnings: list[str]
    metadata: dict[str, Any]


class LocalFileAdapter:
    """本地 JSON/CSV 文件适配器基类。"""

    source_type = "local_file"
    json_record_keys: tuple[str, ...] = ()
    id_field = "record_id"

    def load(self, path: Path | None) -> AdapterResult:
        """读取本地文件并返回标准化记录。

        Args:
            path: JSON 或 CSV 文件路径。

        Returns:
            适配器结果。文件未提供或不存在时返回空记录和警告。
        """

        if path is None:
            return AdapterResult(self.source_type, None, [], ["未提供输入文件。"], {})
        if not path.exists():
            return AdapterResult(self.source_type, path, [], ["输入文件不存在。"], {})
        suffix = path.suffix.lower()
        if suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            records, metadata = self._records_from_json(payload)
        elif suffix == ".csv":
            records, metadata = self._records_from_csv(path)
        else:
            return AdapterResult(self.source_type, path, [], [f"不支持的文件类型：{suffix}"], {})
        normalized = [self.normalize_record(record, index) for index, record in enumerate(records, start=1)]
        return AdapterResult(self.source_type, path, normalized, [], metadata)

    def normalize_record(self, record: dict[str, Any], index: int) -> dict[str, Any]:
        """标准化单条记录，子类可覆盖字段映射。"""

        normalized = dict(record)
        normalized.setdefault("record_id", normalized.get(self.id_field) or f"{self.source_type}-{index:04d}")
        normalized.setdefault("source_type", self.source_type)
        normalized.setdefault("reliability", "medium")
        normalized.setdefault("limitations", [])
        normalized["limitations"] = self._normalize_list(normalized.get("limitations"))
        normalized.setdefault("tags", [])
        normalized["tags"] = self._normalize_list(normalized.get("tags"))
        return normalized

    def _records_from_json(self, payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """从 JSON payload 中抽取记录列表和文件元数据。"""

        if isinstance(payload, list):
            return payload, {}
        if not isinstance(payload, dict):
            return [], {}
        metadata = {key: value for key, value in payload.items() if key not in self.json_record_keys}
        for key in self.json_record_keys:
            records = payload.get(key)
            if isinstance(records, list):
                return records, metadata
        return [], metadata

    def _records_from_csv(self, path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """从 CSV 文件中读取记录。"""

        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            return [dict(row) for row in reader], {"source_name": path.stem}

    def _normalize_list(self, value: Any) -> list[Any]:
        """把字符串/空值/list 统一成 list。"""

        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            separators = ["|", ";", "；", ",", "，"]
            for separator in separators:
                if separator in value:
                    return [item.strip() for item in value.split(separator) if item.strip()]
            return [value.strip()] if value.strip() else []
        return [value]


class CompanyEventsFileAdapter(LocalFileAdapter):
    """公司事件、公告、投资者关系等本地文件适配器。"""

    source_type = "local_company_events_file"
    json_record_keys = ("events", "records")
    id_field = "event_id"

    def normalize_record(self, record: dict[str, Any], index: int) -> dict[str, Any]:
        normalized = super().normalize_record(record, index)
        normalized.setdefault("event_id", normalized["record_id"])
        normalized.setdefault("entity_scope", "company")
        normalized.setdefault("event_type", "other")
        normalized.setdefault("title", "未命名公司事件")
        normalized.setdefault("summary", "")
        return normalized


class PolicyRegulationFileAdapter(LocalFileAdapter):
    """政策和监管文件本地适配器。"""

    source_type = "local_policy_regulation_file"
    json_record_keys = ("policies", "records")
    id_field = "policy_id"

    def normalize_record(self, record: dict[str, Any], index: int) -> dict[str, Any]:
        normalized = super().normalize_record(record, index)
        normalized.setdefault("policy_id", normalized["record_id"])
        normalized.setdefault("entity_scope", "policy")
        normalized.setdefault("title", "未命名政策监管记录")
        normalized.setdefault("impact_areas", [])
        normalized["impact_areas"] = self._normalize_list(normalized.get("impact_areas"))
        return normalized


class IndustryPublicStatsFileAdapter(LocalFileAdapter):
    """行业公开统计数据本地适配器。"""

    source_type = "local_industry_public_stats_file"
    json_record_keys = ("stats", "records")
    id_field = "stat_id"

    def normalize_record(self, record: dict[str, Any], index: int) -> dict[str, Any]:
        normalized = super().normalize_record(record, index)
        normalized.setdefault("stat_id", normalized["record_id"])
        normalized.setdefault("entity_scope", "industry")
        normalized.setdefault("metric_name", "未命名行业统计指标")
        normalized.setdefault("frequency", "unknown")
        return normalized


class IndustrySignalFileAdapter(LocalFileAdapter):
    """行业信号本地适配器。"""

    source_type = "local_industry_signals_file"
    json_record_keys = ("signals", "records")
    id_field = "signal_id"

    def normalize_record(self, record: dict[str, Any], index: int) -> dict[str, Any]:
        normalized = super().normalize_record(record, index)
        normalized.setdefault("signal_id", normalized["record_id"])
        normalized.setdefault("entity_scope", "industry")
        normalized.setdefault("signal_type", "other")
        normalized.setdefault("direction", "unknown")
        normalized.setdefault("summary", "")
        return normalized


class MarketValuationFileAdapter(LocalFileAdapter):
    """行情估值快照本地适配器。

    该适配器用于承接用户稳定来源导出的 JSON/CSV 文件，当前不绑定任何外部 provider。
    """

    source_type = "local_market_valuation_file"
    json_record_keys = ("snapshots", "records")
    id_field = "snapshot_id"

    def normalize_record(self, record: dict[str, Any], index: int) -> dict[str, Any]:
        normalized = super().normalize_record(record, index)
        normalized.setdefault("snapshot_id", normalized["record_id"])
        normalized.setdefault("entity_scope", "market")
        normalized.setdefault("currency", "CNY")
        normalized.setdefault("source_name", "user_supplied")
        return normalized


def filter_records(
    records: list[dict[str, Any]],
    stock_code: str | None = None,
    industry: str | None = None,
    as_of_date: str | None = None,
    include_unscoped: bool = True,
) -> list[dict[str, Any]]:
    """按公司、行业和日期宽松过滤适配器记录。

    Args:
        records: 标准化记录。
        stock_code: 目标股票代码。
        industry: 目标行业。
        as_of_date: 目标日期。
        include_unscoped: 记录未声明公司/行业时是否保留。

    Returns:
        过滤后的记录列表。
    """

    filtered = []
    for record in records:
        record_stock = str(record.get("stock_code") or "").strip()
        record_industry = str(record.get("industry") or "").strip()
        if stock_code and record_stock and record_stock != str(stock_code):
            continue
        if stock_code and not record_stock and not include_unscoped:
            continue
        if industry and record_industry and record_industry != str(industry):
            continue
        if industry and not record_industry and not include_unscoped:
            continue
        if as_of_date and record.get("as_of_date") and str(record["as_of_date"]) > as_of_date:
            continue
        if as_of_date and record.get("date") and str(record["date"]) > as_of_date:
            continue
        filtered.append(record)
    return filtered


def select_market_snapshot(records: list[dict[str, Any]], stock_code: str, as_of_date: str) -> dict[str, Any] | None:
    """从行情估值记录中选择目标公司在 as_of_date 前后的最新快照。"""

    candidates = [record for record in records if str(record.get("stock_code") or "") == str(stock_code)]
    if not candidates:
        return None
    exact = [record for record in candidates if str(record.get("as_of_date") or "") == as_of_date]
    if exact:
        return exact[-1]
    dated = [record for record in candidates if record.get("as_of_date") and str(record["as_of_date"]) <= as_of_date]
    if dated:
        return sorted(dated, key=lambda item: str(item.get("as_of_date")))[-1]
    return candidates[-1]
