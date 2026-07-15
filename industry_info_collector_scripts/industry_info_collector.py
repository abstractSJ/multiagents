"""通用行业研究信息收集器。

本模块把本地财报收集、财报解析、财务分析、行业 seed 数据和用户/上游系统
提供的本地行业资料文件汇聚成行业研究员可直接消费的结构化输入包。它只做
资料整理、来源登记和缺口审计，不形成行业结论，也不输出任何交易建议。
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    # 包导入路径供测试与其他模块复用；避免依赖调用方手工修改 sys.path。
    from .adapters import (
        AdapterResult,
        CompanyEventsFileAdapter,
        IndustryPublicStatsFileAdapter,
        IndustrySignalFileAdapter,
        MarketValuationFileAdapter,
        PolicyRegulationFileAdapter,
        filter_records,
        select_market_snapshot,
    )
except ImportError:
    # 直接运行同目录脚本时保留原有的顶层模块解析方式。
    from adapters import (
        AdapterResult,
        CompanyEventsFileAdapter,
        IndustryPublicStatsFileAdapter,
        IndustrySignalFileAdapter,
        MarketValuationFileAdapter,
        PolicyRegulationFileAdapter,
        filter_records,
        select_market_snapshot,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = PROJECT_ROOT / "industry_info_collector_scripts" / "collector_workspace"
DEFAULT_SEED_DATA = PROJECT_ROOT / "industry_info_collector_scripts" / "reference" / "industry_seed_data.json"
DEFAULT_FINANCIAL_MANIFEST = PROJECT_ROOT / "info_collector_scripts" / "collector_workspace" / "manifests" / "cninfo_all_reports.json"


@dataclass
class CollectionPaths:
    """行业信息包生成所需的输入路径。

    Args:
        financial_analysis_report: 财务分析员输出 JSON 路径。
        processor_content_json: 信息处理员 content.json 路径。
        financial_manifest: 原财报信息收集员总清单路径。
        seed_data: 行业 seed 数据路径。
        company_events_file: 本地公司事件文件。
        policy_regulation_file: 本地政策监管文件。
        industry_public_stats_file: 本地行业公开统计文件。
        industry_signals_file: 本地行业信号文件。
        market_valuation_file: 本地行情估值快照文件。
        event_timeline_file: 本地事件时间线文件。
        event_impacts_file: 本地事件影响观测文件。
    """

    financial_analysis_report: Path | None
    processor_content_json: Path | None
    financial_manifest: Path | None
    seed_data: Path
    company_events_file: Path | None = None
    policy_regulation_file: Path | None = None
    industry_public_stats_file: Path | None = None
    industry_signals_file: Path | None = None
    market_valuation_file: Path | None = None
    event_timeline_file: Path | None = None
    event_impacts_file: Path | None = None


@dataclass
class ClassificationOverride:
    """CLI 或上游显式传入的行业分类覆盖。"""

    industry_name: str | None = None
    secondary_industry: str | None = None
    classification_system: str | None = None


@dataclass
class EventStudyRequest:
    """事件研究请求。

    Args:
        event_name: 事件名称。
        event_type: 事件类型。
        event_description: 事件描述。
        event_start_date: 事件开始时间。
        event_end_date: 事件结束时间或 ongoing。
        event_status: 事件状态。
        event_window: 事件观察窗口。
        baseline_period: 事件前基线窗口。
        impact_variables: 需要重点判断的影响变量。
        pricing_variable: 重点价格变量。
        affected_segments: 受影响的细分环节。
        geography_scope: 事件影响区域。
        counterfactual_assumption: 无事件时的基线假设。
    """

    event_name: str | None = None
    event_type: str | None = None
    event_description: str | None = None
    event_start_date: str | None = None
    event_end_date: str | None = None
    event_status: str | None = None
    event_window: str | None = None
    baseline_period: str | None = None
    impact_variables: list[str] | None = None
    pricing_variable: str | None = None
    affected_segments: list[str] | None = None
    geography_scope: str | None = None
    counterfactual_assumption: str | None = None


class IndustryInfoCollector:
    """面向行业研究员的信息包收集器。

    Args:
        workspace: 信息收集员2工作区路径。
    """

    def __init__(self, workspace: Path = DEFAULT_WORKSPACE) -> None:
        self.workspace = workspace
        self.manifest_dir = self.workspace / "manifests"
        self.package_root = self.workspace / "packages"

    def collect(
        self,
        stock_code: str | None,
        company_name: str | None,
        fiscal_year: str | None,
        as_of_date: str,
        paths: CollectionPaths,
        offline: bool = True,
        classification_override: ClassificationOverride | None = None,
        target: str | None = None,
        deliverable_type: str = "investment_research",
        event_study_request: EventStudyRequest | None = None,
    ) -> dict[str, Any]:
        """生成行业研究输入包并写入工作区。

        Args:
            stock_code: 股票代码；纯行业模式下可为空。
            company_name: 公司名称；纯行业模式下可为空。
            fiscal_year: 财报年度；纯行业模式下可为空。
            as_of_date: 信息包生成日期，格式 YYYY-MM-DD。
            paths: 输入文件路径集合。
            offline: 是否离线模式。
            classification_override: 显式行业分类输入。
            target: 纯行业模式下的目标行业、板块或主题名。
            deliverable_type: 交付类型，例如 investment_research 或 theme_event_study。
            event_study_request: 事件研究请求；仅事件研究时传入。

        Returns:
            写入文件路径、package_id 和输入质量摘要。
        """

        self._ensure_workspace()
        deliverable_type = deliverable_type or "investment_research"
        explicit_industry_target = target or (classification_override.industry_name if classification_override else None)
        has_company_context = bool(stock_code and fiscal_year)
        package_slug = self._slugify_identifier(stock_code or explicit_industry_target or company_name or "unknown_industry")
        package_id = f"industry_input_{package_slug}_{fiscal_year or 'na'}_{as_of_date}"
        output_dir = self.package_root / package_slug / as_of_date
        output_dir.mkdir(parents=True, exist_ok=True)

        financial_report = self._read_json_if_exists(paths.financial_analysis_report)
        processor_content = self._read_json_if_exists(paths.processor_content_json)
        financial_manifest = self._read_json_if_exists(paths.financial_manifest)
        seed_data = self._read_json_if_exists(paths.seed_data) or {}

        effective_stock_code = stock_code or package_slug
        if has_company_context:
            effective_company_name = company_name or self._infer_company_name(effective_stock_code, financial_report, processor_content, seed_data)
        else:
            effective_company_name = company_name or explicit_industry_target or package_slug
        effective_fiscal_year = fiscal_year or "unknown"

        source_refs: list[dict[str, Any]] = []
        evidence_items: list[dict[str, Any]] = []
        seed_company = seed_data.get("companies", {}).get(effective_stock_code, {})

        adapter_results = self._load_adapters(paths)
        company_events_records = (
            filter_records(adapter_results["company_events"].records, stock_code=effective_stock_code, include_unscoped=False)
            if has_company_context
            else []
        )

        industry_classification = self._build_industry_classification(
            stock_code=effective_stock_code,
            seed_company=seed_company,
            financial_report=financial_report,
            source_refs=source_refs,
            evidence_items=evidence_items,
            paths=paths,
            as_of_date=as_of_date,
            override=classification_override,
        )
        if explicit_industry_target and industry_classification.get("primary_industry") == "未知行业":
            industry_classification["primary_industry"] = explicit_industry_target
            industry_classification["classification_basis"] = industry_classification.get("classification_basis", []) + ["目标行业由 CLI 或上游显式传入。"]
        industry_name = industry_classification.get("primary_industry") or explicit_industry_target or "未知行业"
        seed_industry = seed_data.get("industries", {}).get(industry_name, {})

        policy_records = filter_records(adapter_results["policy_regulation"].records, industry=industry_name)
        public_stats_records = filter_records(adapter_results["industry_public_stats"].records, industry=industry_name)
        industry_signal_records = filter_records(adapter_results["industry_signals"].records, industry=industry_name, as_of_date=as_of_date)
        market_snapshot = select_market_snapshot(adapter_results["market_valuation"].records, effective_stock_code, as_of_date) if has_company_context else None

        if has_company_context or isinstance(financial_report, dict) or isinstance(processor_content, dict):
            company_profile = self._build_company_profile(
                stock_code=effective_stock_code,
                company_name=effective_company_name,
                financial_report=financial_report,
                processor_content=processor_content,
                source_refs=source_refs,
                evidence_items=evidence_items,
                paths=paths,
                as_of_date=as_of_date,
            )
            financial_summary = self._build_financial_summary(
                financial_report=financial_report,
                source_refs=source_refs,
                evidence_items=evidence_items,
                paths=paths,
                as_of_date=as_of_date,
            )
            business_segments = self._build_business_segments(
                seed_company=seed_company,
                financial_report=financial_report,
                processor_content=processor_content,
                source_refs=source_refs,
                evidence_items=evidence_items,
                paths=paths,
                as_of_date=as_of_date,
            )
            competitors = self._build_competitors(
                seed_company=seed_company,
                source_refs=source_refs,
                evidence_items=evidence_items,
                paths=paths,
                as_of_date=as_of_date,
            )
        else:
            company_profile = {
                "company_name": effective_company_name,
                "stock_code": effective_stock_code,
                "exchange": "not_applicable_industry_first_package",
                "report_title": None,
                "published_at": None,
                "announcement_id": None,
                "main_business": "纯行业研究输入包；当前未绑定单一公司主营业务。",
                "business_model": "not_applicable_industry_first_package",
                "source_refs": [],
            }
            financial_summary = {"source_refs": [], "limitations": ["纯行业模式下未接入公司财务分析报告。"]}
            business_segments = []
            competitors = []

        company_events = self._records_with_source(
            records=company_events_records,
            adapter_result=adapter_results["company_events"],
            claim="本地公司事件文件提供公司事件、公告或投资者关系资料。",
            source_refs=source_refs,
            evidence_items=evidence_items,
            as_of_date=as_of_date,
        )
        industry_data = self._build_industry_data(
            industry_name=industry_name,
            seed_industry=seed_industry,
            public_stats_records=public_stats_records,
            industry_signal_records=industry_signal_records,
            adapter_results=adapter_results,
            source_refs=source_refs,
            evidence_items=evidence_items,
            paths=paths,
            as_of_date=as_of_date,
        )
        seed_policy = self._seed_list_with_source(
            seed_industry.get("policy_and_regulation", []),
            paths.seed_data,
            "政策监管 seed 指针",
            "curated_seed",
            source_refs,
            evidence_items,
            as_of_date,
        )
        local_policy = self._records_with_source(
            records=policy_records,
            adapter_result=adapter_results["policy_regulation"],
            claim="本地政策监管文件提供行业政策或监管资料。",
            source_refs=source_refs,
            evidence_items=evidence_items,
            as_of_date=as_of_date,
        )
        policy_and_regulation = seed_policy + local_policy
        technology_trends = self._seed_list_with_source(
            seed_industry.get("technology_trends", []),
            paths.seed_data,
            "技术趋势 seed 指针",
            "curated_seed",
            source_refs,
            evidence_items,
            as_of_date,
        )
        news = self._seed_list_with_source(
            seed_industry.get("news_pointers", []),
            paths.seed_data,
            "新闻补采 seed 指针",
            "curated_seed",
            source_refs,
            evidence_items,
            as_of_date,
        )
        market_data = self._build_market_data(
            market_snapshot=market_snapshot,
            adapter_result=adapter_results["market_valuation"],
            source_refs=source_refs,
            evidence_items=evidence_items,
            as_of_date=as_of_date,
        )
        annual_report_source = self._find_annual_report_source(financial_manifest, effective_stock_code, effective_fiscal_year) if has_company_context else None
        if annual_report_source:
            ref_id = self._add_source_ref(
                source_refs,
                source_type="financial_report_manifest",
                source_path=paths.financial_manifest,
                source_detail="cninfo_all_reports.json matched annual report record",
                reliability="high",
                as_of_date=as_of_date,
                limitations=[],
            )
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="原始年报来源和本地路径已从财报信息收集员总清单确认。",
                    evidence=json.dumps(annual_report_source, ensure_ascii=False),
                    source_type="financial_report_manifest",
                    source_path=paths.financial_manifest,
                    source_detail="matched record",
                    reliability="high",
                    as_of_date=as_of_date,
                    limitations=[],
                )
            )

        event_study = self._build_event_study_overlay(
            industry_name=industry_name,
            as_of_date=as_of_date,
            deliverable_type=deliverable_type,
            event_study_request=event_study_request,
            public_stats_records=public_stats_records,
            industry_signal_records=industry_signal_records,
            source_refs=source_refs,
            evidence_items=evidence_items,
            paths=paths,
        )
        quantitative_variable_table = self._build_quantitative_variable_table(
            public_stats_records=public_stats_records,
            industry_signal_records=industry_signal_records,
            market_snapshot=market_snapshot,
            event_study=event_study,
            as_of_date=as_of_date,
        )

        limitations = self._build_limitations(
            offline=offline,
            financial_report=financial_report,
            seed_data=seed_data,
            industry_classification=industry_classification,
            market_data=market_data,
        )
        recommended_next_collection = self._build_recommended_next_collection(
            seed_industry=seed_industry,
            public_stats=industry_data.get("public_stats", []),
            industry_signals=industry_data.get("industry_signals", []),
            company_events=company_events,
            policy_and_regulation=policy_and_regulation,
            market_data=market_data,
            event_study=event_study,
        )
        gaps = self._build_general_gaps(
            deliverable_type=deliverable_type,
            public_stats=industry_data.get("public_stats", []),
            industry_signals=industry_data.get("industry_signals", []),
            policy_and_regulation=policy_and_regulation,
            company_events=company_events,
            event_study=event_study,
            market_data=market_data,
        )
        package_quality_gate = self._build_package_quality_gate(
            deliverable_type=deliverable_type,
            quantitative_variable_table=quantitative_variable_table,
            policy_and_regulation=policy_and_regulation,
            public_stats=industry_data.get("public_stats", []),
            industry_signals=industry_data.get("industry_signals", []),
            competitors=competitors,
            event_study=event_study,
            has_company_context=has_company_context,
            as_of_date=as_of_date,
            gaps=gaps,
        )

        package = {
            "schema_version": "1.2",
            "collector_name": "信息收集员2",
            "generated_at": self._now_iso(),
            "task_id": package_id,
            "target": {
                "industry_name": industry_name,
                "as_of_date": as_of_date,
                "deliverable_type": deliverable_type,
                "segment_scope": event_study_request.affected_segments if event_study_request and event_study_request.affected_segments else [],
                "geography_scope": event_study_request.geography_scope if event_study_request and event_study_request.geography_scope else "unknown",
            },
            "deliverable_type": deliverable_type,
            "company": {
                "name": effective_company_name if has_company_context else None,
                "ticker": effective_stock_code if has_company_context else None,
                "exchange": seed_company.get("exchange") or company_profile.get("exchange") or ("unknown" if has_company_context else None),
                "country": seed_company.get("country") or ("unknown" if has_company_context else None),
                "fiscal_year": effective_fiscal_year if has_company_context else None,
                "as_of_date": as_of_date,
                "role": "validation_anchor" if has_company_context else "not_applicable_industry_first_package",
            },
            "information_package": {
                "company_profile": company_profile,
                "financial_summary": financial_summary,
                "business_segments": business_segments,
                "industry_classification": industry_classification,
                "competitors": competitors,
                "company_events": company_events,
                "industry_data": industry_data,
                "policy_and_regulation": policy_and_regulation,
                "technology_trends": technology_trends,
                "management_commentary": self._build_management_commentary(financial_report),
                "news": news,
                "market_data": market_data,
                "annual_report_source": annual_report_source,
            },
            "financial_analysis_report": self._build_financial_analysis_ref(financial_report, paths.financial_analysis_report),
            "research_scope": {
                "time_horizon": self._resolve_time_horizon(deliverable_type, event_study_request),
                "focus": self._resolve_focus_text(deliverable_type, event_study_request),
            },
            "source_ref_index": source_refs,
            "limitations": limitations,
            "recommended_next_collection": recommended_next_collection,
            "quantitative_variable_table": quantitative_variable_table,
            "gaps": gaps,
            "package_quality_gate": package_quality_gate,
            "can_support_full_research": package_quality_gate["can_support_full_research"],
        }
        if event_study is not None:
            package["event_study"] = event_study

        evidence_table = {
            "schema_version": "1.0",
            "package_id": package_id,
            "generated_at": package["generated_at"],
            "items": evidence_items,
        }
        audit = self._build_audit(
            package_id=package_id,
            paths=paths,
            package=package,
            offline=offline,
            financial_report=financial_report,
            seed_data=seed_data,
            adapter_results=adapter_results,
            deliverable_type=deliverable_type,
            event_study=event_study,
        )

        package_path = output_dir / "industry_input_package.json"
        markdown_path = output_dir / "industry_input_package.md"
        evidence_path = output_dir / "evidence_table.json"
        audit_path = output_dir / "collection_audit.json"

        self._write_json(package_path, package)
        self._write_json(evidence_path, evidence_table)
        self._write_json(audit_path, audit)
        markdown_path.write_text(self._render_package_markdown(package, audit), encoding="utf-8")

        manifest_record = self._manifest_record(
            package_id=package_id,
            package=package,
            audit=audit,
            output_dir=output_dir,
            package_path=package_path,
            markdown_path=markdown_path,
            evidence_path=evidence_path,
            audit_path=audit_path,
        )
        self._update_manifest(manifest_record)

        return {
            "package_id": package_id,
            "package_path": str(package_path),
            "markdown_path": str(markdown_path),
            "evidence_path": str(evidence_path),
            "audit_path": str(audit_path),
            "input_quality": audit["input_quality"],
            "ready_for_industry_researcher": audit["ready_for_industry_researcher"],
            "limitations": limitations,
        }

    def _ensure_workspace(self) -> None:
        """创建工作区目录，避免把目录初始化工作留给用户。"""

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.package_root.mkdir(parents=True, exist_ok=True)

    def _load_adapters(self, paths: CollectionPaths) -> dict[str, AdapterResult]:
        """读取所有可选本地适配器文件。"""

        return {
            "company_events": CompanyEventsFileAdapter().load(paths.company_events_file),
            "policy_regulation": PolicyRegulationFileAdapter().load(paths.policy_regulation_file),
            "industry_public_stats": IndustryPublicStatsFileAdapter().load(paths.industry_public_stats_file),
            "industry_signals": IndustrySignalFileAdapter().load(paths.industry_signals_file),
            "market_valuation": MarketValuationFileAdapter().load(paths.market_valuation_file),
        }

    def _read_json_if_exists(self, path: Path | None) -> Any:
        """读取 JSON 文件；路径缺失或文件不存在时返回 None。"""

        if path is None or not path.exists():
            return None
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _write_json(self, path: Path, payload: Any) -> None:
        """以稳定中文格式写入 JSON，便于人工审阅和版本比较。"""

        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _infer_company_name(self, stock_code: str, financial_report: Any, processor_content: Any, seed_data: dict[str, Any]) -> str:
        """从本地输入推断公司名称，保证 CLI 未传公司名时仍能生成包。"""

        if isinstance(financial_report, dict):
            company = financial_report.get("company", {})
            if company.get("company_name"):
                return company["company_name"]
        if isinstance(processor_content, dict):
            metadata = processor_content.get("document_metadata", {})
            if metadata.get("company_name"):
                return metadata["company_name"]
        seed_company = seed_data.get("companies", {}).get(stock_code, {})
        return seed_company.get("company_name") or stock_code

    def _build_company_profile(
        self,
        stock_code: str,
        company_name: str,
        financial_report: Any,
        processor_content: Any,
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        paths: CollectionPaths,
        as_of_date: str,
    ) -> dict[str, Any]:
        """构建公司画像，优先使用财务分析员报告和 PDF 解析元数据。"""

        business_profile = financial_report.get("business_profile", {}) if isinstance(financial_report, dict) else {}
        metadata = processor_content.get("document_metadata", {}) if isinstance(processor_content, dict) else {}
        main_business = business_profile.get("main_business") or self._search_content_text(processor_content or {}, ["主营业务", "经营模式", "营业收入"]) or "未从本地输入中提取到主营业务描述。"
        ref_id = self._add_source_ref(
            source_refs,
            source_type="financial_analyst_output_or_processor_content",
            source_path=paths.financial_analysis_report if isinstance(financial_report, dict) else paths.processor_content_json,
            source_detail="business_profile.main_business / content keyword search",
            reliability="medium" if isinstance(financial_report, dict) else "low",
            as_of_date=as_of_date,
            limitations=["该公司画像是行业研究输入，关键表述仍应回到年报原文复核。"],
        )
        evidence_items.append(
            self._evidence_item(
                ref_id=ref_id,
                claim="公司主营业务和经营模式来自本地财务分析或年报解析结果。",
                evidence=self._shorten(main_business, 800),
                source_type="financial_analyst_output_or_processor_content",
                source_path=paths.financial_analysis_report if isinstance(financial_report, dict) else paths.processor_content_json,
                source_detail="company_profile evidence",
                reliability="medium" if isinstance(financial_report, dict) else "low",
                as_of_date=as_of_date,
                limitations=["自动摘要或关键词检索可能包含重复句和解析噪声。"],
            )
        )
        return {
            "company_name": company_name,
            "stock_code": stock_code,
            "exchange": metadata.get("exchange") or metadata.get("stock_exchange") or "unknown",
            "report_title": metadata.get("title"),
            "published_at": metadata.get("published_at"),
            "announcement_id": metadata.get("announcement_id"),
            "main_business": main_business,
            "business_model": business_profile.get("business_model") or "未结构化提取；需要行业研究员结合年报和业务分部复核。",
            "source_refs": [ref_id],
        }

    def _build_financial_summary(
        self,
        financial_report: Any,
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        paths: CollectionPaths,
        as_of_date: str,
    ) -> dict[str, Any]:
        """整理财务分析员提供的核心财务摘要，供行业研究员做交叉验证。"""

        metrics = financial_report.get("financial_metrics", {}) if isinstance(financial_report, dict) else {}
        summary = {}
        for key in ["revenue", "net_profit_attributable", "deducted_net_profit", "operating_cash_flow", "gross_margin", "roe", "eps"]:
            metric = metrics.get(key, {})
            summary[key] = {
                "label": metric.get("label", key),
                "current": metric.get("current"),
                "yoy": metric.get("yoy"),
                "unit": metric.get("unit"),
                "extraction_status": metric.get("extraction_status", "missing"),
                "source_refs": metric.get("source_refs", []),
            }
        if isinstance(financial_report, dict):
            ref_id = self._add_source_ref(
                source_refs,
                source_type="financial_analyst_output",
                source_path=paths.financial_analysis_report,
                source_detail="financial_metrics",
                reliability="medium",
                as_of_date=as_of_date,
                limitations=["行业研究员应把财务指标作为公司表现输入，不应据此直接形成行业景气结论。"],
            )
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="核心财务摘要来自财务分析员报告。",
                    evidence=json.dumps(summary, ensure_ascii=False)[:1200],
                    source_type="financial_analyst_output",
                    source_path=paths.financial_analysis_report,
                    source_detail="financial_metrics",
                    reliability="medium",
                    as_of_date=as_of_date,
                    limitations=["部分指标可能需要回到 RAG 或 content.json 核验。"],
                )
            )
            summary["source_refs"] = [ref_id]
        else:
            summary["source_refs"] = []
            summary["limitations"] = ["未找到财务分析员报告，财务摘要缺失。"]
        return summary

    def _build_business_segments(
        self,
        seed_company: dict[str, Any],
        financial_report: Any,
        processor_content: Any,
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        paths: CollectionPaths,
        as_of_date: str,
    ) -> list[dict[str, Any]]:
        """通用业务分部构建，不根据特定行业或特定产品硬编码。"""

        seed_segments = seed_company.get("business_segments", [])
        if seed_segments:
            ref_id = self._add_source_ref(
                source_refs,
                source_type="curated_seed",
                source_path=paths.seed_data,
                source_detail="business_segments",
                reliability="medium",
                as_of_date=as_of_date,
                limitations=["seed 业务分部是研究输入，不替代年报分部表。"],
            )
            evidence_items.append(self._evidence_item(ref_id, "业务分部候选来自 seed 数据。", json.dumps(seed_segments, ensure_ascii=False), "curated_seed", paths.seed_data, "business_segments", "medium", as_of_date, ["需要回到年报分部表复核。"] ))
            return [{**segment, "source_refs": [ref_id]} for segment in seed_segments]

        business_profile = financial_report.get("business_profile", {}) if isinstance(financial_report, dict) else {}
        revenue_drivers = business_profile.get("revenue_drivers", [])
        segment_evidence = "\n".join(str(item) for item in revenue_drivers[:5])
        if not segment_evidence and isinstance(processor_content, dict):
            segment_evidence = self._search_content_text(processor_content, ["主营业务", "收入构成", "分产品", "分行业", "分地区", "营业收入", "毛利率"])
        ref_id = self._add_source_ref(
            source_refs,
            source_type="financial_analyst_output_and_processor_content",
            source_path=paths.financial_analysis_report or paths.processor_content_json,
            source_detail="business_profile.revenue_drivers / generic content keyword search",
            reliability="medium" if segment_evidence else "low",
            as_of_date=as_of_date,
            limitations=["第一版只整理业务分部证据线索，未对分部表做完整结构化重算。"],
        )
        evidence_items.append(
            self._evidence_item(
                ref_id=ref_id,
                claim="业务分部候选来自财务分析员报告或年报解析关键词检索。",
                evidence=self._shorten(segment_evidence or "未从本地输入可靠提取业务分部。", 1200),
                source_type="financial_analyst_output_and_processor_content",
                source_path=paths.financial_analysis_report or paths.processor_content_json,
                source_detail="business segments evidence",
                reliability="medium" if segment_evidence else "low",
                as_of_date=as_of_date,
                limitations=["业务分部数值应由行业研究员或后续脚本回到原始表格精确核验。"],
            )
        )
        if segment_evidence:
            return [
                {
                    "segment_name": "待结构化分部",
                    "role_in_business": "从财务分析员报告或年报解析结果中找到业务分部证据，但尚未拆成标准化分部字段。",
                    "evidence_summary": self._shorten(segment_evidence, 800),
                    "source_refs": [ref_id],
                    "limitations": ["需要补充分产品、分行业、分地区的标准化收入和毛利率数据。"],
                }
            ]
        return [
            {
                "segment_name": "未结构化提取",
                "role_in_business": "需要从年报分部表、公司公告或人工资料补充。",
                "evidence_summary": "未从本地输入可靠提取业务分部。",
                "source_refs": [ref_id],
                "limitations": ["当前未结构化抽取业务分部。"],
            }
        ]

    def _build_industry_classification(
        self,
        stock_code: str,
        seed_company: dict[str, Any],
        financial_report: Any,
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        paths: CollectionPaths,
        as_of_date: str,
        override: ClassificationOverride | None,
    ) -> dict[str, Any]:
        """构建初始行业分类，优先使用显式输入，其次使用 seed。"""

        classification = seed_company.get("initial_industry_classification", {})
        if override and (override.industry_name or override.secondary_industry):
            primary = override.industry_name or classification.get("primary_industry", "未知行业")
            secondary = override.secondary_industry or classification.get("secondary_industry", "未知细分行业")
            system = override.classification_system or "user_cli_override"
            basis = ["行业分类由 CLI 或上游显式传入。"]
            limitations = ["显式行业分类仍需行业研究员结合业务分部和利润来源复核。"]
            source_type = "cli_input"
            source_path = None
            detail = "industry classification override"
            reliability = "medium"
        elif classification:
            primary = classification.get("primary_industry", "未知行业")
            secondary = classification.get("secondary_industry", "未知细分行业")
            system = classification.get("classification_system", "curated_seed")
            basis = classification.get("classification_basis", [])
            limitations = classification.get("limitations", [])
            source_type = "curated_seed"
            source_path = paths.seed_data
            detail = f"companies.{stock_code}.initial_industry_classification"
            reliability = "medium"
        else:
            primary = "未知行业"
            secondary = "未知细分行业"
            system = "missing"
            basis = []
            limitations = ["未提供行业分类 seed 或 CLI 覆盖，信息收集员2不自动推断行业。"]
            source_type = "missing_classification"
            source_path = None
            detail = "industry classification missing"
            reliability = "low"
        if isinstance(financial_report, dict) and financial_report.get("business_profile", {}).get("main_business"):
            basis.append("财务分析员报告提供了主营业务描述，可作为行业归属复核输入，但本收集器不自动推断行业分类。")
        ref_id = self._add_source_ref(source_refs, source_type, source_path, detail, reliability, as_of_date, limitations)
        evidence_items.append(
            self._evidence_item(
                ref_id=ref_id,
                claim=f"{stock_code} 初始行业分类为 {primary}/{secondary}。",
                evidence=json.dumps({"primary_industry": primary, "secondary_industry": secondary, "basis": basis}, ensure_ascii=False),
                source_type=source_type,
                source_path=source_path,
                source_detail=detail,
                reliability=reliability,
                as_of_date=as_of_date,
                limitations=limitations,
            )
        )
        return {
            "primary_industry": primary,
            "secondary_industry": secondary,
            "classification_system": system,
            "classification_basis": basis,
            "source_refs": [ref_id],
            "limitations": limitations,
        }

    def _build_competitors(
        self,
        seed_company: dict[str, Any],
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        paths: CollectionPaths,
        as_of_date: str,
    ) -> list[dict[str, Any]]:
        """生成同行候选；同行年报和同行财务分析由其他 Agent 负责。"""

        peers = seed_company.get("peer_candidates", [])
        if not peers:
            return []
        ref_id = self._add_source_ref(
            source_refs,
            source_type="curated_seed",
            source_path=paths.seed_data,
            source_detail="peer_candidates",
            reliability="medium",
            as_of_date=as_of_date,
            limitations=["同行候选不是最终估值可比公司；同行年报和财务分析由原信息收集员和财务分析员负责。"],
        )
        evidence_items.append(self._evidence_item(ref_id, "同行候选来自 seed 数据。", json.dumps(peers, ensure_ascii=False), "curated_seed", paths.seed_data, "peer_candidates", "medium", as_of_date, ["需要行业研究员和估值分析员继续筛选可比性。"] ))
        return [{**peer, "source_refs": [ref_id]} for peer in peers]

    def _build_industry_data(
        self,
        industry_name: str,
        seed_industry: dict[str, Any],
        public_stats_records: list[dict[str, Any]],
        industry_signal_records: list[dict[str, Any]],
        adapter_results: dict[str, AdapterResult],
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        paths: CollectionPaths,
        as_of_date: str,
    ) -> dict[str, Any]:
        """通用行业数据构建，合并 seed 指针、本地公开统计和行业信号。"""

        source_ref_ids: list[str] = []
        value_chain = seed_industry.get("value_chain", {})
        industry_metrics = seed_industry.get("industry_metrics", [])
        tracking_indicators = seed_industry.get("tracking_indicators") or {
            "supply_demand": seed_industry.get("supply_demand_indicators_to_track", []),
            "pricing": seed_industry.get("pricing_indicators_to_track", []),
            "inventory": seed_industry.get("inventory_indicators_to_track", []),
            "cost": [],
            "technology": [],
            "policy": [],
        }
        if seed_industry:
            ref_id = self._add_source_ref(
                source_refs,
                source_type="curated_seed",
                source_path=paths.seed_data,
                source_detail=f"industries.{industry_name}.value_chain / industry_metrics / tracking_indicators",
                reliability="medium",
                as_of_date=as_of_date,
                limitations=["seed 行业数据是研究框架或指针，不替代权威实时数据。"],
            )
            source_ref_ids.append(ref_id)
            evidence_items.append(self._evidence_item(ref_id, f"{industry_name} 行业框架来自 seed 数据。", json.dumps(seed_industry, ensure_ascii=False)[:1600], "curated_seed", paths.seed_data, f"industries.{industry_name}", "medium", as_of_date, ["不能据此直接形成实时行业景气判断。"] ))
            value_chain = {**value_chain, "source_refs": [ref_id]}
            industry_metrics = [{**metric, "source_refs": [ref_id]} for metric in industry_metrics]
        public_stats = self._records_with_source(
            records=public_stats_records,
            adapter_result=adapter_results["industry_public_stats"],
            claim="本地行业公开统计文件提供行业统计指标。",
            source_refs=source_refs,
            evidence_items=evidence_items,
            as_of_date=as_of_date,
        )
        industry_signals = self._records_with_source(
            records=industry_signal_records,
            adapter_result=adapter_results["industry_signals"],
            claim="本地行业信号文件提供价格、库存、需求、供给、渠道、技术或政策信号。",
            source_refs=source_refs,
            evidence_items=evidence_items,
            as_of_date=as_of_date,
        )
        return {
            "value_chain": value_chain,
            "industry_metrics": industry_metrics,
            "public_stats": public_stats,
            "industry_signals": industry_signals,
            "tracking_indicators": tracking_indicators,
            "source_refs": source_ref_ids,
        }

    def _build_market_data(
        self,
        market_snapshot: dict[str, Any] | None,
        adapter_result: AdapterResult,
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        as_of_date: str,
    ) -> dict[str, Any]:
        """构建市场和估值快照，当前只接收用户稳定来源导出的本地文件。"""

        if not market_snapshot:
            return {
                "price_snapshot": None,
                "valuation_snapshot": None,
                "institutional_holding": None,
                "collection_status": "not_provided",
                "adapter_status": {
                    "adapter": "local_market_valuation_file",
                    "source_path": str(adapter_result.source_path) if adapter_result.source_path else None,
                    "warnings": adapter_result.warnings,
                },
                "limitations": ["未提供本地行情估值文件，行业研究员不能基于本包判断估值高低。"],
                "recommended_next_collection": ["接入用户提供的稳定行情/估值来源导出文件。"],
            }
        ref_id = self._add_source_ref(
            source_refs,
            source_type=adapter_result.source_type,
            source_path=adapter_result.source_path,
            source_detail="selected market valuation snapshot",
            reliability=market_snapshot.get("reliability", "medium"),
            as_of_date=as_of_date,
            limitations=market_snapshot.get("limitations", []),
        )
        evidence_items.append(self._evidence_item(ref_id, "行情估值快照来自本地行情估值文件。", json.dumps(market_snapshot, ensure_ascii=False), adapter_result.source_type, adapter_result.source_path, "selected market valuation snapshot", market_snapshot.get("reliability", "medium"), as_of_date, market_snapshot.get("limitations", [])))
        return {
            "price_snapshot": {
                "price": market_snapshot.get("price"),
                "currency": market_snapshot.get("currency"),
                "as_of_date": market_snapshot.get("as_of_date"),
                "source_refs": [ref_id],
            },
            "valuation_snapshot": {
                "market_cap": market_snapshot.get("market_cap"),
                "pe_ttm": market_snapshot.get("pe_ttm"),
                "pb": market_snapshot.get("pb"),
                "ps_ttm": market_snapshot.get("ps_ttm"),
                "ev_ebitda": market_snapshot.get("ev_ebitda"),
                "dividend_yield": market_snapshot.get("dividend_yield"),
                "source_refs": [ref_id],
            },
            "institutional_holding": market_snapshot.get("institutional_holding"),
            "collection_status": "available_from_local_file",
            "adapter_status": {
                "adapter": "local_market_valuation_file",
                "source_path": str(adapter_result.source_path) if adapter_result.source_path else None,
                "warnings": adapter_result.warnings,
            },
            "limitations": market_snapshot.get("limitations", []),
        }

    def _slugify_identifier(self, value: str) -> str:
        """把行业或代码转成稳定目录名。

        Args:
            value: 原始标识。

        Returns:
            仅包含字母、数字和下划线的稳定字符串；若全部被清洗掉，则退回 hash。
        """

        cleaned = "".join(char if char.isalnum() else "_" for char in value.strip())
        cleaned = cleaned.strip("_")
        return cleaned.upper() if cleaned else hashlib.sha1(value.encode("utf-8")).hexdigest()[:12].upper()

    def _resolve_time_horizon(self, deliverable_type: str, event_study_request: EventStudyRequest | None) -> str:
        """根据交付类型和事件请求确定研究时窗。"""

        if event_study_request and event_study_request.event_window:
            return event_study_request.event_window
        if deliverable_type == "theme_event_study":
            return "事件观察窗口待补充"
        return "1年"

    def _resolve_focus_text(self, deliverable_type: str, event_study_request: EventStudyRequest | None) -> str:
        """生成可读的研究焦点描述。"""

        if deliverable_type == "theme_event_study":
            impact_variables = ", ".join(event_study_request.impact_variables or []) if event_study_request else "事件传导"
            event_name = event_study_request.event_name if event_study_request and event_study_request.event_name else "事件"
            return f"事件研究 | {event_name} | 重点变量：{impact_variables}"
        return "行业归属 | 行业公开统计 | 行业信号 | 政策监管 | 市场估值 | 数据缺口补采"

    def _event_requires_pricing_mechanism(self, impact_variables: list[str], pricing_variable: str | None) -> bool:
        """判断事件研究是否必须解释定价或利润形成机制。"""

        pricing_keywords = {"price", "pricing", "profit", "margin", "cost", "asp", "fee"}
        normalized = {item.strip().lower() for item in impact_variables if item}
        return bool(pricing_variable) or bool(normalized & pricing_keywords)

    def _build_event_study_overlay(
        self,
        industry_name: str,
        as_of_date: str,
        deliverable_type: str,
        event_study_request: EventStudyRequest | None,
        public_stats_records: list[dict[str, Any]],
        industry_signal_records: list[dict[str, Any]],
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        paths: CollectionPaths,
    ) -> dict[str, Any] | None:
        """构建事件研究覆盖层。

        该覆盖层把“事件是什么、事件前行业是什么状态、事件通过什么机制影响行业、
        当前已经观察到什么、还缺什么证据”结构化写进输入包，供下游行业研究员直接消费。
        """

        if deliverable_type != "theme_event_study" and not event_study_request:
            return None

        request = event_study_request or EventStudyRequest()
        impact_variables = request.impact_variables or []
        timeline_payload = self._read_json_if_exists(paths.event_timeline_file)
        impacts_payload = self._read_json_if_exists(paths.event_impacts_file)
        timeline_records = timeline_payload.get("event_timeline", []) if isinstance(timeline_payload, dict) else timeline_payload if isinstance(timeline_payload, list) else []
        observed_payload = impacts_payload.get("observed_impacts", []) if isinstance(impacts_payload, dict) else impacts_payload if isinstance(impacts_payload, list) else []
        expected_payload = impacts_payload.get("expected_impacts", []) if isinstance(impacts_payload, dict) else []
        falsification_payload = impacts_payload.get("falsification_indicators", []) if isinstance(impacts_payload, dict) else []
        transmission_payload = impacts_payload.get("transmission_chain", []) if isinstance(impacts_payload, dict) else []
        baseline_payload = impacts_payload.get("baseline_and_counterfactual", {}) if isinstance(impacts_payload, dict) else {}
        pricing_payload = impacts_payload.get("pricing_mechanism", {}) if isinstance(impacts_payload, dict) else {}

        timeline_ref_ids: list[str] = []
        impacts_ref_ids: list[str] = []
        request_ref_ids: list[str] = []
        if request.event_name or request.event_type:
            ref_id = self._add_source_ref(
                source_refs,
                source_type="cli_event_study_request",
                source_path=None,
                source_detail="event study request from CLI or upstream session",
                reliability="low",
                as_of_date=as_of_date,
                limitations=["该来源只说明研究问题和事件定义，不构成事件已传导的证据。"],
            )
            request_ref_ids.append(ref_id)
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="事件研究请求由 CLI 或上游会话显式给出。",
                    evidence=json.dumps(
                        {
                            "event_name": request.event_name,
                            "event_type": request.event_type,
                            "impact_variables": impact_variables,
                            "pricing_variable": request.pricing_variable,
                            "geography_scope": request.geography_scope,
                            "counterfactual_assumption": request.counterfactual_assumption,
                        },
                        ensure_ascii=False,
                    ),
                    source_type="cli_event_study_request",
                    source_path=None,
                    source_detail="event study request",
                    reliability="low",
                    as_of_date=as_of_date,
                    limitations=["这是研究问题本身，不是行业事实证据。"],
                )
            )
        if paths.event_timeline_file and paths.event_timeline_file.exists():
            ref_id = self._add_source_ref(
                source_refs,
                source_type="local_event_timeline_file",
                source_path=paths.event_timeline_file,
                source_detail="event timeline overlay input",
                reliability="medium",
                as_of_date=as_of_date,
                limitations=["事件时间线需要结合正式公告、政策原文或权威资料复核。"],
            )
            timeline_ref_ids.append(ref_id)
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="事件时间线来自本地事件时间线文件。",
                    evidence=json.dumps(timeline_records, ensure_ascii=False)[:1800],
                    source_type="local_event_timeline_file",
                    source_path=paths.event_timeline_file,
                    source_detail="event_timeline",
                    reliability="medium",
                    as_of_date=as_of_date,
                    limitations=["事件时间线为输入层整理，不等于所有节点都已完成基本面验证。"],
                )
            )
        if paths.event_impacts_file and paths.event_impacts_file.exists():
            ref_id = self._add_source_ref(
                source_refs,
                source_type="local_event_impacts_file",
                source_path=paths.event_impacts_file,
                source_detail="event impacts overlay input",
                reliability="medium",
                as_of_date=as_of_date,
                limitations=["事件影响观测依赖本地整理文件，重要指标仍需回到原始来源核实。"],
            )
            impacts_ref_ids.append(ref_id)
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="事件影响、传导链和证伪指标来自本地事件影响文件。",
                    evidence=json.dumps(impacts_payload, ensure_ascii=False)[:1800],
                    source_type="local_event_impacts_file",
                    source_path=paths.event_impacts_file,
                    source_detail="event_impacts",
                    reliability="medium",
                    as_of_date=as_of_date,
                    limitations=["若本地事件影响文件只包含预期推演，下游必须继续降级。"],
                )
            )

        if not timeline_records and request.event_name:
            timeline_records = [
                {
                    "date": request.event_start_date or as_of_date,
                    "event": request.event_name,
                    "why_it_matters": request.event_description or "事件定义已知，但关键节点仍待继续补证。",
                    "affected_channel": ", ".join(impact_variables) if impact_variables else "unknown",
                }
            ]

        event_timeline = [
            {
                "date": record.get("date") or record.get("event_date") or request.event_start_date or "unknown",
                "event": record.get("event") or record.get("title") or record.get("event_name") or request.event_name or "未命名事件",
                "why_it_matters": record.get("why_it_matters") or record.get("summary") or record.get("description") or "需要结合行业变量解释其重要性。",
                "affected_channel": record.get("affected_channel") or record.get("channel") or (", ".join(impact_variables) if impact_variables else "unknown"),
                "source_refs": list(dict.fromkeys(record.get("source_refs", []) + timeline_ref_ids + request_ref_ids)),
            }
            for record in timeline_records
        ]

        pre_event_baseline = baseline_payload.get("pre_event_baseline", {}) if isinstance(baseline_payload, dict) else {}
        counterfactual_without_event = baseline_payload.get("counterfactual_without_event", {}) if isinstance(baseline_payload, dict) else {}
        baseline_and_counterfactual = {
            "pre_event_baseline": {
                "demand_state": pre_event_baseline.get("demand_state") or "unknown",
                "supply_state": pre_event_baseline.get("supply_state") or "unknown",
                "price_state": pre_event_baseline.get("price_state") or "unknown",
                "inventory_state": pre_event_baseline.get("inventory_state") or "unknown",
                "profit_state": pre_event_baseline.get("profit_state") or "unknown",
                "baseline_period": pre_event_baseline.get("baseline_period") or request.baseline_period or "unknown",
                "source_refs": pre_event_baseline.get("source_refs", []) + impacts_ref_ids + request_ref_ids,
            },
            "counterfactual_without_event": {
                "expected_demand_path": counterfactual_without_event.get("expected_demand_path") or request.counterfactual_assumption or "unknown",
                "expected_supply_path": counterfactual_without_event.get("expected_supply_path") or "unknown",
                "expected_price_path": counterfactual_without_event.get("expected_price_path") or "unknown",
                "key_assumptions": counterfactual_without_event.get("key_assumptions", []),
                "confidence": counterfactual_without_event.get("confidence") or "低",
            },
        }

        transmission_chain = []
        if isinstance(transmission_payload, list) and transmission_payload:
            transmission_chain = [
                {
                    "step": item.get("step") or index + 1,
                    "from": item.get("from") or item.get("source") or request.event_name or "外部事件",
                    "to": item.get("to") or item.get("target") or industry_name,
                    "channel": item.get("channel") or "unknown",
                    "mechanism": item.get("mechanism") or item.get("description") or "unknown",
                    "expected_direction": item.get("expected_direction") or item.get("direction") or "unknown",
                    "expected_lag": item.get("expected_lag") or item.get("lag") or "unknown",
                    "evidence_status": item.get("evidence_status") or "expected",
                    "observed_evidence_refs": item.get("observed_evidence_refs", []) + impacts_ref_ids + request_ref_ids,
                    "missing_evidence": item.get("missing_evidence", []),
                }
                for index, item in enumerate(transmission_payload)
            ]
        elif impact_variables:
            transmission_chain = [
                {
                    "step": index + 1,
                    "from": request.event_name or "外部事件",
                    "to": variable,
                    "channel": variable,
                    "mechanism": f"需要继续验证事件如何通过 {variable} 这一环节传导到 {industry_name}。",
                    "expected_direction": "unknown",
                    "expected_lag": request.event_window or "unknown",
                    "evidence_status": "expected",
                    "observed_evidence_refs": impacts_ref_ids + request_ref_ids,
                    "missing_evidence": [f"缺少 {variable} 的事件后观测数据。"],
                }
                for index, variable in enumerate(impact_variables)
            ]

        pricing_mechanism: dict[str, Any] | None = None
        if self._event_requires_pricing_mechanism(impact_variables, request.pricing_variable):
            pricing_mechanism = {
                "is_price_relevant": True,
                "price_variable": pricing_payload.get("price_variable") or request.pricing_variable or "unknown",
                "price_formation_mechanism": pricing_payload.get("price_formation_mechanism") or "unknown",
                "cost_pass_through": pricing_payload.get("cost_pass_through") or "unknown",
                "contract_lag": pricing_payload.get("contract_lag") or "unknown",
                "substitute_constraint": pricing_payload.get("substitute_constraint") or "unknown",
                "price_observation_sources": pricing_payload.get("price_observation_sources", []) + impacts_ref_ids,
                "price_data_gaps": pricing_payload.get("price_data_gaps", []),
            }

        observed_impacts = [
            {
                "variable_name": item.get("variable_name") or item.get("metric_name") or "unknown",
                "current_value": item.get("current_value", item.get("value", "unknown")),
                "unit": item.get("unit"),
                "as_of_date": item.get("as_of_date") or as_of_date,
                "pre_event_value": item.get("pre_event_value"),
                "change_since_event": item.get("change_since_event") or "unknown",
                "source_refs": item.get("source_refs", []) + impacts_ref_ids + request_ref_ids,
                "confidence": item.get("confidence") or "低",
            }
            for item in observed_payload
        ]
        if not observed_impacts and impact_variables:
            observed_impacts = [
                {
                    "variable_name": variable,
                    "current_value": "unknown",
                    "unit": None,
                    "as_of_date": as_of_date,
                    "pre_event_value": None,
                    "change_since_event": "unknown",
                    "source_refs": impacts_ref_ids + request_ref_ids,
                    "confidence": "低",
                }
                for variable in impact_variables
            ]

        expected_impacts = [
            {
                "variable_name": item.get("variable_name") or item.get("metric_name") or "unknown",
                "expected_direction": item.get("expected_direction") or item.get("direction") or "unknown",
                "expected_magnitude": item.get("expected_magnitude") or "unknown",
                "expected_time_window": item.get("expected_time_window") or request.event_window or "unknown",
                "reasoning": item.get("reasoning") or item.get("summary") or "unknown",
                "confidence": item.get("confidence") or "低",
            }
            for item in expected_payload
        ]
        if not expected_impacts and impact_variables:
            expected_impacts = [
                {
                    "variable_name": variable,
                    "expected_direction": "unknown",
                    "expected_magnitude": "unknown",
                    "expected_time_window": request.event_window or "unknown",
                    "reasoning": f"需要继续验证 {request.event_name or '该事件'} 是否通过 {variable} 传导到行业。",
                    "confidence": "低",
                }
                for variable in impact_variables
            ]

        has_observed_numeric_impact = any(item.get("current_value") not in {None, "", "unknown"} for item in observed_impacts)
        impact_classification = "confirmed_fundamental" if has_observed_numeric_impact else "insufficient_evidence"

        falsification_indicators = [
            {
                "indicator": item.get("indicator") or item.get("variable_name") or item.get("metric_name") or "unknown",
                "direction_or_threshold": item.get("direction_or_threshold") or item.get("threshold") or "unknown",
                "observation_window": item.get("observation_window") or request.event_window or "unknown",
                "data_source": item.get("data_source") or "unknown",
                "would_falsify": item.get("would_falsify") or "需要结合具体研究判断补充。",
                "current_status": item.get("current_status") or "not_started",
            }
            for item in falsification_payload
        ]
        if not falsification_indicators and impact_variables:
            falsification_indicators = [
                {
                    "indicator": variable,
                    "direction_or_threshold": "unknown",
                    "observation_window": request.event_window or "unknown",
                    "data_source": "unknown",
                    "would_falsify": f"若 {variable} 长时间无变化，则事件影响可能仍停留在预期层。",
                    "current_status": "not_started",
                }
                for variable in impact_variables
            ]

        event_specific_gaps = []
        if not request.event_name:
            event_specific_gaps.append(
                {
                    "gap": "缺少明确事件名称或事件定义。",
                    "blocks_which_judgement": "无法稳定界定研究对象和事件边界。",
                    "suggested_source": "主会话补充事件定义或本地事件清单。",
                    "fallback_proxy": "无",
                    "severity": "high",
                }
            )
        if not event_timeline:
            event_specific_gaps.append(
                {
                    "gap": "缺少关键事件时间线。",
                    "blocks_which_judgement": "无法判断事件何时开始影响行业，以及哪些节点是新增变量。",
                    "suggested_source": "政策原文、公告、新闻时间线、本地事件文件。",
                    "fallback_proxy": "只保留事件假设，不下已传导结论。",
                    "severity": "high",
                }
            )
        if not transmission_chain:
            event_specific_gaps.append(
                {
                    "gap": "缺少事件到行业变量的传导链。",
                    "blocks_which_judgement": "无法判断事件如何影响供给、需求、价格或利润。",
                    "suggested_source": "行业研究员手工梳理传导链或本地事件影响文件。",
                    "fallback_proxy": "仅保留背景描述。",
                    "severity": "high",
                }
            )
        if self._event_requires_pricing_mechanism(impact_variables, request.pricing_variable) and not pricing_mechanism:
            event_specific_gaps.append(
                {
                    "gap": "缺少定价或利润形成机制。",
                    "blocks_which_judgement": "无法把事件和价格、ASP、费率、毛利率或利润联系起来。",
                    "suggested_source": "价格机制说明、招投标规则、长协机制、成本传导资料。",
                    "fallback_proxy": "仅判断供需或物流，不下价格/利润结论。",
                    "severity": "high",
                }
            )
        if not has_observed_numeric_impact:
            event_specific_gaps.append(
                {
                    "gap": "缺少事件后的可观察落地变量。",
                    "blocks_which_judgement": "无法确认事件影响已经兑现到基本面。",
                    "suggested_source": "价格、库存、产量、进口量、订单、招标量、毛利率等事件后数据。",
                    "fallback_proxy": "降级为事件驱动假设待验证。",
                    "severity": "high",
                }
            )

        return {
            "event_metadata": {
                "event_name": request.event_name,
                "event_type": request.event_type,
                "event_description": request.event_description,
                "event_start_date": request.event_start_date or "unknown",
                "event_end_date": request.event_end_date or "unknown",
                "event_status": request.event_status or "unknown",
                "geography_scope": request.geography_scope or "unknown",
                "affected_industry": industry_name,
                "affected_segments": request.affected_segments or [],
                "as_of_date": as_of_date,
            },
            "baseline_and_counterfactual": baseline_and_counterfactual,
            "event_timeline": event_timeline,
            "transmission_chain": transmission_chain,
            "pricing_mechanism": pricing_mechanism,
            "observed_vs_expected_impacts": {
                "observed_impacts": observed_impacts,
                "expected_impacts": expected_impacts,
                "impact_classification": impact_classification,
            },
            "falsification_indicators": falsification_indicators,
            "event_specific_gaps": event_specific_gaps,
        }

    def _build_quantitative_variable_table(
        self,
        public_stats_records: list[dict[str, Any]],
        industry_signal_records: list[dict[str, Any]],
        market_snapshot: dict[str, Any] | None,
        event_study: dict[str, Any] | None,
        as_of_date: str,
    ) -> list[dict[str, Any]]:
        """统一生成核心量化变量表。

        第一版优先复用本地公开统计、行业信号和事件观测变量，若数值缺失则明确写 unknown，
        不用空表假装已经量化完成。
        """

        variables: list[dict[str, Any]] = []
        for record in public_stats_records[:8]:
            variables.append(
                {
                    "variable_name": record.get("metric_name") or record.get("name") or "unknown",
                    "current_value": record.get("value", "unknown"),
                    "unit": record.get("unit"),
                    "as_of_date": record.get("as_of_date") or record.get("period") or as_of_date,
                    "yoy": record.get("yoy", "unknown"),
                    "qoq_or_mom": record.get("qoq") or record.get("mom") or "unknown",
                    "history_window": record.get("history_window") or record.get("period") or "unknown",
                    "percentile_or_range_position": record.get("percentile_or_range_position") or "unknown",
                    "source_path_or_url": record.get("source_refs", []),
                    "coverage_note": record.get("coverage") or record.get("summary") or "行业公开统计",
                    "confidence": record.get("reliability") or "medium",
                    "gap_reason": (record.get("limitations") or [None])[0],
                }
            )
        for record in industry_signal_records[:8]:
            variables.append(
                {
                    "variable_name": record.get("metric_name") or record.get("name") or "unknown",
                    "current_value": record.get("value", "unknown"),
                    "unit": record.get("unit"),
                    "as_of_date": record.get("as_of_date") or as_of_date,
                    "yoy": record.get("yoy", "unknown"),
                    "qoq_or_mom": record.get("mom") or record.get("qoq") or record.get("direction") or "unknown",
                    "history_window": record.get("history_window") or "unknown",
                    "percentile_or_range_position": record.get("percentile_or_range_position") or "unknown",
                    "source_path_or_url": record.get("source_refs", []),
                    "coverage_note": record.get("summary") or "行业信号",
                    "confidence": record.get("reliability") or "medium",
                    "gap_reason": (record.get("limitations") or [None])[0],
                }
            )
        if event_study:
            for item in event_study.get("observed_vs_expected_impacts", {}).get("observed_impacts", [])[:6]:
                variables.append(
                    {
                        "variable_name": item.get("variable_name") or "unknown",
                        "current_value": item.get("current_value", "unknown"),
                        "unit": item.get("unit"),
                        "as_of_date": item.get("as_of_date") or as_of_date,
                        "yoy": "unknown",
                        "qoq_or_mom": item.get("change_since_event") or "unknown",
                        "history_window": event_study.get("baseline_and_counterfactual", {}).get("pre_event_baseline", {}).get("baseline_period") or "unknown",
                        "percentile_or_range_position": "unknown",
                        "source_path_or_url": item.get("source_refs", []),
                        "coverage_note": "事件后观测变量",
                        "confidence": item.get("confidence") or "low",
                        "gap_reason": None if item.get("current_value") not in {None, "", "unknown"} else "尚未取得事件后可观察数值。",
                    }
                )
        if market_snapshot:
            variables.append(
                {
                    "variable_name": "market_cap",
                    "current_value": market_snapshot.get("market_cap", "unknown"),
                    "unit": market_snapshot.get("currency"),
                    "as_of_date": market_snapshot.get("as_of_date") or as_of_date,
                    "yoy": "unknown",
                    "qoq_or_mom": "unknown",
                    "history_window": "unknown",
                    "percentile_or_range_position": "unknown",
                    "source_path_or_url": [],
                    "coverage_note": "本地行情估值快照",
                    "confidence": market_snapshot.get("reliability") or "medium",
                    "gap_reason": None,
                }
            )
        deduped: list[dict[str, Any]] = []
        seen = set()
        for item in variables:
            key = (item.get("variable_name"), item.get("as_of_date"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _build_general_gaps(
        self,
        deliverable_type: str,
        public_stats: list[dict[str, Any]],
        industry_signals: list[dict[str, Any]],
        policy_and_regulation: list[dict[str, Any]],
        company_events: list[dict[str, Any]],
        event_study: dict[str, Any] | None,
        market_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """生成统一 gap 列表。

        这里不直接下行业结论，而是说明缺什么、会卡住哪条判断、可用什么替代。
        """

        gaps: list[dict[str, Any]] = []
        if not public_stats:
            gaps.append(
                {
                    "gap": "缺少行业公开统计序列。",
                    "blocks_which_judgement": "无法稳定判断行业总量、历史位置和周期阶段。",
                    "suggested_source": "统计局、协会、海关、行业数据库或用户本地统计文件。",
                    "fallback_proxy": "局部样本或公司口径只能作为弱代理。",
                    "severity": "high",
                }
            )
        if not industry_signals:
            gaps.append(
                {
                    "gap": "缺少行业信号数据。",
                    "blocks_which_judgement": "难以跟踪价格、库存、需求、供给或渠道的高频变化。",
                    "suggested_source": "价格、库存、供需、技术或渠道的本地信号文件。",
                    "fallback_proxy": "新闻和管理层口径只能作为辅助线索。",
                    "severity": "medium",
                }
            )
        if not policy_and_regulation:
            gaps.append(
                {
                    "gap": "缺少政策/监管原文。",
                    "blocks_which_judgement": "无法确认政策、监管、集采或制裁到底作用到哪个环节。",
                    "suggested_source": "政策原文、监管文件、集采规则、出口限制文件。",
                    "fallback_proxy": "二手转述只能支持低置信判断。",
                    "severity": "medium",
                }
            )
        if market_data.get("collection_status") != "available_from_local_file":
            gaps.append(
                {
                    "gap": "缺少稳定行情估值快照。",
                    "blocks_which_judgement": "无法基于本包判断估值位置或市场映射。",
                    "suggested_source": "用户稳定行情/估值导出文件。",
                    "fallback_proxy": "仅做基本面研究，不讨论估值。",
                    "severity": "low",
                }
            )
        if deliverable_type == "theme_event_study" and event_study:
            gaps.extend(event_study.get("event_specific_gaps", []))
        if deliverable_type != "theme_event_study" and not company_events:
            gaps.append(
                {
                    "gap": "缺少公司事件验证材料。",
                    "blocks_which_judgement": "难以用公司行为验证行业边际变化。",
                    "suggested_source": "公司事件、经营公告、投资者关系记录。",
                    "fallback_proxy": "保持行业层研究，不做强公司映射。",
                    "severity": "low",
                }
            )
        return gaps

    def _build_package_quality_gate(
        self,
        deliverable_type: str,
        quantitative_variable_table: list[dict[str, Any]],
        policy_and_regulation: list[dict[str, Any]],
        public_stats: list[dict[str, Any]],
        industry_signals: list[dict[str, Any]],
        competitors: list[dict[str, Any]],
        event_study: dict[str, Any] | None,
        has_company_context: bool,
        as_of_date: str,
        gaps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """生成输入包质量 Gate。"""

        quant_variable_count = sum(1 for item in quantitative_variable_table if item.get("current_value") not in {None, "", "unknown"})
        evidence_categories = 0
        if public_stats or industry_signals:
            evidence_categories += 1
        if any(self._contains_keywords(record, ["供给", "产量", "开工", "库存", "产能", "进口", "出口"]) for record in public_stats + industry_signals):
            evidence_categories += 1
        if any(self._contains_keywords(record, ["价格", "价差", "费率", "毛利", "利润", "asp", "中标", "运价"]) for record in public_stats + industry_signals):
            evidence_categories += 1
        if competitors:
            evidence_categories += 1
        if policy_and_regulation or (event_study and event_study.get("event_timeline")):
            evidence_categories += 1

        if evidence_categories >= 4:
            coverage = "高"
        elif evidence_categories >= 2:
            coverage = "中"
        else:
            coverage = "低"

        if has_company_context and not public_stats and not industry_signals and not policy_and_regulation:
            company_evidence_ratio = "高"
        elif has_company_context:
            company_evidence_ratio = "中"
        else:
            company_evidence_ratio = "低"

        theme_event_study_gate = self._build_theme_event_study_gate(event_study) if deliverable_type == "theme_event_study" else None
        can_support_full_research = quant_variable_count >= 3 and coverage != "低"
        downgrade_reason = ""
        if deliverable_type == "theme_event_study":
            can_support_full_research = can_support_full_research and bool(theme_event_study_gate and theme_event_study_gate.get("has_observed_impact_variable")) and bool(theme_event_study_gate and theme_event_study_gate.get("minimum_passed"))
            downgrade_reason = theme_event_study_gate.get("downgrade_reason") if theme_event_study_gate else "事件研究覆盖层缺失。"
        else:
            if quant_variable_count < 3:
                downgrade_reason = "核心量化变量少于 3 个。"
            elif coverage == "低":
                downgrade_reason = "行业层证据覆盖度偏低。"
            elif company_evidence_ratio == "高":
                downgrade_reason = "公司材料占比过高，行业层证据不足。"

        if can_support_full_research:
            downgrade_reason = ""
        if not downgrade_reason and gaps:
            downgrade_reason = gaps[0].get("gap", "关键证据仍有缺口。")

        gate = {
            "industry_evidence_coverage": coverage,
            "quant_variable_count": quant_variable_count,
            "freshness_check": f"关键输入按 {as_of_date} 视角整理；是否为最新口径需结合源文件日期复核。",
            "source_diversity": "中" if public_stats or industry_signals or policy_and_regulation else "低",
            "company_evidence_ratio": company_evidence_ratio,
            "can_support_full_research": can_support_full_research,
            "downgrade_reason": downgrade_reason or "",
        }
        if theme_event_study_gate is not None:
            gate["theme_event_study_gate"] = theme_event_study_gate
        return gate

    def _build_theme_event_study_gate(self, event_study: dict[str, Any] | None) -> dict[str, Any]:
        """生成事件研究最低成熟度 Gate。"""

        event_study = event_study or {}
        pricing_mechanism = event_study.get("pricing_mechanism")
        observed_impacts = event_study.get("observed_vs_expected_impacts", {}).get("observed_impacts", [])
        has_observed_impact_variable = any(item.get("current_value") not in {None, "", "unknown"} for item in observed_impacts)
        minimum_passed = bool(event_study.get("event_timeline")) and bool(event_study.get("transmission_chain")) and bool(event_study.get("baseline_and_counterfactual")) and bool(event_study.get("falsification_indicators"))
        requires_pricing_mechanism = pricing_mechanism is not None or any(
            impact.get("variable_name", "").lower() in {"price", "profit", "margin", "cost", "asp", "fee"}
            for impact in observed_impacts
        )
        has_pricing_mechanism_when_needed = not requires_pricing_mechanism or bool(pricing_mechanism)
        downgrade_reasons = []
        if not event_study.get("event_timeline"):
            downgrade_reasons.append("缺少关键事件时间线。")
        if not event_study.get("transmission_chain"):
            downgrade_reasons.append("缺少事件传导链。")
        if not event_study.get("baseline_and_counterfactual"):
            downgrade_reasons.append("缺少事件前基线或反事实。")
        if not event_study.get("falsification_indicators"):
            downgrade_reasons.append("缺少证伪指标。")
        if requires_pricing_mechanism and not pricing_mechanism:
            downgrade_reasons.append("价格/利润是重点，但缺少定价或利润形成机制。")
        if not has_observed_impact_variable:
            downgrade_reasons.append("缺少事件后的可观察落地变量。")
        return {
            "has_event_timeline": bool(event_study.get("event_timeline")),
            "has_transmission_chain": bool(event_study.get("transmission_chain")),
            "has_baseline_or_counterfactual": bool(event_study.get("baseline_and_counterfactual")),
            "has_pricing_mechanism_when_needed": has_pricing_mechanism_when_needed,
            "has_falsification_indicator": bool(event_study.get("falsification_indicators")),
            "has_observed_impact_variable": has_observed_impact_variable,
            "minimum_passed": minimum_passed and has_pricing_mechanism_when_needed,
            "downgrade_reason": "；".join(downgrade_reasons),
        }

    def _contains_keywords(self, record: dict[str, Any], keywords: list[str]) -> bool:
        """基于记录文本粗略判断其更接近哪类行业变量。"""

        text = json.dumps(record, ensure_ascii=False).lower()
        return any(keyword.lower() in text for keyword in keywords)

    def _records_with_source(
        self,
        records: list[dict[str, Any]],
        adapter_result: AdapterResult,
        claim: str,
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        as_of_date: str,
    ) -> list[dict[str, Any]]:
        """把适配器记录写入来源引用和证据表。"""

        if not records:
            return []
        ref_id = self._add_source_ref(
            source_refs,
            source_type=adapter_result.source_type,
            source_path=adapter_result.source_path,
            source_detail=claim,
            reliability=self._aggregate_reliability(records),
            as_of_date=as_of_date,
            limitations=self._collect_record_limitations(records),
        )
        evidence_items.append(
            self._evidence_item(
                ref_id=ref_id,
                claim=claim,
                evidence=json.dumps(records, ensure_ascii=False)[:1800],
                source_type=adapter_result.source_type,
                source_path=adapter_result.source_path,
                source_detail=claim,
                reliability=self._aggregate_reliability(records),
                as_of_date=as_of_date,
                limitations=self._collect_record_limitations(records),
            )
        )
        return [{**record, "source_refs": list(dict.fromkeys(record.get("source_refs", []) + [ref_id]))} for record in records]

    def _seed_list_with_source(
        self,
        items: list[dict[str, Any]],
        seed_path: Path,
        claim: str,
        source_type: str,
        source_refs: list[dict[str, Any]],
        evidence_items: list[dict[str, Any]],
        as_of_date: str,
    ) -> list[dict[str, Any]]:
        """为 seed 列表批量补充来源引用。"""

        if not items:
            return []
        ref_id = self._add_source_ref(source_refs, source_type, seed_path, claim, "low", as_of_date, ["该列表为离线 seed 指针，后续需要权威来源补采。"])
        evidence_items.append(self._evidence_item(ref_id, claim, json.dumps(items, ensure_ascii=False), source_type, seed_path, claim, "low", as_of_date, ["不是实时权威数据。"] ))
        return [{**item, "source_refs": [ref_id]} for item in items]

    def _build_management_commentary(self, financial_report: Any) -> list[dict[str, Any]]:
        """整理管理层观点入口；当前优先提示回到财务分析员报告和年报原文。"""

        if not isinstance(financial_report, dict):
            return []
        growth = financial_report.get("growth_and_outlook", {})
        return [
            {
                "topic": "财务分析员报告中的成长和展望字段",
                "summary": self._shorten(json.dumps(growth, ensure_ascii=False), 800),
                "limitations": ["该内容来自财务分析员报告摘要，行业研究员应回到年报管理层讨论与分析章节核验。"],
            }
        ]

    def _build_financial_analysis_ref(self, financial_report: Any, path: Path | None) -> dict[str, Any]:
        """整理财务分析员报告入口，供行业研究员理解其可用性和限制。"""

        if not isinstance(financial_report, dict):
            return {"available": False, "source_path": str(path) if path else None, "limitations": ["未找到财务分析员报告。"]}
        return {
            "available": True,
            "source_path": str(path),
            "summary": financial_report.get("executive_summary", {}).get("overall_view"),
            "key_findings": financial_report.get("executive_summary", {}).get("key_reasons", []),
            "risks": financial_report.get("risks", []),
            "input_quality": financial_report.get("input_audit", {}).get("input_quality"),
            "limitations": financial_report.get("input_audit", {}).get("limitations", []),
        }

    def _find_annual_report_source(self, financial_manifest: Any, stock_code: str, fiscal_year: str) -> dict[str, Any] | None:
        """从原财报收集员 manifest 中定位目标年报记录。"""

        if not isinstance(financial_manifest, list):
            return None
        for record in financial_manifest:
            if (
                str(record.get("stock_code")) == str(stock_code)
                and str(record.get("report_year")) == str(fiscal_year)
                and record.get("report_type") == "annual"
                and record.get("title_classification") in {"annual_full", None, ""}
            ):
                return {
                    "announcement_id": record.get("announcement_id"),
                    "title": record.get("title"),
                    "published_at": record.get("published_at"),
                    "source_pdf_url": record.get("source_pdf_url"),
                    "local_relative_path": record.get("local_relative_path"),
                    "download_status": record.get("download_status"),
                }
        return None

    def _build_limitations(
        self,
        offline: bool,
        financial_report: Any,
        seed_data: dict[str, Any],
        industry_classification: dict[str, Any],
        market_data: dict[str, Any],
    ) -> list[str]:
        """统一生成限制条件，防止下游过度解读输入包。"""

        limitations = []
        if offline:
            limitations.append("本次为离线模式，未主动抓取网页、新闻或外部接口。")
        if not isinstance(financial_report, dict):
            limitations.append("未找到财务分析员报告，财务摘要不足。")
        if not seed_data:
            limitations.append("未找到行业 seed 数据，行业分类、同行候选和产业链信息可能不足。")
        if industry_classification.get("primary_industry") == "未知行业":
            limitations.append("未提供可靠行业分类，行业研究员需要先确认真实行业归属。")
        if market_data.get("collection_status") != "available_from_local_file":
            limitations.append("未提供行情估值文件，不能基于本包判断估值高低。")
        limitations.extend(
            [
                "同行候选如存在，仅作为研究线索，不等于最终估值可比公司；同行年报和财务分析由原信息收集员与财务分析员负责。",
                "行业指标、政策、统计和信号均为行业研究输入，不构成自动行业结论。",
            ]
        )
        return list(dict.fromkeys(limitations))

    def _build_recommended_next_collection(
        self,
        seed_industry: dict[str, Any],
        public_stats: list[dict[str, Any]],
        industry_signals: list[dict[str, Any]],
        company_events: list[dict[str, Any]],
        policy_and_regulation: list[dict[str, Any]],
        market_data: dict[str, Any],
        event_study: dict[str, Any] | None = None,
    ) -> list[str]:
        """根据缺口生成通用补采建议，不重复要求同行年报和同行财务分析。"""

        recommendations = list(seed_industry.get("recommended_next_collection", []))
        if not public_stats:
            recommendations.append("补充行业公开统计数据，例如产量、销量、价格、库存、进出口、渗透率或行业收入利润等适用于该行业的指标。")
        if not industry_signals:
            recommendations.append("补充行业信号数据，例如价格、需求、库存、渠道、供给、政策或技术变化。")
        if not company_events:
            recommendations.append("补充公司事件、非财报公告、投资者关系记录或经营事项。")
        if not policy_and_regulation:
            recommendations.append("补充行业相关政策、监管文件或合规要求原文。")
        if market_data.get("collection_status") != "available_from_local_file":
            recommendations.append("接入用户提供的稳定行情/估值来源，补充价格、市值、估值和股息率快照。")
        if event_study:
            if not event_study.get("event_timeline"):
                recommendations.append("补充关键事件时间线，明确事件起点、升级节点和政策落地节点。")
            if not event_study.get("transmission_chain"):
                recommendations.append("补充事件到供给、需求、物流、价格或利润的传导链。")
            if not event_study.get("observed_vs_expected_impacts", {}).get("observed_impacts"):
                recommendations.append("补充事件后的可观察变量，例如价格、库存、进口量、订单、招标量或毛利率。")
            if self._event_requires_pricing_mechanism(
                [item.get("variable_name") for item in event_study.get("observed_vs_expected_impacts", {}).get("observed_impacts", []) if item.get("variable_name")],
                (event_study.get("pricing_mechanism") or {}).get("price_variable") if event_study.get("pricing_mechanism") else None,
            ) and not event_study.get("pricing_mechanism"):
                recommendations.append("补充定价或利润形成机制，解释事件如何影响价格、费率、ASP 或利润。")
        return list(dict.fromkeys(recommendations))

    def _build_audit(
        self,
        package_id: str,
        paths: CollectionPaths,
        package: dict[str, Any],
        offline: bool,
        financial_report: Any,
        seed_data: dict[str, Any],
        adapter_results: dict[str, AdapterResult],
        deliverable_type: str,
        event_study: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """生成收集审计，用于说明输入质量和数据缺口。"""

        info = package["information_package"]
        input_files = {
            "financial_analysis_report": self._file_audit(paths.financial_analysis_report),
            "processor_content_json": self._file_audit(paths.processor_content_json),
            "financial_manifest": self._file_audit(paths.financial_manifest),
            "seed_data": self._file_audit(paths.seed_data),
            "company_events_file": self._file_audit(paths.company_events_file),
            "policy_regulation_file": self._file_audit(paths.policy_regulation_file),
            "industry_public_stats_file": self._file_audit(paths.industry_public_stats_file),
            "industry_signals_file": self._file_audit(paths.industry_signals_file),
            "market_valuation_file": self._file_audit(paths.market_valuation_file),
            "event_timeline_file": self._file_audit(paths.event_timeline_file),
            "event_impacts_file": self._file_audit(paths.event_impacts_file),
        }
        classification = info["industry_classification"]
        missing_core = [name for name in ["financial_analysis_report", "processor_content_json"] if not input_files[name]["exists"]]
        input_quality = "medium"
        if missing_core or classification.get("primary_industry") == "未知行业":
            input_quality = "low"
        market_reliable = self._market_snapshot_has_values(info["market_data"])
        stats_reliable = any(stat.get("value") not in {None, ""} and stat.get("reliability") != "low" for stat in info["industry_data"].get("public_stats", []))
        if market_reliable and stats_reliable:
            input_quality = "high" if not missing_core else "medium"
        ready = bool(info["company_profile"]) and bool(classification.get("primary_industry"))
        if deliverable_type == "theme_event_study":
            ready = ready and bool(event_study)
        return {
            "schema_version": "1.1",
            "package_id": package_id,
            "generated_at": package["generated_at"],
            "offline_mode": offline,
            "network_attempted": False,
            "input_files": input_files,
            "adapter_warnings": {name: result.warnings for name, result in adapter_results.items() if result.warnings},
            "data_availability": {
                "company_profile": "available" if info["company_profile"] else "missing",
                "financial_summary": "available" if isinstance(financial_report, dict) else "missing",
                "business_segments": "available" if info["business_segments"] and info["business_segments"][0].get("segment_name") != "未结构化提取" else "partial",
                "industry_classification": "available" if classification.get("primary_industry") != "未知行业" else "missing",
                "peer_candidates": "available_seed" if info["competitors"] else "not_collected_by_design_or_missing",
                "company_events": "available_local_file" if info["company_events"] else "missing",
                "public_stats": "available_local_file" if info["industry_data"].get("public_stats") else "missing",
                "industry_signals": "available_local_file" if info["industry_data"].get("industry_signals") else "missing",
                "policy_and_regulation": "available_local_file_or_seed" if info["policy_and_regulation"] else "missing",
                "market_data": "available_local_file" if info["market_data"].get("collection_status") == "available_from_local_file" else "not_provided",
                "event_study": "available" if event_study else "not_applicable",
            },
            "deliverable_type": deliverable_type,
            "input_quality": input_quality,
            "ready_for_industry_researcher": ready,
            "limitations": package["limitations"],
            "recommended_next_collection": package["recommended_next_collection"],
        }

    def _file_audit(self, path: Path | None) -> dict[str, Any]:
        """记录输入文件存在性和 hash，方便后续复核同一输入是否变化。"""

        if path is None:
            return {"path": None, "exists": False, "sha256": None}
        return {"path": str(path), "exists": path.exists(), "sha256": self._sha256(path) if path.exists() else None}

    def _manifest_record(
        self,
        package_id: str,
        package: dict[str, Any],
        audit: dict[str, Any],
        output_dir: Path,
        package_path: Path,
        markdown_path: Path,
        evidence_path: Path,
        audit_path: Path,
    ) -> dict[str, Any]:
        """生成行业输入包总清单记录。"""

        classification = package["information_package"]["industry_classification"]
        rel = lambda path: str(path.relative_to(self.workspace)).replace("\\", "/")
        source_modes = [
            key.replace("_file", "") if key.endswith("_file") else key
            for key, item in audit["input_files"].items()
            if item.get("exists")
        ]
        return {
            "package_id": package_id,
            "stock_code": package["company"]["ticker"] or package.get("target", {}).get("industry_name") or "unknown",
            "company_name": package["company"]["name"],
            "fiscal_year": package["company"]["fiscal_year"],
            "as_of_date": package["company"].get("as_of_date") or package.get("target", {}).get("as_of_date"),
            "primary_industry": classification.get("primary_industry"),
            "secondary_industry": classification.get("secondary_industry"),
            "deliverable_type": package.get("deliverable_type"),
            "event_name": package.get("event_study", {}).get("event_metadata", {}).get("event_name") if package.get("event_study") else None,
            "event_type": package.get("event_study", {}).get("event_metadata", {}).get("event_type") if package.get("event_study") else None,
            "package_relative_path": rel(package_path),
            "markdown_relative_path": rel(markdown_path),
            "evidence_relative_path": rel(evidence_path),
            "audit_relative_path": rel(audit_path),
            "source_modes": source_modes,
            "offline_mode": audit["offline_mode"],
            "input_quality": audit["input_quality"],
            "ready_for_industry_researcher": audit["ready_for_industry_researcher"],
            "generated_at": package["generated_at"],
            "limitations_count": len(package["limitations"]),
            "output_dir": rel(output_dir),
        }

    def _update_manifest(self, record: dict[str, Any]) -> None:
        """维护 JSON / CSV 双格式总清单，同 package_id 重跑时更新同一条记录。"""

        json_path = self.manifest_dir / "industry_packages.json"
        csv_path = self.manifest_dir / "industry_packages.csv"
        records = []
        if json_path.exists():
            records = json.loads(json_path.read_text(encoding="utf-8"))
        by_id = {item["package_id"]: item for item in records}
        by_id[record["package_id"]] = record
        records = sorted(by_id.values(), key=lambda item: (str(item.get("stock_code") or ""), str(item.get("as_of_date") or ""), item["package_id"]))
        self._write_json(json_path, records)
        fieldnames = list(record.keys())
        with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for item in records:
                writer.writerow(item)

    def _render_package_markdown(self, package: dict[str, Any], audit: dict[str, Any]) -> str:
        """渲染人类可读输入包摘要。"""

        company = package["company"]
        info = package["information_package"]
        classification = info["industry_classification"]
        financial = info["financial_summary"]
        market_status = info["market_data"].get("collection_status")
        target = package.get("target", {})
        title_name = company.get("name") or target.get("industry_name") or "未命名行业"
        lines = [
            f"# {title_name} 行业研究输入包",
            "",
            "## 1. 基本信息",
            f"- 目标行业：{target.get('industry_name', classification.get('primary_industry'))}",
            f"- 交付类型：{package.get('deliverable_type')}",
            f"- 股票代码：{company.get('ticker')}",
            f"- 财年：{company.get('fiscal_year')}",
            f"- 生成日期：{company.get('as_of_date') or target.get('as_of_date')}",
            f"- 输入质量：{audit['input_quality']}",
            f"- 是否可交给行业研究员：{audit['ready_for_industry_researcher']}",
            "",
            "## 2. 初始行业分类",
            f"- 主要行业：{classification.get('primary_industry')}",
            f"- 细分行业：{classification.get('secondary_industry')}",
            "- 分类依据：",
        ]
        lines.extend([f"  - {item}" for item in classification.get("classification_basis", [])] or ["  - 暂无分类依据，需要补充。"])
        lines.extend(["", "## 3. 核心财务摘要"])
        for key in ["revenue", "net_profit_attributable", "deducted_net_profit", "operating_cash_flow", "roe"]:
            metric = financial.get(key, {})
            current = metric.get("current") or {}
            yoy = metric.get("yoy") or {}
            lines.append(f"- {metric.get('label', key)}：{current.get('value', '缺失')} {current.get('unit', '')}；同比：{yoy.get('value', '缺失')} {yoy.get('unit', '')}")
        lines.extend([
            "",
            "## 4. 通用行业资料计数",
            f"- 公司事件：{len(info.get('company_events', []))} 条",
            f"- 政策监管：{len(info.get('policy_and_regulation', []))} 条",
            f"- 行业公开统计：{len(info['industry_data'].get('public_stats', []))} 条",
            f"- 行业信号：{len(info['industry_data'].get('industry_signals', []))} 条",
            f"- 行情估值状态：{market_status}",
            "",
            "## 5. 同行候选",
        ])
        if info.get("competitors"):
            for peer in info["competitors"]:
                lines.append(f"- {peer.get('stock_code')} {peer.get('company_name')}：{peer.get('reason')}")
        else:
            lines.append("- 未由信息收集员2提供；同行年报和同行财务分析由原信息收集员与财务分析员负责。")
        lines.extend(["", "## 6. 行业指标与信号"])
        for metric in info["industry_data"].get("industry_metrics", []):
            lines.append(f"- 指标指针：{metric.get('metric_name')}：{metric.get('description')}")
        for stat in info["industry_data"].get("public_stats", []):
            lines.append(f"- 公开统计：{stat.get('metric_name')} {stat.get('period', '')} = {stat.get('value')} {stat.get('unit', '')}")
        for signal in info["industry_data"].get("industry_signals", []):
            lines.append(f"- 行业信号：{signal.get('metric_name')} / {signal.get('direction')}：{signal.get('summary')}")
        if package.get("event_study"):
            event_study = package["event_study"]
            metadata = event_study.get("event_metadata", {})
            lines.extend([
                "",
                "## 7. 事件研究覆盖层",
                f"- 事件名称：{metadata.get('event_name')}",
                f"- 事件类型：{metadata.get('event_type')}",
                f"- 事件状态：{metadata.get('event_status')}",
                f"- 影响区域：{metadata.get('geography_scope')}",
                f"- 时间线条数：{len(event_study.get('event_timeline', []))}",
                f"- 传导链条数：{len(event_study.get('transmission_chain', []))}",
                f"- 证伪指标条数：{len(event_study.get('falsification_indicators', []))}",
            ])
            if event_study.get("pricing_mechanism"):
                lines.append(f"- 定价变量：{event_study['pricing_mechanism'].get('price_variable')}")
        lines.extend(["", "## 8. 当前限制"])
        lines.extend([f"- {item}" for item in package["limitations"]])
        lines.extend(["", "## 9. 推荐下一步补采"])
        lines.extend([f"- {item}" for item in package["recommended_next_collection"]])
        return "\n".join(lines) + "\n"

    def _add_source_ref(
        self,
        source_refs: list[dict[str, Any]],
        source_type: str,
        source_path: Path | None,
        source_detail: str,
        reliability: str,
        as_of_date: str,
        limitations: list[str],
    ) -> str:
        """登记来源引用并返回 ref_id，保证 JSON 内证据可回溯。"""

        ref_id = f"SRC-{len(source_refs) + 1:03d}"
        source_refs.append(
            {
                "ref_id": ref_id,
                "source_type": source_type,
                "source_path": str(source_path) if source_path else None,
                "source_detail": source_detail,
                "reliability": reliability,
                "as_of_date": as_of_date,
                "limitations": limitations,
            }
        )
        return ref_id

    def _evidence_item(
        self,
        ref_id: str,
        claim: str,
        evidence: str,
        source_type: str,
        source_path: Path | None,
        source_detail: str,
        reliability: str,
        as_of_date: str,
        limitations: list[str],
    ) -> dict[str, Any]:
        """创建标准证据项。"""

        return {
            "ref_id": ref_id,
            "claim": claim,
            "evidence": evidence,
            "source_type": source_type,
            "source_path": str(source_path) if source_path else None,
            "source_detail": source_detail,
            "reliability": reliability,
            "as_of_date": as_of_date,
            "limitations": limitations,
        }

    def _search_content_text(self, processor_content: dict[str, Any], keywords: list[str]) -> str:
        """从 content.json 中找包含任一关键词的页面文本片段。"""

        matches = []
        for page in processor_content.get("pages", []):
            text = page.get("text", "")
            if any(keyword in text for keyword in keywords):
                matches.append(f"第{page.get('page_number')}页：{self._shorten(text, 500)}")
            if len(matches) >= 3:
                break
        return "\n".join(matches)

    def _market_snapshot_has_values(self, market_data: dict[str, Any]) -> bool:
        """判断行情估值快照是否至少包含一个可用核心数值。"""

        if market_data.get("collection_status") != "available_from_local_file":
            return False
        price = market_data.get("price_snapshot") or {}
        valuation = market_data.get("valuation_snapshot") or {}
        return any(
            value not in {None, ""}
            for value in [
                price.get("price"),
                valuation.get("market_cap"),
                valuation.get("pe_ttm"),
                valuation.get("pb"),
                valuation.get("ps_ttm"),
                valuation.get("ev_ebitda"),
                valuation.get("dividend_yield"),
            ]
        )

    def _aggregate_reliability(self, records: list[dict[str, Any]]) -> str:
        """根据记录可靠性给出保守汇总等级。"""

        levels = [record.get("reliability", "medium") for record in records]
        if "low" in levels:
            return "low"
        if "medium" in levels:
            return "medium"
        if levels:
            return "high"
        return "low"

    def _collect_record_limitations(self, records: list[dict[str, Any]]) -> list[str]:
        """汇总记录限制条件。"""

        limitations: list[str] = []
        for record in records:
            value = record.get("limitations") or []
            if isinstance(value, list):
                limitations.extend(str(item) for item in value)
            else:
                limitations.append(str(value))
        return list(dict.fromkeys(item for item in limitations if item))

    def _shorten(self, text: str, limit: int) -> str:
        """限制长文本长度，避免输出文件被单段证据撑爆。"""

        if len(text) <= limit:
            return text
        return text[:limit] + "……"

    def _sha256(self, path: Path) -> str:
        """计算文件 SHA256，用于审计输入是否可复现。"""

        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _now_iso(self) -> str:
        """返回北京时间 ISO 时间。"""

        return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def default_financial_analysis_path(stock_code: str, company_name: str, fiscal_year: str) -> Path:
    """按当前财务分析员工作区约定推断 analyst_report.json 路径。"""

    return PROJECT_ROOT / "financial_analyst_scripts" / "analyst_workspace" / "reports" / "annual" / fiscal_year / stock_code / f"{stock_code}-{company_name}-{fiscal_year}年年报" / "analyst_report.json"


def default_processor_content_path(stock_code: str, company_name: str, fiscal_year: str) -> Path:
    """按当前信息处理员工作区约定推断 content.json 路径。"""

    return PROJECT_ROOT / "info_processor_scripts" / "processor_workspace" / "parsed_reports" / "annual" / fiscal_year / stock_code / f"{stock_code}-{company_name}-{fiscal_year}年年报" / "content.json"
