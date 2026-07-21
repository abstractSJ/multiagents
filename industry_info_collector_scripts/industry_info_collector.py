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
        if explicit_industry_target and industry_classification.get("primary_industry") == "Unknown Industry":
            industry_classification["primary_industry"] = explicit_industry_target
            industry_classification["classification_basis"] = industry_classification.get("classification_basis", []) + ["The target industry was provided explicitly by the CLI or upstream workflow."]
        industry_name = industry_classification.get("primary_industry") or explicit_industry_target or "Unknown Industry"
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
                "main_business": "Industry-only research input package; not currently tied to a single company business.",
                "business_model": "not_applicable_industry_first_package",
                "source_refs": [],
            }
            financial_summary = {"source_refs": [], "limitations": ["No company financial-analysis report is attached in industry-only mode."]}
            business_segments = []
            competitors = []

        company_events = self._records_with_source(
            records=company_events_records,
            adapter_result=adapter_results["company_events"],
            claim="The local company-events file provides company events, announcements, or investor-relations materials.",
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
            "Policy and Regulation Seed Pointers",
            "curated_seed",
            source_refs,
            evidence_items,
            as_of_date,
        )
        local_policy = self._records_with_source(
            records=policy_records,
            adapter_result=adapter_results["policy_regulation"],
            claim="The local policy-regulation file provides industry policy or regulatory materials.",
            source_refs=source_refs,
            evidence_items=evidence_items,
            as_of_date=as_of_date,
        )
        policy_and_regulation = seed_policy + local_policy
        technology_trends = self._seed_list_with_source(
            seed_industry.get("technology_trends", []),
            paths.seed_data,
            "Technology Trend Seed Pointers",
            "curated_seed",
            source_refs,
            evidence_items,
            as_of_date,
        )
        news = self._seed_list_with_source(
            seed_industry.get("news_pointers", []),
            paths.seed_data,
            "News Collection Seed Pointers",
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
                    claim="The original annual-report source and local path were confirmed from the financial-report collector master manifest.",
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
            "collector_name": "Industry Information Collector",
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
        main_business = business_profile.get("main_business") or self._search_content_text(processor_content or {}, ["主营业务", "经营模式", "营业收入"]) or "No main-business description was extracted from local inputs."
        ref_id = self._add_source_ref(
            source_refs,
            source_type="financial_analyst_output_or_processor_content",
            source_path=paths.financial_analysis_report if isinstance(financial_report, dict) else paths.processor_content_json,
            source_detail="business_profile.main_business / content keyword search",
            reliability="medium" if isinstance(financial_report, dict) else "low",
            as_of_date=as_of_date,
            limitations=["This company profile is an industry-research input; key statements still require verification against the original annual report."],
        )
        evidence_items.append(
            self._evidence_item(
                ref_id=ref_id,
                claim="The company main business and operating model come from local financial analysis or annual-report parsing.",
                evidence=self._shorten(main_business, 800),
                source_type="financial_analyst_output_or_processor_content",
                source_path=paths.financial_analysis_report if isinstance(financial_report, dict) else paths.processor_content_json,
                source_detail="company_profile evidence",
                reliability="medium" if isinstance(financial_report, dict) else "low",
                as_of_date=as_of_date,
                limitations=["Automated summaries or keyword searches may contain duplicate sentences and parsing noise."],
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
            "business_model": business_profile.get("business_model") or "Not extracted in structured form; the industry researcher must verify against the annual report and business segments.",
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
                limitations=["The industry researcher should use financial metrics as company-performance inputs, not as direct evidence of industry conditions."],
            )
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="The core financial summary comes from the financial analyst report.",
                    evidence=json.dumps(summary, ensure_ascii=False)[:1200],
                    source_type="financial_analyst_output",
                    source_path=paths.financial_analysis_report,
                    source_detail="financial_metrics",
                    reliability="medium",
                    as_of_date=as_of_date,
                    limitations=["Some metrics may require verification against RAG or content.json."],
                )
            )
            summary["source_refs"] = [ref_id]
        else:
            summary["source_refs"] = []
            summary["limitations"] = ["Financial analyst report not found; financial summary is missing."]
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
                limitations=["Seed business segments are research inputs and do not replace annual-report segment tables."],
            )
            evidence_items.append(self._evidence_item(ref_id, "Business-segment candidates come from seed data.", json.dumps(seed_segments, ensure_ascii=False), "curated_seed", paths.seed_data, "business_segments", "medium", as_of_date, ["Verification against the annual-report segment table is required."] ))
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
            limitations=["The first version organizes business-segment evidence signals only and does not fully reconstruct segment tables."],
        )
        evidence_items.append(
            self._evidence_item(
                ref_id=ref_id,
                claim="Business-segment candidates come from the financial analyst report or keyword search over parsed annual-report content.",
                evidence=self._shorten(segment_evidence or "Business segments were not reliably extracted from local inputs.", 1200),
                source_type="financial_analyst_output_and_processor_content",
                source_path=paths.financial_analysis_report or paths.processor_content_json,
                source_detail="business segments evidence",
                reliability="medium" if segment_evidence else "low",
                as_of_date=as_of_date,
                limitations=["Business-segment figures require precise verification against original tables by the industry researcher or a later script."],
            )
        )
        if segment_evidence:
            return [
                {
                    "segment_name": "Segment Pending Structuring",
                    "role_in_business": "Business-segment evidence was found in the financial analyst report or annual-report parsing, but has not been split into standardized segment fields.",
                    "evidence_summary": self._shorten(segment_evidence, 800),
                    "source_refs": [ref_id],
                    "limitations": ["Add standardized revenue and gross-margin data by product, industry, and region."],
                }
            ]
        return [
            {
                "segment_name": "Not Structured",
                "role_in_business": "Supplement from annual-report segment tables, company announcements, or manually curated materials.",
                "evidence_summary": "Business segments were not reliably extracted from local inputs.",
                "source_refs": [ref_id],
                "limitations": ["Business segments are not currently extracted in structured form."],
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
            primary = override.industry_name or classification.get("primary_industry", "Unknown Industry")
            secondary = override.secondary_industry or classification.get("secondary_industry", "Unknown Sub-Industry")
            system = override.classification_system or "user_cli_override"
            basis = ["Industry classification was provided explicitly by the CLI or upstream workflow."]
            limitations = ["Explicit industry classification still requires verification against business segments and profit sources by the industry researcher."]
            source_type = "cli_input"
            source_path = None
            detail = "industry classification override"
            reliability = "medium"
        elif classification:
            primary = classification.get("primary_industry", "Unknown Industry")
            secondary = classification.get("secondary_industry", "Unknown Sub-Industry")
            system = classification.get("classification_system", "curated_seed")
            basis = classification.get("classification_basis", [])
            limitations = classification.get("limitations", [])
            source_type = "curated_seed"
            source_path = paths.seed_data
            detail = f"companies.{stock_code}.initial_industry_classification"
            reliability = "medium"
        else:
            primary = "Unknown Industry"
            secondary = "Unknown Sub-Industry"
            system = "missing"
            basis = []
            limitations = ["No industry-classification seed or CLI override was provided; the industry information collector does not infer the industry automatically."]
            source_type = "missing_classification"
            source_path = None
            detail = "industry classification missing"
            reliability = "low"
        if isinstance(financial_report, dict) and financial_report.get("business_profile", {}).get("main_business"):
            basis.append("The financial analyst report provides a main-business description that can support industry-classification verification, but this collector does not infer the classification automatically.")
        ref_id = self._add_source_ref(source_refs, source_type, source_path, detail, reliability, as_of_date, limitations)
        evidence_items.append(
            self._evidence_item(
                ref_id=ref_id,
                claim=f"{stock_code} initial industry classification is {primary}/{secondary}.",
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
            limitations=["Peer candidates are not final valuation comparables; peer annual reports and financial analysis remain the responsibility of the original information collector and financial analyst."],
        )
        evidence_items.append(self._evidence_item(ref_id, "Peer candidates come from seed data.", json.dumps(peers, ensure_ascii=False), "curated_seed", paths.seed_data, "peer_candidates", "medium", as_of_date, ["The industry researcher and valuation analyst must continue screening comparability."] ))
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
                limitations=["Seed industry data provides a research framework or pointers and does not replace authoritative real-time data."],
            )
            source_ref_ids.append(ref_id)
            evidence_items.append(self._evidence_item(ref_id, f"{industry_name} industry framework comes from seed data.", json.dumps(seed_industry, ensure_ascii=False)[:1600], "curated_seed", paths.seed_data, f"industries.{industry_name}", "medium", as_of_date, ["This cannot independently support a real-time industry-cycle conclusion."] ))
            value_chain = {**value_chain, "source_refs": [ref_id]}
            industry_metrics = [{**metric, "source_refs": [ref_id]} for metric in industry_metrics]
        public_stats = self._records_with_source(
            records=public_stats_records,
            adapter_result=adapter_results["industry_public_stats"],
            claim="The local public industry-statistics file provides industry metrics.",
            source_refs=source_refs,
            evidence_items=evidence_items,
            as_of_date=as_of_date,
        )
        industry_signals = self._records_with_source(
            records=industry_signal_records,
            adapter_result=adapter_results["industry_signals"],
            claim="The local industry-signals file provides pricing, inventory, demand, supply, channel, technology, or policy signals.",
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
                "limitations": ["No local market-valuation file was provided; the industry researcher cannot assess valuation from this package."],
                "recommended_next_collection": ["Connect a stable user-provided market/valuation export file."],
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
        evidence_items.append(self._evidence_item(ref_id, "The market-valuation snapshot comes from the local market-valuation file.", json.dumps(market_snapshot, ensure_ascii=False), adapter_result.source_type, adapter_result.source_path, "selected market valuation snapshot", market_snapshot.get("reliability", "medium"), as_of_date, market_snapshot.get("limitations", [])))
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
            return "Event observation window pending"
        return "1 year"

    def _resolve_focus_text(self, deliverable_type: str, event_study_request: EventStudyRequest | None) -> str:
        """生成可读的研究焦点描述。"""

        if deliverable_type == "theme_event_study":
            impact_variables = ", ".join(event_study_request.impact_variables or []) if event_study_request else "Event transmission"
            event_name = event_study_request.event_name if event_study_request and event_study_request.event_name else "Event"
            return f"Event Study | {event_name} | Key variables: {impact_variables}"
        return "Industry Classification | Public Statistics | Industry Signals | Policy and Regulation | Market Valuation | Gap-Filling Collection"

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
                limitations=["This source defines the research question and event only; it does not prove that transmission has occurred."],
            )
            request_ref_ids.append(ref_id)
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="The event-study request was provided explicitly by the CLI or upstream session.",
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
                    limitations=["This is the research question itself, not factual industry evidence."],
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
                limitations=["The event timeline requires verification against official announcements, original policy documents, or authoritative sources."],
            )
            timeline_ref_ids.append(ref_id)
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="The event timeline comes from the local event-timeline file.",
                    evidence=json.dumps(timeline_records, ensure_ascii=False)[:1800],
                    source_type="local_event_timeline_file",
                    source_path=paths.event_timeline_file,
                    source_detail="event_timeline",
                    reliability="medium",
                    as_of_date=as_of_date,
                    limitations=["The event timeline is organized at the input layer; not every node has completed fundamental validation."],
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
                limitations=["Event-impact observations depend on locally curated files; important metrics still require verification against original sources."],
            )
            impacts_ref_ids.append(ref_id)
            evidence_items.append(
                self._evidence_item(
                    ref_id=ref_id,
                    claim="Event impacts, transmission chains, and falsification indicators come from the local event-impact file.",
                    evidence=json.dumps(impacts_payload, ensure_ascii=False)[:1800],
                    source_type="local_event_impacts_file",
                    source_path=paths.event_impacts_file,
                    source_detail="event_impacts",
                    reliability="medium",
                    as_of_date=as_of_date,
                    limitations=["If the local event-impact file contains only forward scenarios, downstream conclusions must remain downgraded."],
                )
            )

        if not timeline_records and request.event_name:
            timeline_records = [
                {
                    "date": request.event_start_date or as_of_date,
                    "event": request.event_name,
                    "why_it_matters": request.event_description or "The event is defined, but key nodes still require additional evidence.",
                    "affected_channel": ", ".join(impact_variables) if impact_variables else "unknown",
                }
            ]

        event_timeline = [
            {
                "date": record.get("date") or record.get("event_date") or request.event_start_date or "unknown",
                "event": record.get("event") or record.get("title") or record.get("event_name") or request.event_name or "Unnamed Event",
                "why_it_matters": record.get("why_it_matters") or record.get("summary") or record.get("description") or "Its importance must be explained using industry variables.",
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
                "confidence": counterfactual_without_event.get("confidence") or "low",
            },
        }

        transmission_chain = []
        if isinstance(transmission_payload, list) and transmission_payload:
            transmission_chain = [
                {
                    "step": item.get("step") or index + 1,
                    "from": item.get("from") or item.get("source") or request.event_name or "External Event",
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
                    "from": request.event_name or "External Event",
                    "to": variable,
                    "channel": variable,
                    "mechanism": f"Further validation is required to show how the event transmits through {variable} to {industry_name}.",
                    "expected_direction": "unknown",
                    "expected_lag": request.event_window or "unknown",
                    "evidence_status": "expected",
                    "observed_evidence_refs": impacts_ref_ids + request_ref_ids,
                    "missing_evidence": [f"Post-event observation data for {variable} is missing."],
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
                "confidence": item.get("confidence") or "low",
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
                    "confidence": "low",
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
                "confidence": item.get("confidence") or "low",
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
                    "reasoning": f"Further validation is required to determine whether {request.event_name or 'the event'} transmits to the industry through {variable}.",
                    "confidence": "low",
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
                "would_falsify": item.get("would_falsify") or "Complete this based on the specific research conclusion.",
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
                    "would_falsify": f"If {variable} remains unchanged for an extended period, the event impact may still be only an expectation.",
                    "current_status": "not_started",
                }
                for variable in impact_variables
            ]

        event_specific_gaps = []
        if not request.event_name:
            event_specific_gaps.append(
                {
                    "gap": "A clear event name or definition is missing.",
                    "blocks_which_judgement": "The research target and event boundary cannot be defined reliably.",
                    "suggested_source": "The main session should add an event definition or local event list.",
                    "fallback_proxy": "None",
                    "severity": "high",
                }
            )
        if not event_timeline:
            event_specific_gaps.append(
                {
                    "gap": "A key event timeline is missing.",
                    "blocks_which_judgement": "It is not possible to determine when the event began affecting the industry or which nodes are new variables.",
                    "suggested_source": "Original policy documents, announcements, news timelines, or local event files.",
                    "fallback_proxy": "Retain only the event hypothesis; do not conclude that transmission has occurred.",
                    "severity": "high",
                }
            )
        if not transmission_chain:
            event_specific_gaps.append(
                {
                    "gap": "The transmission chain from the event to industry variables is missing.",
                    "blocks_which_judgement": "It is not possible to determine how the event affects supply, demand, pricing, or profit.",
                    "suggested_source": "The industry researcher should map the transmission chain manually or use a local event-impact file.",
                    "fallback_proxy": "Retain background description only.",
                    "severity": "high",
                }
            )
        if self._event_requires_pricing_mechanism(impact_variables, request.pricing_variable) and not pricing_mechanism:
            event_specific_gaps.append(
                {
                    "gap": "The pricing or profit-formation mechanism is missing.",
                    "blocks_which_judgement": "The event cannot be linked to price, ASP, rates, gross margin, or profit.",
                    "suggested_source": "Pricing-mechanism documentation, tender rules, long-term contract mechanisms, or cost-pass-through materials.",
                    "fallback_proxy": "Assess supply-demand or logistics only; do not conclude on pricing or profit.",
                    "severity": "high",
                }
            )
        if not has_observed_numeric_impact:
            event_specific_gaps.append(
                {
                    "gap": "Observable post-event realization variables are missing.",
                    "blocks_which_judgement": "It is not possible to confirm that the event impact has reached fundamentals.",
                    "suggested_source": "Post-event data such as price, inventory, output, imports, orders, tender volume, or gross margin.",
                    "fallback_proxy": "Downgrade to an event-driven hypothesis pending validation.",
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
                    "coverage_note": record.get("coverage") or record.get("summary") or "Public Industry Statistics",
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
                    "coverage_note": record.get("summary") or "Industry Signal",
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
                        "coverage_note": "Post-Event Observation Variable",
                        "confidence": item.get("confidence") or "low",
                        "gap_reason": None if item.get("current_value") not in {None, "", "unknown"} else "No observable post-event value has been obtained.",
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
                    "coverage_note": "Local Market-Valuation Snapshot",
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
                    "gap": "Public industry-statistics series are missing.",
                    "blocks_which_judgement": "Industry scale, historical position, and cycle stage cannot be assessed reliably.",
                    "suggested_source": "Statistics bureaus, associations, customs, industry databases, or user-provided local statistics files.",
                    "fallback_proxy": "Local samples or company-level measures can serve only as weak proxies.",
                    "severity": "high",
                }
            )
        if not industry_signals:
            gaps.append(
                {
                    "gap": "Industry-signal data is missing.",
                    "blocks_which_judgement": "High-frequency changes in price, inventory, demand, supply, or channels are difficult to track.",
                    "suggested_source": "Local signal files covering price, inventory, supply-demand, technology, or channels.",
                    "fallback_proxy": "News and management commentary can serve only as supporting signals.",
                    "severity": "medium",
                }
            )
        if not policy_and_regulation:
            gaps.append(
                {
                    "gap": "Original policy or regulatory documents are missing.",
                    "blocks_which_judgement": "It is not possible to confirm which segment is affected by policy, regulation, centralized procurement, or sanctions.",
                    "suggested_source": "Original policy documents, regulatory files, centralized-procurement rules, or export-restriction documents.",
                    "fallback_proxy": "Secondary summaries can support only low-confidence conclusions.",
                    "severity": "medium",
                }
            )
        if market_data.get("collection_status") != "available_from_local_file":
            gaps.append(
                {
                    "gap": "A stable market-valuation snapshot is missing.",
                    "blocks_which_judgement": "Valuation position or market mapping cannot be assessed from this package.",
                    "suggested_source": "A stable user-provided market/valuation export file.",
                    "fallback_proxy": "Perform fundamental research only; do not discuss valuation.",
                    "severity": "low",
                }
            )
        if deliverable_type == "theme_event_study" and event_study:
            gaps.extend(event_study.get("event_specific_gaps", []))
        if deliverable_type != "theme_event_study" and not company_events:
            gaps.append(
                {
                    "gap": "Company-event validation materials are missing.",
                    "blocks_which_judgement": "Company behavior cannot be used to validate marginal industry changes.",
                    "suggested_source": "Company events, operating announcements, or investor-relations records.",
                    "fallback_proxy": "Keep the analysis at the industry level and avoid strong company mapping.",
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
            coverage = "high"
        elif evidence_categories >= 2:
            coverage = "medium"
        else:
            coverage = "low"

        if has_company_context and not public_stats and not industry_signals and not policy_and_regulation:
            company_evidence_ratio = "high"
        elif has_company_context:
            company_evidence_ratio = "medium"
        else:
            company_evidence_ratio = "low"

        theme_event_study_gate = self._build_theme_event_study_gate(event_study) if deliverable_type == "theme_event_study" else None
        can_support_full_research = quant_variable_count >= 3 and coverage != "low"
        downgrade_reason = ""
        if deliverable_type == "theme_event_study":
            can_support_full_research = can_support_full_research and bool(theme_event_study_gate and theme_event_study_gate.get("has_observed_impact_variable")) and bool(theme_event_study_gate and theme_event_study_gate.get("minimum_passed"))
            downgrade_reason = theme_event_study_gate.get("downgrade_reason") if theme_event_study_gate else "The event-study coverage layer is missing."
        else:
            if quant_variable_count < 3:
                downgrade_reason = "There are fewer than three core quantitative variables."
            elif coverage == "low":
                downgrade_reason = "Industry-level evidence coverage is low."
            elif company_evidence_ratio == "high":
                downgrade_reason = "Company materials account for too much of the package; industry-level evidence is insufficient."

        if can_support_full_research:
            downgrade_reason = ""
        if not downgrade_reason and gaps:
            downgrade_reason = gaps[0].get("gap", "Critical evidence gaps remain.")

        gate = {
            "industry_evidence_coverage": coverage,
            "quant_variable_count": quant_variable_count,
            "freshness_check": f"Key inputs are organized as of {as_of_date}; verify source-file dates to confirm whether they use the latest basis.",
            "source_diversity": "medium" if public_stats or industry_signals or policy_and_regulation else "low",
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
            downgrade_reasons.append("A key event timeline is missing.")
        if not event_study.get("transmission_chain"):
            downgrade_reasons.append("The event transmission chain is missing.")
        if not event_study.get("baseline_and_counterfactual"):
            downgrade_reasons.append("The pre-event baseline or counterfactual is missing.")
        if not event_study.get("falsification_indicators"):
            downgrade_reasons.append("Falsification indicators are missing.")
        if requires_pricing_mechanism and not pricing_mechanism:
            downgrade_reasons.append("Price/profit is a focus, but the pricing or profit-formation mechanism is missing.")
        if not has_observed_impact_variable:
            downgrade_reasons.append("Observable post-event realization variables are missing.")
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
        ref_id = self._add_source_ref(source_refs, source_type, seed_path, claim, "low", as_of_date, ["This list contains offline seed pointers and requires later collection from authoritative sources."])
        evidence_items.append(self._evidence_item(ref_id, claim, json.dumps(items, ensure_ascii=False), source_type, seed_path, claim, "low", as_of_date, ["Not authoritative real-time data."] ))
        return [{**item, "source_refs": [ref_id]} for item in items]

    def _build_management_commentary(self, financial_report: Any) -> list[dict[str, Any]]:
        """整理管理层观点入口；当前优先提示回到财务分析员报告和年报原文。"""

        if not isinstance(financial_report, dict):
            return []
        growth = financial_report.get("growth_and_outlook", {})
        return [
            {
                "topic": "Growth and Outlook Fields in the Financial Analyst Report",
                "summary": self._shorten(json.dumps(growth, ensure_ascii=False), 800),
                "limitations": ["This content comes from the financial analyst report summary; the industry researcher should verify it against the annual report Management Discussion and Analysis section."],
            }
        ]

    def _build_financial_analysis_ref(self, financial_report: Any, path: Path | None) -> dict[str, Any]:
        """整理财务分析员报告入口，供行业研究员理解其可用性和限制。"""

        if not isinstance(financial_report, dict):
            return {"available": False, "source_path": str(path) if path else None, "limitations": ["Financial analyst report not found."]}
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
            limitations.append("This run is offline and did not actively fetch web pages, news, or external APIs.")
        if not isinstance(financial_report, dict):
            limitations.append("The financial analyst report was not found; the financial summary is insufficient.")
        if not seed_data:
            limitations.append("Industry seed data was not found; industry classification, peer candidates, and value-chain information may be insufficient.")
        if industry_classification.get("primary_industry") == "Unknown Industry":
            limitations.append("No reliable industry classification was provided; the industry researcher must confirm the actual industry classification first.")
        if market_data.get("collection_status") != "available_from_local_file":
            limitations.append("No market-valuation file was provided; valuation cannot be assessed from this package.")
        limitations.extend(
            [
                "Peer candidates, if present, are research signals rather than final valuation comparables. Peer annual reports and financial analysis remain the responsibility of the original information collector and financial analyst.",
                "Industry metrics, policies, statistics, and signals are research inputs and do not constitute an automatic industry conclusion.",
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
            recommendations.append("Add public industry statistics such as output, sales, price, inventory, imports/exports, penetration, or industry revenue and profit, as applicable.")
        if not industry_signals:
            recommendations.append("Add industry-signal data such as price, demand, inventory, channels, supply, policy, or technology changes.")
        if not company_events:
            recommendations.append("Add company events, non-financial-report announcements, investor-relations records, or operating developments.")
        if not policy_and_regulation:
            recommendations.append("Add original industry-related policy, regulatory, or compliance documents.")
        if market_data.get("collection_status") != "available_from_local_file":
            recommendations.append("Connect a stable user-provided market/valuation source and add price, market cap, valuation, and dividend-yield snapshots.")
        if event_study:
            if not event_study.get("event_timeline"):
                recommendations.append("Add a key event timeline covering the starting point, escalation nodes, and policy implementation nodes.")
            if not event_study.get("transmission_chain"):
                recommendations.append("Add the transmission chain from the event to supply, demand, logistics, price, or profit.")
            if not event_study.get("observed_vs_expected_impacts", {}).get("observed_impacts"):
                recommendations.append("Add observable post-event variables such as price, inventory, import volume, orders, tender volume, or gross margin.")
            if self._event_requires_pricing_mechanism(
                [item.get("variable_name") for item in event_study.get("observed_vs_expected_impacts", {}).get("observed_impacts", []) if item.get("variable_name")],
                (event_study.get("pricing_mechanism") or {}).get("price_variable") if event_study.get("pricing_mechanism") else None,
            ) and not event_study.get("pricing_mechanism"):
                recommendations.append("Add the pricing or profit-formation mechanism explaining how the event affects price, rates, ASP, or profit.")
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
        if missing_core or classification.get("primary_industry") == "Unknown Industry":
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
                "business_segments": "available" if info["business_segments"] and info["business_segments"][0].get("segment_name") != "Not Structured" else "partial",
                "industry_classification": "available" if classification.get("primary_industry") != "Unknown Industry" else "missing",
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
        title_name = company.get("name") or target.get("industry_name") or "Unnamed Industry"
        lines = [
            f"# {title_name} Industry Research Input Package",
            "",
            "## 1. Basic Information",
            f"- Target industry: {target.get('industry_name', classification.get('primary_industry'))}",
            f"- Deliverable type: {package.get('deliverable_type')}",
            f"- Stock code: {company.get('ticker')}",
            f"- Fiscal year: {company.get('fiscal_year')}",
            f"- As-of date: {company.get('as_of_date') or target.get('as_of_date')}",
            f"- Input quality: {audit['input_quality']}",
            f"- Ready for industry researcher: {audit['ready_for_industry_researcher']}",
            "",
            "## 2. Initial Industry Classification",
            f"- Primary industry: {classification.get('primary_industry')}",
            f"- Secondary industry: {classification.get('secondary_industry')}",
            "- Classification basis:",
        ]
        lines.extend([f"  - {item}" for item in classification.get("classification_basis", [])] or ["  - No classification basis is available; additional evidence is required."])
        lines.extend(["", "## 3. Core Financial Summary"])
        for key in ["revenue", "net_profit_attributable", "deducted_net_profit", "operating_cash_flow", "roe"]:
            metric = financial.get(key, {})
            current = metric.get("current") or {}
            yoy = metric.get("yoy") or {}
            lines.append(f"- {metric.get('label', key)}: {current.get('value', 'Missing')} {current.get('unit', '')}; YoY: {yoy.get('value', 'Missing')} {yoy.get('unit', '')}")
        lines.extend([
            "",
            "## 4. General Industry Data Counts",
            f"- Company events: {len(info.get('company_events', []))}",
            f"- Policy and regulation: {len(info.get('policy_and_regulation', []))}",
            f"- Public industry statistics: {len(info['industry_data'].get('public_stats', []))}",
            f"- Industry signals: {len(info['industry_data'].get('industry_signals', []))}",
            f"- Market-valuation status: {market_status}",
            "",
            "## 5. Peer Candidates",
        ])
        if info.get("competitors"):
            for peer in info["competitors"]:
                lines.append(f"- {peer.get('stock_code')} {peer.get('company_name')}: {peer.get('reason')}")
        else:
            lines.append("- Not provided by the industry information collector. Peer annual reports and financial analysis remain the responsibility of the original information collector and financial analyst.")
        lines.extend(["", "## 6. Industry Metrics and Signals"])
        for metric in info["industry_data"].get("industry_metrics", []):
            lines.append(f"- Metric pointer: {metric.get('metric_name')}: {metric.get('description')}")
        for stat in info["industry_data"].get("public_stats", []):
            lines.append(f"- Public statistic: {stat.get('metric_name')} {stat.get('period', '')} = {stat.get('value')} {stat.get('unit', '')}")
        for signal in info["industry_data"].get("industry_signals", []):
            lines.append(f"- Industry signal: {signal.get('metric_name')} / {signal.get('direction')}: {signal.get('summary')}")
        if package.get("event_study"):
            event_study = package["event_study"]
            metadata = event_study.get("event_metadata", {})
            lines.extend([
                "",
                "## 7. Event-Study Coverage",
                f"- Event name: {metadata.get('event_name')}",
                f"- Event type: {metadata.get('event_type')}",
                f"- Event status: {metadata.get('event_status')}",
                f"- Geographic scope: {metadata.get('geography_scope')}",
                f"- Timeline entries: {len(event_study.get('event_timeline', []))}",
                f"- Transmission-chain entries: {len(event_study.get('transmission_chain', []))}",
                f"- Falsification indicators: {len(event_study.get('falsification_indicators', []))}",
            ])
            if event_study.get("pricing_mechanism"):
                lines.append(f"- Pricing variable: {event_study['pricing_mechanism'].get('price_variable')}")
        lines.extend(["", "## 8. Current Limitations"])
        lines.extend([f"- {item}" for item in package["limitations"]])
        lines.extend(["", "## 9. Recommended Next Collection"])
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
                matches.append(f"Page {page.get('page_number')}: {self._shorten(text, 500)}")
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
