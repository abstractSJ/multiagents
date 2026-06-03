"""海外上市公司公开源输入包构建器。

本模块把 SEC submissions、companyfacts 和 filing manifest 转成下游研究可消费的
company_input_package，并保留每个归一化财务数字的来源概念与来源 URL。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


@dataclass(frozen=True)
class ConceptCandidate:
    """候选 XBRL 概念。

    Args:
        taxonomy: XBRL taxonomy 名称，通常是 us-gaap。
        concept: XBRL 概念名。
        preferred_unit: 优先使用的单位。
        confidence: 该概念对目标指标的匹配置信度。
    """

    taxonomy: str
    concept: str
    preferred_unit: str
    confidence: str = "medium"


METRIC_CONCEPTS: dict[str, list[ConceptCandidate]] = {
    "revenue": [
        ConceptCandidate("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD", "high"),
        ConceptCandidate("us-gaap", "Revenues", "USD", "medium"),
        ConceptCandidate("us-gaap", "SalesRevenueNet", "USD", "medium"),
    ],
    "gross_profit": [ConceptCandidate("us-gaap", "GrossProfit", "USD", "high")],
    "operating_income": [ConceptCandidate("us-gaap", "OperatingIncomeLoss", "USD", "high")],
    "net_income": [
        ConceptCandidate("us-gaap", "NetIncomeLoss", "USD", "high"),
        ConceptCandidate("us-gaap", "ProfitLoss", "USD", "medium"),
    ],
    "diluted_eps": [ConceptCandidate("us-gaap", "EarningsPerShareDiluted", "USD/shares", "high")],
    "assets": [ConceptCandidate("us-gaap", "Assets", "USD", "high")],
    "liabilities": [ConceptCandidate("us-gaap", "Liabilities", "USD", "high")],
    "equity": [
        ConceptCandidate("us-gaap", "StockholdersEquity", "USD", "high"),
        ConceptCandidate("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD", "medium"),
    ],
    "operating_cash_flow": [ConceptCandidate("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD", "high")],
    "capex": [
        ConceptCandidate("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "USD", "high"),
        ConceptCandidate("us-gaap", "PaymentsToAcquireProductiveAssets", "USD", "medium"),
    ],
    "cash_and_equivalents": [
        ConceptCandidate("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD", "high"),
        ConceptCandidate("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD", "medium"),
    ],
    "debt": [
        ConceptCandidate("us-gaap", "LongTermDebt", "USD", "medium"),
        ConceptCandidate("us-gaap", "LongTermDebtAndFinanceLeaseObligations", "USD", "medium"),
        ConceptCandidate("us-gaap", "LongTermDebtNoncurrent", "USD", "low"),
        ConceptCandidate("us-gaap", "ShortTermBorrowings", "USD", "low"),
    ],
    "diluted_shares": [ConceptCandidate("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "high")],
}


class PublicMarketPackageBuilder:
    """公开市场公司研究输入包构建器。

    Args:
        target_dir: 单家公司工作区，例如 research_workspace/MU。
    """

    def __init__(self, target_dir: str | Path) -> None:
        self.target_dir = Path(target_dir)

    def build(self, ticker: str, company_name: str, cik: str, as_of_date: str, market: str = "NASDAQ") -> dict[str, Any]:
        """构建 company_input_package 及配套产物。

        Args:
            ticker: 股票代码。
            company_name: 公司名称。
            cik: 十位 CIK。
            as_of_date: 信息包日期。
            market: 上市市场。

        Returns:
            构建摘要。
        """

        source_manifest = self._load_json(self.target_dir / "source_manifest.json", default=[])
        filing_manifest = self._load_json(self.target_dir / "filing_manifest.json", default=[])
        collection_audit = self._load_json(self.target_dir / "collection_audit.json", default={})
        market_snapshot = self._load_json(self.target_dir / "market_snapshot.json", default={})
        companyfacts_path = self.target_dir / "raw" / "sec" / f"companyfacts_CIK{cik}.json"
        companyfacts = self._load_json(companyfacts_path, default={})

        normalized_financials = self._normalize_financials(
            companyfacts=companyfacts,
            ticker=ticker,
            company_name=company_name,
            cik=cik,
        )
        market_data = self._build_market_data_summary(market_snapshot)
        evidence_table = self._build_evidence_table(source_manifest, filing_manifest, normalized_financials, market_data)
        known_gaps = self._build_known_gaps(normalized_financials, market_data)
        primary_sources = ["SEC EDGAR submissions", "SEC companyfacts XBRL"]
        if market_data.get("status") in {"available", "needs_review"}:
            primary_sources.append("public webpage market quote cross-check")
        package = {
            "schema_version": "1.0",
            "generated_at": self._now_iso(),
            "target": {
                "ticker": ticker,
                "company_name": company_name,
                "market": market,
                "cik": cik,
                "sec_entity_name": companyfacts.get("entityName"),
            },
            "as_of_date": as_of_date,
            "source_policy": {
                "paid_terminals_used": False,
                "primary_sources": primary_sources,
                "optional_sources": ["Company IR public pages", "licensed exchange or broker market data"],
                "excluded_sources": ["Bloomberg", "FactSet", "Wind", "Capital IQ", "Refinitiv paid terminal"],
            },
            "filings": filing_manifest,
            "financials": normalized_financials,
            "market_data": market_data,
            "business_summary": {
                "status": "not_extracted_in_v1",
                "note": "v1 已获取 SEC filing 与 XBRL 财务事实；经营叙事、产品线和风险因素需要继续读取 10-K/10-Q 正文。",
            },
            "risk_factors": {
                "status": "requires_filing_text_review",
                "note": "风险因素来自 filing 正文，不应仅凭 XBRL companyfacts 自动推断。",
            },
            "evidence_table": evidence_table,
            "known_gaps": known_gaps,
            "collection_audit_path": "collection_audit.json",
            "collection_audit_summary": {
                "status": collection_audit.get("status"),
                "warning_count": len(collection_audit.get("warnings", [])),
                "filing_count": collection_audit.get("filing_count"),
                "downloaded_filing_count": collection_audit.get("downloaded_filing_count"),
            },
        }

        normalized_path = self.target_dir / "normalized_financials.json"
        package_path = self.target_dir / "company_input_package.json"
        evidence_path = self.target_dir / "evidence_table.json"
        markdown_path = self.target_dir / "company_input_package.md"
        self._write_json(normalized_path, normalized_financials)
        self._write_json(evidence_path, {"schema_version": "1.0", "items": evidence_table})
        self._write_json(package_path, package)
        markdown_path.write_text(self._render_markdown(package), encoding="utf-8")

        return {
            "normalized_financials_path": str(normalized_path.resolve()),
            "company_input_package_path": str(package_path.resolve()),
            "evidence_table_path": str(evidence_path.resolve()),
            "company_input_package_md_path": str(markdown_path.resolve()),
            "metric_count": len(normalized_financials.get("metrics", {})),
            "market_snapshot_status": market_data.get("status"),
            "current_price": market_data.get("current_price"),
            "evidence_item_count": len(evidence_table),
            "known_gap_count": len(known_gaps),
        }

    def _normalize_financials(self, companyfacts: dict[str, Any], ticker: str, company_name: str, cik: str) -> dict[str, Any]:
        """归一化 SEC companyfacts 中的核心财务指标。

        Args:
            companyfacts: SEC companyfacts JSON。
            ticker: 股票代码。
            company_name: 公司名称。
            cik: 十位 CIK。

        Returns:
            归一化财务数据。
        """

        source_url = SEC_COMPANYFACTS_URL.format(cik=cik)
        metrics: dict[str, Any] = {}
        for metric_name, candidates in METRIC_CONCEPTS.items():
            facts = self._collect_metric_facts(companyfacts, metric_name, candidates, source_url)
            metrics[metric_name] = {
                "aliases_tried": [f"{candidate.taxonomy}:{candidate.concept}" for candidate in candidates],
                "latest_annual": self._latest_fact(facts, annual=True),
                "latest_quarterly": self._latest_fact(facts, annual=False),
                "recent_facts": facts[:10],
                "fact_count": len(facts),
                "status": "available" if facts else "missing",
            }

        derived_metrics = self._build_derived_metrics(metrics)
        return {
            "schema_version": "1.0",
            "generated_at": self._now_iso(),
            "target": {"ticker": ticker, "company_name": company_name, "cik": cik},
            "currency": "USD",
            "source_url": source_url,
            "metrics": metrics,
            "derived_metrics": derived_metrics,
            "normalization_notes": [
                "SEC XBRL 概念存在公司差异；每个指标保留 source_concept 和 confidence，避免把弱匹配伪装成强可比数据。",
                "同一指标可能同时存在年度、季度、累计口径；v1 优先展示最新 10-K 年度和最新 10-Q 季度事实。",
            ],
        }

    def _collect_metric_facts(
        self,
        companyfacts: dict[str, Any],
        metric_name: str,
        candidates: list[ConceptCandidate],
        source_url: str,
    ) -> list[dict[str, Any]]:
        """收集某个指标的候选事实。

        Args:
            companyfacts: SEC companyfacts JSON。
            metric_name: 内部指标名。
            candidates: 候选 XBRL 概念。
            source_url: companyfacts 来源 URL。

        Returns:
            按 filing 日期和期末日倒序排列的事实列表。
        """

        facts_root = companyfacts.get("facts", {})
        collected: list[dict[str, Any]] = []
        for candidate in candidates:
            concept_payload = facts_root.get(candidate.taxonomy, {}).get(candidate.concept)
            if not concept_payload:
                continue
            units = concept_payload.get("units", {})
            unit_name = candidate.preferred_unit if candidate.preferred_unit in units else self._first_unit(units)
            if not unit_name:
                continue
            for item in units.get(unit_name, []):
                form = str(item.get("form") or "")
                if form not in {"10-K", "10-Q"}:
                    continue
                value = item.get("val")
                if value is None:
                    continue
                collected.append(
                    {
                        "metric": metric_name,
                        "value": value,
                        "unit": unit_name,
                        "source_concept": f"{candidate.taxonomy}:{candidate.concept}",
                        "source_url": source_url,
                        "confidence": candidate.confidence,
                        "form": form,
                        "fiscal_year": item.get("fy"),
                        "fiscal_period": item.get("fp"),
                        "start_date": item.get("start"),
                        "end_date": item.get("end"),
                        "filed": item.get("filed"),
                        "accession_number": item.get("accn"),
                        "frame": item.get("frame"),
                    }
                )
        # 保留最新事实优先，便于下游快速读取；没有 filed 时用 end_date 作为兜底排序键。
        collected.sort(key=lambda fact: (str(fact.get("filed") or ""), str(fact.get("end_date") or "")), reverse=True)
        return collected

    def _latest_fact(self, facts: list[dict[str, Any]], annual: bool) -> dict[str, Any] | None:
        """选择最新年度或季度事实。

        Args:
            facts: 候选事实列表。
            annual: True 选择 10-K / FY，False 选择 10-Q / Q*。

        Returns:
            最新事实；没有则返回 None。
        """

        for fact in facts:
            fiscal_period = str(fact.get("fiscal_period") or "")
            if annual and fact.get("form") == "10-K" and fiscal_period == "FY":
                return fact
            if not annual and fact.get("form") == "10-Q" and fiscal_period.startswith("Q"):
                return fact
        return None

    def _build_derived_metrics(self, metrics: dict[str, Any]) -> dict[str, Any]:
        """构建派生指标。

        Args:
            metrics: 已归一化指标。

        Returns:
            派生指标字典。
        """

        operating_cash_flow = metrics.get("operating_cash_flow", {}).get("latest_annual")
        capex = metrics.get("capex", {}).get("latest_annual")
        if not operating_cash_flow or not capex:
            return {
                "free_cash_flow_proxy": {
                    "status": "missing",
                    "reason": "缺少最新年度 operating_cash_flow 或 capex。",
                }
            }
        if operating_cash_flow.get("fiscal_year") != capex.get("fiscal_year"):
            return {
                "free_cash_flow_proxy": {
                    "status": "missing",
                    "reason": "operating_cash_flow 与 capex 的最新年度不一致，v1 不强行相减。",
                }
            }
        return {
            "free_cash_flow_proxy": {
                "status": "available",
                "value": operating_cash_flow["value"] - capex["value"],
                "unit": operating_cash_flow.get("unit"),
                "fiscal_year": operating_cash_flow.get("fiscal_year"),
                "formula": "operating_cash_flow - capex",
                "source_facts": [operating_cash_flow, capex],
                "confidence": "medium",
            }
        }

    def _build_market_data_summary(self, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        """构建行情快照摘要。

        Args:
            market_snapshot: public_market_quote_scraper 生成的 market_snapshot.json。

        Returns:
            可写入公司输入包的行情摘要。
        """

        if not market_snapshot:
            return {
                "status": "missing",
                "snapshot_path": "market_snapshot.json",
                "audit_path": "market_snapshot_audit.json",
                "note": "未生成公开网页行情交叉快照；估值前需要补充当前价格。",
            }
        primary_snapshot = market_snapshot.get("primary_snapshot") or {}
        cross_check = market_snapshot.get("cross_check") or {}
        audit = market_snapshot.get("audit") or {}
        consensus_passed = cross_check.get("consensus_passed") is True
        status = "available" if audit.get("status") == "pass" and consensus_passed else "needs_review"
        return {
            "status": status,
            "snapshot_path": "market_snapshot.json",
            "audit_path": "market_snapshot_audit.json",
            "scope": market_snapshot.get("market_data_scope"),
            "current_price": primary_snapshot.get("last_price"),
            "previous_close": primary_snapshot.get("previous_close"),
            "open_price": primary_snapshot.get("open_price"),
            "high": primary_snapshot.get("high"),
            "low": primary_snapshot.get("low"),
            "volume": primary_snapshot.get("volume"),
            "primary_source": primary_snapshot.get("source"),
            "primary_source_url": primary_snapshot.get("url"),
            "market_time": primary_snapshot.get("market_time"),
            "retrieved_at": primary_snapshot.get("retrieved_at"),
            "median_last_price": cross_check.get("median_last_price"),
            "max_deviation_pct": cross_check.get("max_deviation_pct"),
            "consensus_passed": consensus_passed,
            "source_count": cross_check.get("successful_price_source_count"),
            "sources": [
                {
                    "source": source.get("source"),
                    "url": source.get("url"),
                    "status": source.get("status"),
                    "last_price": source.get("last_price"),
                    "retrieved_at": source.get("retrieved_at"),
                }
                for source in market_snapshot.get("sources", [])
            ],
            "limitations": audit.get("limitations", []),
        }

    def _build_evidence_table(
        self,
        source_manifest: list[dict[str, Any]],
        filing_manifest: list[dict[str, Any]],
        normalized_financials: dict[str, Any],
        market_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """构建证据表。

        Args:
            source_manifest: 来源清单。
            filing_manifest: filing 清单。
            normalized_financials: 归一化财务数据。
            market_data: 公开网页行情快照摘要。

        Returns:
            证据 item 列表。
        """

        items: list[dict[str, Any]] = []
        source_index = 1
        for source in source_manifest:
            if source.get("source_type") == "sec_filing_primary_document":
                continue
            items.append(
                {
                    "ref_id": f"SRC-{source_index:03d}",
                    "evidence_type": source.get("source_type"),
                    "title": source.get("source_id"),
                    "source_url": source.get("url"),
                    "local_path": source.get("local_path"),
                    "status": source.get("status"),
                }
            )
            source_index += 1
        for index, filing in enumerate(filing_manifest, start=1):
            items.append(
                {
                    "ref_id": f"FILING-{index:03d}",
                    "evidence_type": "sec_filing",
                    "title": f"{filing.get('form')} {filing.get('filing_date')} {filing.get('primary_document')}",
                    "source_url": filing.get("filing_url"),
                    "local_path": filing.get("local_path"),
                    "status": filing.get("download_status"),
                }
            )
        metric_index = 1
        for metric_name, metric_payload in normalized_financials.get("metrics", {}).items():
            latest_annual = metric_payload.get("latest_annual")
            if not latest_annual:
                continue
            items.append(
                {
                    "ref_id": f"FIN-{metric_index:03d}",
                    "evidence_type": "sec_xbrl_fact",
                    "title": f"{metric_name} latest annual",
                    "source_url": latest_annual.get("source_url"),
                    "local_path": "raw/sec/companyfacts_CIK" + normalized_financials.get("target", {}).get("cik", "") + ".json",
                    "status": "available",
                    "value": latest_annual.get("value"),
                    "unit": latest_annual.get("unit"),
                    "source_concept": latest_annual.get("source_concept"),
                    "period": latest_annual.get("fiscal_year"),
                }
            )
            metric_index += 1
        if market_data.get("status") in {"available", "needs_review"}:
            items.append(
                {
                    "ref_id": "MARKET-001",
                    "evidence_type": "public_webpage_quote_cross_check",
                    "title": "market_snapshot daily quote cross-check",
                    "source_url": market_data.get("primary_source_url"),
                    "local_path": market_data.get("snapshot_path"),
                    "status": market_data.get("status"),
                    "value": market_data.get("current_price"),
                    "unit": "USD/share",
                    "source_count": market_data.get("source_count"),
                    "max_deviation_pct": market_data.get("max_deviation_pct"),
                }
            )
        return items

    def _build_known_gaps(self, normalized_financials: dict[str, Any], market_data: dict[str, Any]) -> list[dict[str, str]]:
        """构建已知缺口列表。"""

        missing_metrics = [name for name, payload in normalized_financials.get("metrics", {}).items() if payload.get("status") == "missing"]
        gaps = [
            {
                "gap_type": "paid_terminal_consensus",
                "description": "未使用 Bloomberg、FactSet、Wind、Capital IQ 等付费终端，因此没有一致预期、机构目标价和终端口径同行倍数。",
            },
            {
                "gap_type": "business_detail",
                "description": "产品线、价格、出货量、订单、资本开支计划和管理层指引等经营变量需要继续从 filing 正文、IR 材料和行业公开数据补证。",
            },
        ]
        if market_data.get("status") == "missing":
            gaps.append(
                {
                    "gap_type": "market_price",
                    "description": "未生成公开网页行情交叉快照；估值分析前仍需补充当前股价、市值、企业价值和同业倍数。",
                }
            )
        elif market_data.get("status") == "needs_review":
            gaps.append(
                {
                    "gap_type": "market_price_needs_review",
                    "description": "已生成公开网页行情快照，但多源一致性或审计状态未通过；估值前需要人工复核。",
                }
            )
        else:
            gaps.append(
                {
                    "gap_type": "market_data_license_scope",
                    "description": "当前价格来自公开网页交叉抓取，可用于内部投研估值输入；不能等同于交易所授权实时行情或对外分发行情。",
                }
            )
        if missing_metrics:
            gaps.append(
                {
                    "gap_type": "xbrl_metric_missing",
                    "description": "部分标准指标未在当前候选概念中稳定取得：" + ", ".join(missing_metrics),
                }
            )
        return gaps

    def _render_markdown(self, package: dict[str, Any]) -> str:
        """渲染人工可读 Markdown 摘要。"""

        target = package["target"]
        financials = package["financials"]
        market_data = package.get("market_data", {})
        lines = [
            f"# {target['company_name']} ({target['ticker']}) 公开源公司输入包",
            "",
            f"- 市场：{target.get('market', '')}",
            f"- CIK：{target.get('cik', '')}",
            f"- 信息日期：{package.get('as_of_date', '')}",
            f"- 是否使用付费终端：{package['source_policy']['paid_terminals_used']}",
            f"- Filing 记录数：{len(package.get('filings', []))}",
            f"- 行情快照状态：{market_data.get('status', 'missing')}",
            f"- 当前价：{market_data.get('current_price', '')}",
            f"- 证据条目数：{len(package.get('evidence_table', []))}",
            "",
            "## 日级行情快照",
            "",
            f"- 来源范围：{market_data.get('scope', '')}",
            f"- 主来源：{market_data.get('primary_source', '')}",
            f"- 页面行情时间：{market_data.get('market_time', '')}",
            f"- 多源中位价：{market_data.get('median_last_price', '')}",
            f"- 最大价格偏差：{market_data.get('max_deviation_pct', '')}",
            f"- 交叉校验通过：{market_data.get('consensus_passed', '')}",
            "",
            "## 最新年度核心财务事实",
            "",
            "| 指标 | 值 | 单位 | 财年 | 来源概念 | 置信度 |",
            "|---|---:|---|---:|---|---|",
        ]
        for metric_name, payload in financials.get("metrics", {}).items():
            fact = payload.get("latest_annual")
            if not fact:
                continue
            lines.append(
                f"| {metric_name} | {fact.get('value', '')} | {fact.get('unit', '')} | {fact.get('fiscal_year', '')} | {fact.get('source_concept', '')} | {fact.get('confidence', '')} |"
            )
        lines.extend(["", "## 已知缺口", ""])
        for gap in package.get("known_gaps", []):
            lines.append(f"- **{gap.get('gap_type')}**：{gap.get('description')}")
        lines.extend(["", "## 近期 Filing", "", "| Form | Filing date | Report date | Primary document | 下载状态 |", "|---|---|---|---|---|"])
        for filing in package.get("filings", [])[:12]:
            lines.append(
                f"| {filing.get('form', '')} | {filing.get('filing_date', '')} | {filing.get('report_date', '')} | {filing.get('primary_document', '')} | {filing.get('download_status', '')} |"
            )
        lines.append("")
        return "\n".join(lines)

    def _first_unit(self, units: dict[str, list[dict[str, Any]]]) -> str | None:
        """返回第一个可用单位。"""

        for unit_name in units:
            return unit_name
        return None

    def _load_json(self, path: Path, default: Any) -> Any:
        """读取 JSON，文件不存在时返回默认值。"""

        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: Any) -> None:
        """写入 UTF-8 JSON 文件。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _now_iso(self) -> str:
        """返回 UTC ISO 时间。"""

        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
