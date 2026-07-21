"""
财务分析员证据草稿模块。

该模块不是正式财务分析员 Agent 本体，而是 LLM-first 研究工作流的
基础设施工具：读取信息处理员产出的 llm_digest、digest_audit、RAG
证据索引和摘要覆盖报告，生成指标候选、证据核验、开放问题和规则草稿。
正式研究结论应由 LLM Agent 基于这些证据进行多轮阅读、补证、反证和
专业判断后生成，不能把本模块的规则输出直接当作交易级分析报告。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_ANALYST_WORKSPACE = Path(__file__).resolve().parent / "analyst_workspace"
DEFAULT_SCHEMA_VERSION = "1.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SEVERITY_SCORE = {"critical": 5, "high": 4, "medium_high": 3.5, "medium": 3, "medium_low": 2, "low": 1, "unknown": 0}
PRIORITY_SCORE = {"S": 3, "A": 2, "B": 1, "C": 0}

CORE_RAG_QUERIES = {
    "Revenue": ["营业收入"],
    "Net Profit Attributable": ["归属于上市公司股东的净利润", "归母净利润"],
    "Recurring Net Profit Attributable": ["扣除非经常性损益", "扣非"],
    "Operating Cash Flow": ["经营活动产生的现金流量净额", "经营现金流"],
    "Gross Margin": ["毛利率"],
    "ROE": ["加权平均净资产收益率", "ROE"],
    "Accounts Receivable": ["应收账款"],
    "Inventory": ["存货"],
    "Goodwill": ["商誉"],
    "Interest-Bearing Debt": ["短期借款", "长期借款", "一年内到期的非流动负债", "有息负债"],
    "Audit Opinion": ["审计意见", "非标准审计意见", "标准无保留意见"],
    "Material Risks": ["重大诉讼", "担保", "资金占用", "关联交易", "内部控制"],
    "Dividend": ["利润分配", "现金红利", "分红"],
}

METRIC_DEFINITIONS = {
    "revenue": {
        "label": "Revenue",
        "aliases": ["营业收入"],
        "exclude": ["扣除项目", "扣除后", "调增", "调减", "增长率门槛", "占比"],
        "ratio_metric": False,
    },
    "net_profit_attributable": {
        "label": "Net Profit Attributable",
        "aliases": ["归属于上市公司股东的净利润", "归母净利润"],
        "exclude": ["扣除非经常性损益", "扣非"],
        "ratio_metric": False,
    },
    "deducted_net_profit": {
        "label": "Recurring Net Profit Attributable",
        "aliases": ["扣除非经常性损益的净利润", "扣非归母净利润", "扣除非经常性损益"],
        "exclude": ["影响金额", "非经常性损益合计"],
        "ratio_metric": False,
    },
    "gross_margin": {
        "label": "Gross Margin",
        "aliases": ["毛利率", "综合能源服务毛利率"],
        "exclude": [],
        "ratio_metric": True,
    },
    "net_margin": {
        "label": "Net Margin",
        "aliases": ["净利率", "销售净利率"],
        "exclude": [],
        "ratio_metric": True,
    },
    "roe": {
        "label": "ROE",
        "aliases": ["加权平均净资产收益率", "净资产收益率", "ROE"],
        "exclude": [],
        "ratio_metric": True,
    },
    "eps": {
        "label": "Basic EPS",
        "aliases": ["基本每股收益"],
        "exclude": [],
        "ratio_metric": False,
    },
    "operating_cash_flow": {
        "label": "Net Operating Cash Flow",
        "aliases": ["经营活动产生的现金流量净额", "经营活动现金流量净额", "经营现金流"],
        "exclude": [],
        "ratio_metric": False,
    },
    "total_assets": {
        "label": "Total Assets",
        "aliases": ["总资产", "资产总额"],
        "exclude": ["周转"],
        "ratio_metric": False,
    },
    "equity": {
        "label": "Equity Attributable to Shareholders",
        "aliases": ["归属于上市公司股东的净资产", "归母净资产", "净资产"],
        "exclude": ["净资产收益率"],
        "ratio_metric": False,
    },
    "interest_bearing_debt": {
        "label": "Interest-Bearing Debt",
        "aliases": ["短期借款", "长期借款", "一年内到期的非流动负债", "应付债券", "有息负债"],
        "exclude": [],
        "ratio_metric": False,
    },
    "cash_and_cash_equivalents": {
        "label": "Cash and Cash Equivalents",
        "aliases": ["货币资金", "现金及现金等价物余额", "现金及现金等价物"],
        "exclude": ["收到的现金"],
        "ratio_metric": False,
    },
    "accounts_receivable": {
        "label": "Accounts Receivable",
        "aliases": ["应收账款"],
        "exclude": ["减值准备转回"],
        "ratio_metric": False,
    },
    "contract_assets": {
        "label": "Contract Assets",
        "aliases": ["合同资产"],
        "exclude": [],
        "ratio_metric": False,
    },
    "inventory": {
        "label": "Inventory",
        "aliases": ["存货"],
        "exclude": [],
        "ratio_metric": False,
    },
    "goodwill": {
        "label": "Goodwill",
        "aliases": ["商誉"],
        "exclude": ["商誉减值"],
        "ratio_metric": False,
    },
}

BUSINESS_KEYWORDS = ["主营业务", "业务模式", "商业模式", "收入结构", "产品", "行业", "客户", "EPC", "EPCOS", "综合能源"]
PROFIT_KEYWORDS = ["营业收入", "净利润", "扣非", "毛利率", "费用", "减值", "非经常性损益", "盈利"]
CASHFLOW_KEYWORDS = ["现金流", "经营活动", "收现", "回款", "现金及现金等价物"]
BALANCE_SHEET_KEYWORDS = ["应收账款", "合同资产", "存货", "商誉", "货币资金", "短期借款", "长期借款", "资产减值", "信用减值", "受限资产"]
CAPITAL_ALLOCATION_KEYWORDS = ["利润分配", "现金红利", "分红", "回购", "融资", "资本开支", "研发投入"]
GOVERNANCE_KEYWORDS = ["审计意见", "关键审计事项", "内部控制", "会计差错", "追溯调整", "资金占用", "违规担保", "重大诉讼", "关联交易"]


@dataclass
class AnalystInputPaths:
    """
    财务分析员输入文件集合。

    参数：
        report_dir: 信息处理员生成的单份报告目录。
        digest_json: llm_digest.json 路径。
        digest_audit: digest_audit.json 路径。
        rag_chunks: rag_chunks.jsonl 路径。
        summary_comparison_json: summary_comparison.json 路径，可不存在。
        summary_comparison_md: summary_comparison.md 路径，可不存在。
        content_json: content.json 路径，可作为必要时的兜底证据。
    返回值：
        dataclass 实例，无额外返回值。
    """

    report_dir: Path
    digest_json: Path
    digest_audit: Path
    rag_chunks: Path
    summary_comparison_json: Path
    summary_comparison_md: Path
    content_json: Path


@dataclass
class DigestFinding:
    """
    digest 中单条财务发现的标准化记录。

    参数：
        item_type: finding 或 risk。
        topic: 主题或风险类型。
        summary: 摘要内容。
        numbers: 该发现中抽取出的数字列表。
        pages: 来源页码。
        chunk_id: 来源 digest chunk。
        section: 来源章节。
        severity: 风险严重性；普通发现为空。
    返回值：
        dataclass 实例，无额外返回值。
    """

    item_type: str
    topic: str
    summary: str
    numbers: list[dict[str, Any]] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    chunk_id: str = ""
    section: str = ""
    severity: str = ""


@dataclass
class SourceRefBuilder:
    """
    证据引用编号生成器。

    参数：
        refs: 已生成的证据引用列表。
        key_to_ref_id: 去重键到引用编号的映射。
    返回值：
        dataclass 实例，无额外返回值。
    """

    refs: list[dict[str, Any]] = field(default_factory=list)
    key_to_ref_id: dict[str, str] = field(default_factory=dict)

    def add_ref(
        self,
        *,
        source_type: str,
        pages: list[int] | None,
        chunk_id: str,
        quote: str,
        confidence: str = "high",
        chunk_type: str = "",
        section: str = "",
    ) -> str:
        """
        新增或复用一条证据引用。

        参数：
            source_type: 来源类型，例如 digest 或 rag。
            pages: 来源页码。
            chunk_id: 来源 chunk 编号。
            quote: 支撑结论的原文或摘要片段。
            confidence: 对该证据引用可靠性的判断。
            chunk_type: RAG chunk 类型。
            section: 来源章节。
        返回值：
            证据引用编号，例如 REF-001。
        """
        clean_quote = normalize_space(quote)[:500]
        clean_pages = sorted({int(page) for page in pages or [] if str(page).isdigit() or isinstance(page, int)})
        dedupe_key = json.dumps(
            {
                "source_type": source_type,
                "pages": clean_pages,
                "chunk_id": chunk_id,
                "quote": clean_quote[:180],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if dedupe_key in self.key_to_ref_id:
            return self.key_to_ref_id[dedupe_key]
        ref_id = f"REF-{len(self.refs) + 1:03d}"
        self.key_to_ref_id[dedupe_key] = ref_id
        self.refs.append(
            {
                "ref_id": ref_id,
                "source_type": source_type,
                "pages": clean_pages,
                "chunk_id": chunk_id,
                "rag_chunk_id": chunk_id if source_type == "rag" else "",
                "chunk_type": chunk_type,
                "section": section,
                "quote": clean_quote,
                "confidence": confidence,
            }
        )
        return ref_id


class FinancialReportAnalyzer:
    """
    财务分析员证据草稿执行器。

    该执行器只负责生成指标候选、证据核验和规则化研究草稿，供后续
    LLM 财务分析 Agent 阅读和复核。它不能替代 Agent 的多轮研究判断。

    参数：
        workspace: 财务分析员工作区，默认写入 financial_analyst_scripts/analyst_workspace。
    返回值：
        初始化后的分析器实例。
    """

    def __init__(self, workspace: str | Path = DEFAULT_ANALYST_WORKSPACE) -> None:
        self.workspace = Path(workspace).resolve()

    def analyze_report_dir(
        self,
        report_dir: str | Path,
        *,
        output_dir: str | Path | None = None,
        analysis_depth: str = "standard",
        allow_incomplete_digest: bool = False,
        focus: str = "",
    ) -> dict[str, Any]:
        """
        读取单份信息处理员报告目录并生成规则化证据草稿。

        参数：
            report_dir: 信息处理员输出的单份报告目录。
            output_dir: 证据草稿输出目录；不传时按公司、年份和报告名自动生成。
            analysis_depth: 分析深度标签，仅用于记录草稿生成配置。
            allow_incomplete_digest: digest 不完整时是否允许输出低置信初步草稿。
            focus: 可选重点分析方向，例如 cashflow、receivable、growth。
        返回值：
            包含输出路径、规则评级和置信度的字典；这些评级只是候选信号。
        """
        input_paths = self.resolve_input_paths(report_dir)
        digest = load_json(input_paths.digest_json)
        digest_audit = load_json(input_paths.digest_audit)
        summary_comparison = load_optional_json(input_paths.summary_comparison_json)
        rag_chunks = load_jsonl(input_paths.rag_chunks)

        digest_complete = bool(digest_audit.get("complete")) and bool(digest.get("complete", True))
        if not digest_complete and not allow_incomplete_digest:
            raise RuntimeError(
                "digest_audit.complete=false or llm_digest.complete=false. "
                "Pass --allow-incomplete-digest explicitly to generate a preliminary report."
            )

        metadata = digest.get("document_metadata", {}) or {}
        output_path = Path(output_dir).resolve() if output_dir else self.default_output_dir(metadata)
        output_path.mkdir(parents=True, exist_ok=True)

        ref_builder = SourceRefBuilder()
        findings = flatten_digest_findings(digest)
        input_audit = self.build_input_audit(digest_audit, summary_comparison, input_paths, rag_chunks)
        financial_metrics = self.build_financial_metrics(findings, metadata, ref_builder)
        evidence_check = self.build_evidence_check(financial_metrics, findings, rag_chunks, ref_builder)
        analysis_sections = self.build_analysis_sections(findings, financial_metrics, ref_builder)
        risks = self.build_risks(findings, ref_builder)
        opportunities = self.build_opportunities(findings, ref_builder)
        ratings = self.build_ratings(financial_metrics, findings, input_audit, evidence_check)
        open_questions = self.build_open_questions(financial_metrics, findings, input_audit, focus)
        upstream_requests = self.build_upstream_requests(metadata, input_audit, open_questions)
        decision_signals = self.build_decision_signals(ratings, financial_metrics, findings, ref_builder)

        report_payload = {
            "schema_version": DEFAULT_SCHEMA_VERSION,
            "generated_at": beijing_now(),
            "company": build_company_payload(metadata),
            "input_audit": input_audit,
            "executive_summary": {
                "overall_view": build_overall_view(ratings, financial_metrics, findings),
                "fundamental_rating": ratings["fundamental_rating"],
                "financial_quality_rating": ratings["financial_quality_rating"],
                "risk_rating": ratings["risk_rating"],
                "confidence": ratings["confidence"],
                "key_reasons": ratings["key_reasons"],
            },
            "business_profile": self.build_business_profile(findings, ref_builder),
            "financial_metrics": financial_metrics,
            "analysis_sections": analysis_sections,
            "risks": risks,
            "opportunities": opportunities,
            "decision_signals": decision_signals,
            "open_questions": open_questions,
            "upstream_requests": upstream_requests,
            "analysis_metadata": {
                "analysis_depth": analysis_depth,
                "focus": focus,
                "method": "rule_based_evidence_draft_for_llm_agent_review",
                "artifact_role": "evidence_draft_not_final_research_report",
                "required_next_actor": "llm_financial_analyst_agent",
                "limitations": build_method_limitations(input_audit, evidence_check),
            },
            "source_ref_index": ref_builder.refs,
        }

        analyst_audit = self.build_analyst_audit(input_paths, input_audit, evidence_check, upstream_requests, report_payload)
        write_json(output_path / "analyst_report.json", report_payload)
        (output_path / "analyst_report.md").write_text(render_analyst_markdown(report_payload), encoding="utf-8")
        write_json(output_path / "evidence_check.json", evidence_check)
        write_json(output_path / "analyst_audit.json", analyst_audit)

        return {
            "output_dir": str(output_path),
            "analyst_report_json": str(output_path / "analyst_report.json"),
            "analyst_report_md": str(output_path / "analyst_report.md"),
            "evidence_check_json": str(output_path / "evidence_check.json"),
            "analyst_audit_json": str(output_path / "analyst_audit.json"),
            "fundamental_rating": ratings["fundamental_rating"],
            "financial_quality_rating": ratings["financial_quality_rating"],
            "risk_rating": ratings["risk_rating"],
            "confidence": ratings["confidence"],
        }

    def resolve_input_paths(self, report_dir: str | Path) -> AnalystInputPaths:
        """
        根据报告目录自动定位财务分析员需要的输入文件。

        参数：
            report_dir: 信息处理员输出的单份报告目录。
        返回值：
            标准化后的输入路径集合。
        """
        base_dir = Path(report_dir).resolve()
        paths = AnalystInputPaths(
            report_dir=base_dir,
            digest_json=base_dir / "llm_digest.json",
            digest_audit=base_dir / "digest_audit.json",
            rag_chunks=base_dir / "rag_index" / "rag_chunks.jsonl",
            summary_comparison_json=base_dir / "summary_comparison.json",
            summary_comparison_md=base_dir / "summary_comparison.md",
            content_json=base_dir / "content.json",
        )
        required_paths = [paths.digest_json, paths.digest_audit, paths.rag_chunks]
        missing_paths = [str(path) for path in required_paths if not path.exists()]
        if missing_paths:
            raise FileNotFoundError("Required financial-analyst inputs are missing: " + "；".join(missing_paths))
        return paths

    def default_output_dir(self, metadata: dict[str, Any]) -> Path:
        """
        按标准目录规则生成财务分析员默认输出目录。

        参数：
            metadata: llm_digest 中的 document_metadata。
        返回值：
            默认输出目录路径。
        """
        report_type = safe_path_part(str(metadata.get("report_type") or "annual"))
        report_year = safe_path_part(str(metadata.get("report_year") or "unknown_year"))
        stock_code = safe_path_part(str(metadata.get("stock_code") or "unknown_code"))
        pdf_stem = safe_path_part(str(metadata.get("pdf_stem") or f"{stock_code}-{report_year}"))
        return self.workspace / "reports" / report_type / report_year / stock_code / pdf_stem

    def build_input_audit(
        self,
        digest_audit: dict[str, Any],
        summary_comparison: dict[str, Any],
        input_paths: AnalystInputPaths,
        rag_chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        生成输入质量审计结论。

        参数：
            digest_audit: 信息处理员生成的 digest_audit.json。
            summary_comparison: 摘要覆盖比对结果，可为空。
            input_paths: 输入路径集合。
            rag_chunks: RAG chunk 列表。
        返回值：
            analyst_report.json 中的 input_audit 字典。
        """
        coverage = summary_comparison.get("coverage", {}) if summary_comparison else {}
        missing_chunks = digest_audit.get("missing_chunks", []) or []
        invalid_results = digest_audit.get("invalid_results", []) or []
        limitations: list[str] = []
        if missing_chunks:
            limitations.append(f"The digest has {len(missing_chunks)} missing chunks; the report can be used only as a preliminary analysis.")
        if invalid_results:
            limitations.append(f"The digest has {len(invalid_results)} invalid results and requires information-processor review.")
        if not rag_chunks:
            limitations.append("The RAG evidence index is empty, so key figures cannot be verified at the evidence layer.")
        digest_number_coverage = coverage.get("digest_number_coverage")
        rag_number_coverage = coverage.get("rag_number_coverage")
        if isinstance(digest_number_coverage, (int, float)) and digest_number_coverage < 0.8:
            limitations.append("Representative summary figures have less than 80% digest coverage; some figures require RAG or content.json verification.")
        if isinstance(rag_number_coverage, (int, float)) and rag_number_coverage < 0.9:
            limitations.append("Representative summary figures have less than 90% RAG coverage; the information processor should review the RAG build.")

        input_quality = "high"
        if missing_chunks or invalid_results or not rag_chunks:
            input_quality = "low"
        elif limitations:
            input_quality = "medium"

        return {
            "digest_complete": bool(digest_audit.get("complete")),
            "missing_chunks": missing_chunks,
            "invalid_results": invalid_results,
            "summary_keyword_coverage": format_ratio(coverage.get("digest_keyword_coverage")),
            "summary_number_coverage": format_ratio(coverage.get("digest_number_coverage")),
            "rag_keyword_coverage": format_ratio(coverage.get("rag_keyword_coverage")),
            "rag_number_coverage": format_ratio(coverage.get("rag_number_coverage")),
            "rag_chunk_count": len(rag_chunks),
            "input_quality": input_quality,
            "limitations": limitations,
            "input_files": {
                "llm_digest_json": str(input_paths.digest_json),
                "digest_audit_json": str(input_paths.digest_audit),
                "rag_chunks_jsonl": str(input_paths.rag_chunks),
                "summary_comparison_json": str(input_paths.summary_comparison_json) if input_paths.summary_comparison_json.exists() else "",
                "summary_comparison_md": str(input_paths.summary_comparison_md) if input_paths.summary_comparison_md.exists() else "",
                "content_json": str(input_paths.content_json) if input_paths.content_json.exists() else "",
            },
        }

    def build_financial_metrics(
        self,
        findings: list[DigestFinding],
        metadata: dict[str, Any],
        ref_builder: SourceRefBuilder,
    ) -> dict[str, Any]:
        """
        从 digest 中抽取核心财务指标。

        参数：
            findings: 标准化后的 digest 发现列表。
            metadata: 报告元数据。
            ref_builder: 证据引用生成器。
        返回值：
            analyst_report.json 中的 financial_metrics 字典。
        """
        report_year = str(metadata.get("report_year") or "")
        previous_year = str(int(report_year) - 1) if report_year.isdigit() else ""
        metrics: dict[str, Any] = {}
        for metric_key, definition in METRIC_DEFINITIONS.items():
            metrics[metric_key] = extract_metric(metric_key, definition, findings, report_year, previous_year, ref_builder)
        return metrics

    def build_business_profile(self, findings: list[DigestFinding], ref_builder: SourceRefBuilder) -> dict[str, Any]:
        """
        生成公司业务画像。

        参数：
            findings: 标准化后的 digest 发现列表。
            ref_builder: 证据引用生成器。
        返回值：
            业务画像字典。
        """
        business_findings = select_findings(findings, BUSINESS_KEYWORDS, limit=6)
        source_refs = [ref_from_finding(finding, ref_builder) for finding in business_findings[:4]]
        return {
            "main_business": summarize_findings(business_findings, "The current digest does not contain a sufficiently clear description of the main business.", max_items=3),
            "revenue_drivers": bullet_summaries(select_findings(findings, ["收入结构", "营业收入", "产品", "地区"], limit=4)),
            "industry_context": bullet_summaries(select_findings(findings, ["行业", "市场需求", "竞争", "电力", "新能源"], limit=4)),
            "business_model_notes": bullet_summaries(select_findings(findings, ["EPC", "EPCOS", "业务模式", "综合能源"], limit=4)),
            "source_refs": deduplicate(source_refs),
        }

    def build_analysis_sections(
        self,
        findings: list[DigestFinding],
        metrics: dict[str, Any],
        ref_builder: SourceRefBuilder,
    ) -> dict[str, Any]:
        """
        生成六大财务分析章节。

        参数：
            findings: 标准化后的 digest 发现列表。
            metrics: 核心财务指标。
            ref_builder: 证据引用生成器。
        返回值：
            analyst_report.json 中的 analysis_sections 字典。
        """
        profitability_findings = select_findings(findings, PROFIT_KEYWORDS, limit=8)
        growth_findings = select_findings(findings, ["增长", "下降", "业务规模", "需求", "季度", "收入"], limit=8)
        cashflow_findings = select_findings(findings, CASHFLOW_KEYWORDS, limit=8)
        balance_findings = select_findings(findings, BALANCE_SHEET_KEYWORDS, limit=8)
        capital_findings = select_findings(findings, CAPITAL_ALLOCATION_KEYWORDS, limit=8)
        governance_findings = select_findings(findings, GOVERNANCE_KEYWORDS, limit=8)

        return {
            "profitability": {
                "view": build_profitability_view(metrics, profitability_findings),
                "positive_factors": positive_factors(profitability_findings),
                "negative_factors": negative_factors(profitability_findings),
                "source_refs": refs_from_findings(profitability_findings[:5], ref_builder),
            },
            "growth_quality": {
                "view": build_growth_view(metrics, growth_findings),
                "positive_factors": positive_factors(growth_findings),
                "negative_factors": negative_factors(growth_findings),
                "source_refs": refs_from_findings(growth_findings[:5], ref_builder),
            },
            "cash_flow_quality": {
                "view": build_cashflow_view(metrics, cashflow_findings),
                "red_flags": risk_summaries(cashflow_findings),
                "source_refs": refs_from_findings(cashflow_findings[:5], ref_builder),
            },
            "balance_sheet_quality": {
                "view": build_balance_sheet_view(metrics, balance_findings),
                "red_flags": risk_summaries(balance_findings),
                "source_refs": refs_from_findings(balance_findings[:5], ref_builder),
            },
            "capital_allocation": {
                "view": build_capital_allocation_view(capital_findings),
                "dividend": find_first_summary(capital_findings, ["利润分配", "现金红利", "分红"]),
                "buyback": find_first_summary(capital_findings, ["回购"]),
                "financing": find_first_summary(capital_findings, ["融资", "筹资"]),
                "capex": find_first_summary(capital_findings, ["资本开支", "投资"]),
                "source_refs": refs_from_findings(capital_findings[:5], ref_builder),
            },
            "governance_and_audit": {
                "view": build_governance_view(governance_findings),
                "audit_opinion": infer_audit_opinion(governance_findings),
                "internal_control": infer_internal_control(governance_findings),
                "red_flags": risk_summaries(governance_findings),
                "source_refs": refs_from_findings(governance_findings[:5], ref_builder),
            },
        }

    def build_evidence_check(
        self,
        metrics: dict[str, Any],
        findings: list[DigestFinding],
        rag_chunks: list[dict[str, Any]],
        ref_builder: SourceRefBuilder,
    ) -> dict[str, Any]:
        """
        用 RAG 证据层核验核心指标和关键事项。

        参数：
            metrics: 核心财务指标。
            findings: 标准化后的 digest 发现列表。
            rag_chunks: RAG chunk 列表。
            ref_builder: 证据引用生成器。
        返回值：
            evidence_check.json 的内容。
        """
        checked_items: list[dict[str, Any]] = []
        unverified_items: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []

        metric_to_query = {
            "revenue": "营业收入",
            "net_profit_attributable": "归属于上市公司股东的净利润",
            "deducted_net_profit": "扣除非经常性损益的净利润",
            "operating_cash_flow": "经营活动产生的现金流量净额",
            "gross_margin": "毛利率",
            "roe": "加权平均净资产收益率",
            "accounts_receivable": "应收账款",
            "inventory": "存货",
            "goodwill": "商誉",
            "interest_bearing_debt": "短期借款 长期借款 有息负债",
            "cash_and_cash_equivalents": "货币资金",
        }
        for metric_key, query in metric_to_query.items():
            metric = metrics.get(metric_key, {}) or {}
            current_value = ((metric.get("current") or {}).get("value") or "")
            claim = build_metric_claim(metric.get("label", metric_key), metric)
            search_query = f"{query} {current_value}" if current_value else query
            hits = rank_rag_chunks(rag_chunks, search_query, top_k=3)
            rag_refs = [rag_ref(hit, ref_builder) for hit in hits]
            status = "verified" if hits and (not current_value or any(value_token_matches(current_value, hit.get("text", "")) for hit in hits)) else "unverified"
            item = {
                "item": metric.get("label", metric_key),
                "claim": claim,
                "status": status,
                "digest_refs": metric.get("source_refs", []),
                "rag_refs": rag_refs,
                "notes": "RAG matched the core metric or a table on the same page." if status == "verified" else "RAG did not retrieve direct evidence for this metric. This does not mean the filing omitted it; verify against content.json or the original PDF.",
            }
            checked_items.append(item)
            if status != "verified":
                unverified_items.append(item)

        for label, aliases in CORE_RAG_QUERIES.items():
            related_findings = select_findings(findings, aliases, limit=2)
            if not related_findings:
                continue
            hits = rank_rag_chunks(rag_chunks, " ".join(aliases), top_k=3)
            rag_refs = [rag_ref(hit, ref_builder) for hit in hits]
            digest_refs = refs_from_findings(related_findings, ref_builder)
            status = "verified" if hits else "unverified"
            item = {
                "item": label,
                "claim": related_findings[0].summary[:220],
                "status": status,
                "digest_refs": digest_refs,
                "rag_refs": rag_refs,
                "notes": "RAG matched relevant evidence." if status == "verified" else "RAG did not retrieve direct evidence for this item; additional evidence is required.",
            }
            checked_items.append(item)
            if status != "verified":
                unverified_items.append(item)

        return {
            "checked_items": checked_items,
            "unverified_items": unverified_items,
            "conflicts": conflicts,
            "summary": {
                "checked_total": len(checked_items),
                "verified_total": len([item for item in checked_items if item["status"] == "verified"]),
                "unverified_total": len(unverified_items),
                "conflict_total": len(conflicts),
            },
        }

    def build_risks(self, findings: list[DigestFinding], ref_builder: SourceRefBuilder) -> list[dict[str, Any]]:
        """
        汇总 digest 中的主要风险。

        参数：
            findings: 标准化后的 digest 发现列表。
            ref_builder: 证据引用生成器。
        返回值：
            风险条目列表。
        """
        risk_findings = [finding for finding in findings if finding.item_type == "risk" and not is_non_material_risk_disclosure(finding.topic, finding.summary)]
        deduped: list[DigestFinding] = []
        seen_topics: set[str] = set()
        for finding in sorted(risk_findings, key=lambda item: (-SEVERITY_SCORE.get(item.severity, 0), min(item.pages or [9999]))):
            topic_key = normalize_space(finding.topic)[:30]
            if topic_key in seen_topics:
                continue
            seen_topics.add(topic_key)
            deduped.append(finding)
            if len(deduped) >= 12:
                break
        return [
            {
                "risk_type": finding.topic,
                "severity": finding.severity or "unknown",
                "description": finding.summary,
                "financial_impact": infer_financial_impact(finding),
                "source_refs": [ref_from_finding(finding, ref_builder)],
            }
            for finding in deduped
        ]

    def build_opportunities(self, findings: list[DigestFinding], ref_builder: SourceRefBuilder) -> list[dict[str, Any]]:
        """
        从业务与经营发现中提取潜在改善信号。

        参数：
            findings: 标准化后的 digest 发现列表。
            ref_builder: 证据引用生成器。
        返回值：
            机会条目列表。
        """
        candidates = select_findings(
            findings,
            ["平台", "研发", "综合能源", "智能", "客户", "新能源", "储能", "微电网", "改善", "增长"],
            limit=8,
        )
        opportunities: list[dict[str, Any]] = []
        for finding in candidates:
            if finding.item_type == "risk":
                continue
            opportunities.append(
                {
                    "opportunity_type": finding.topic,
                    "strength": "medium" if any(keyword in finding.summary for keyword in ["增长", "提升", "超过", "累计", "客户"]) else "low",
                    "description": finding.summary,
                    "source_refs": [ref_from_finding(finding, ref_builder)],
                }
            )
            if len(opportunities) >= 5:
                break
        return opportunities

    def build_ratings(
        self,
        metrics: dict[str, Any],
        findings: list[DigestFinding],
        input_audit: dict[str, Any],
        evidence_check: dict[str, Any],
    ) -> dict[str, Any]:
        """
        基于财务指标、风险和输入质量生成标准评级。

        参数：
            metrics: 核心财务指标。
            findings: 标准化后的 digest 发现列表。
            input_audit: 输入审计结论。
            evidence_check: 证据核验结果。
        返回值：
            包含基本面、财务质量、风险和置信度的评级字典。
        """
        revenue_yoy = metric_yoy_float(metrics.get("revenue", {}))
        net_profit = metric_current_float(metrics.get("net_profit_attributable", {}))
        deducted_profit = metric_current_float(metrics.get("deducted_net_profit", {}))
        ocf = metric_current_float(metrics.get("operating_cash_flow", {}))
        ocf_yoy = metric_yoy_float(metrics.get("operating_cash_flow", {}))
        material_risks = [finding for finding in findings if finding.item_type == "risk" and not is_non_material_risk_disclosure(finding.topic, finding.summary)]
        risk_text = json.dumps([asdict(finding) for finding in material_risks], ensure_ascii=False)
        all_text = json.dumps([asdict(finding) for finding in findings], ensure_ascii=False)

        negative_points = 0
        key_reasons: list[str] = []
        if revenue_yoy is not None and revenue_yoy < -20:
            negative_points += 2
            key_reasons.append(f"Revenue declined materially year over year ({format_number_for_reason(revenue_yoy, '%')}).")
        if net_profit is not None and net_profit < 0:
            negative_points += 2
            key_reasons.append("Net profit attributable to shareholders is negative, indicating a loss or a shift from profit to loss.")
        if deducted_profit is not None and deducted_profit < 0:
            negative_points += 1
            key_reasons.append("Recurring net profit attributable to shareholders is negative, indicating pressure on core earnings quality.")
        if ocf is not None and ocf > 0:
            key_reasons.append("Operating cash flow remains positive and provides some buffer against the loss.")
        if ocf_yoy is not None and ocf_yoy < -40:
            negative_points += 1
            key_reasons.append(f"Operating cash flow declined materially year over year ({format_number_for_reason(ocf_yoy, '%')}); collections and working-capital quality require continued monitoring.")
        if "会计差错" in all_text or "追溯调整" in all_text:
            negative_points += 1
            key_reasons.append("Prior-period accounting corrections or retrospective adjustments exist; revenue recognition and historical comparability require focused review.")
        if any(keyword in risk_text for keyword in ["高", "high", "应收", "合同资产", "减值"]):
            negative_points += 1
            key_reasons.append("The digest flags receivables, contract assets, or impairment risks; asset quality requires attention.")

        severe_risk_count = len([finding for finding in material_risks if SEVERITY_SCORE.get(finding.severity, 0) >= 4])
        if severe_risk_count >= 3:
            negative_points += 1
            key_reasons.append(f"The digest contains {severe_risk_count} high-severity risks.")

        if negative_points >= 6:
            fundamental_rating = "strong_negative"
            financial_quality_rating = "low"
            risk_rating = "high"
        elif negative_points >= 4:
            fundamental_rating = "negative"
            financial_quality_rating = "medium_low"
            risk_rating = "medium_high"
        elif negative_points >= 2:
            fundamental_rating = "neutral"
            financial_quality_rating = "medium"
            risk_rating = "medium"
        else:
            fundamental_rating = "positive"
            financial_quality_rating = "medium_high"
            risk_rating = "medium_low"

        if has_actual_nonstandard_audit_signal(all_text):
            risk_rating = max_rating(risk_rating, "high")
            key_reasons.append("The text contains non-standard wording that may affect the audit or internal-control opinion; confirm the original context.")

        verified_total = int((evidence_check.get("summary") or {}).get("verified_total") or 0)
        checked_total = int((evidence_check.get("summary") or {}).get("checked_total") or 0)
        verified_ratio = verified_total / checked_total if checked_total else 0
        if input_audit.get("input_quality") == "low" or verified_ratio < 0.6:
            confidence = "low"
        elif input_audit.get("input_quality") == "medium" or verified_ratio < 0.8:
            confidence = "medium"
        else:
            confidence = "high"

        return {
            "fundamental_rating": fundamental_rating,
            "financial_quality_rating": financial_quality_rating,
            "risk_rating": risk_rating,
            "confidence": confidence,
            "key_reasons": deduplicate(key_reasons)[:8],
        }

    def build_open_questions(
        self,
        metrics: dict[str, Any],
        findings: list[DigestFinding],
        input_audit: dict[str, Any],
        focus: str,
    ) -> list[dict[str, Any]]:
        """
        生成后续需要补证或跟踪的问题。

        参数：
            metrics: 核心财务指标。
            findings: 标准化后的 digest 发现列表。
            input_audit: 输入审计结论。
            focus: 用户指定的重点方向。
        返回值：
            open_questions 列表。
        """
        questions: list[dict[str, Any]] = []
        if input_audit.get("limitations"):
            questions.append(
                {
                    "question": "Could representative figures missing from the digest affect the core financial conclusions?",
                    "why_it_matters": "The digest is a primary financial-analysis input; insufficient coverage lowers confidence in automated conclusions.",
                    "suggested_evidence": "Ask the information processor to list high-value missing_in_digest figures in summary_comparison.json and state whether RAG covers them.",
                }
            )
        if metric_yoy_float(metrics.get("revenue", {})) is not None and metric_yoy_float(metrics.get("revenue", {})) < -20:
            questions.append(
                {
                    "question": "Did the material revenue decline show signs of recovery in the 2026 Q1 report or order backlog?",
                    "why_it_matters": "The persistence of the revenue decline determines whether this is a cyclical trough, temporary disruption, or fundamental deterioration.",
                    "suggested_evidence": "Add quarterly reports, order announcements, and management commentary on demand and competition.",
                }
            )
        if metric_current_float(metrics.get("net_profit_attributable", {})) is not None and metric_current_float(metrics.get("net_profit_attributable", {})) < 0:
            questions.append(
                {
                    "question": "How much of the current loss comes from one-off impairment or non-recurring items versus core gross-margin and expense pressure?",
                    "why_it_matters": "The nature of the loss determines whether a turnaround thesis is researchable.",
                    "suggested_evidence": "Add asset-impairment details, credit-impairment details, product-level gross margins, and management explanations for changes.",
                }
            )
        all_text = json.dumps([asdict(finding) for finding in findings], ensure_ascii=False)
        if "会计差错" in all_text or "追溯调整" in all_text:
            questions.append(
                {
                    "question": "Have prior-period accounting corrections been remediated, and are there regulatory inquiries or continuing revenue-recognition disputes?",
                    "why_it_matters": "Accounting errors affect historical comparability, revenue-recognition quality, and governance risk assessment.",
                    "suggested_evidence": "Add special correction statements, auditor explanations, regulatory inquiry letters, and responses.",
                }
            )
        if any(keyword in all_text for keyword in ["应收账款", "合同资产", "回款", "减值"]):
            questions.append(
                {
                    "question": "Are aging, customer concentration, subsequent collections, and impairment allowances for receivables and contract assets adequate?",
                    "why_it_matters": "This directly affects earnings quality, cash-flow quality, and balance-sheet risk.",
                    "suggested_evidence": "Add receivables aging, bad-debt allowance schedules, contract-asset details, and subsequent collection data.",
                }
            )
        if focus:
            questions.insert(
                0,
                {
                    "question": f"Does the user-specified focus {focus} require targeted evidence?",
                    "why_it_matters": "Targeted analysis prevents a generic report from missing the investment variables the user actually cares about.",
                    "suggested_evidence": "Search RAG specifically for the focus parameter and ask the information processor to verify content.json when necessary.",
                },
            )
        return dedupe_question_list(questions)[:8]

    def build_upstream_requests(
        self,
        metadata: dict[str, Any],
        input_audit: dict[str, Any],
        open_questions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        根据证据缺口生成给信息处理员的结构化请求建议。

        参数：
            metadata: 报告元数据。
            input_audit: 输入审计结论。
            open_questions: 待核验问题。
        返回值：
            upstream_requests 列表。
        """
        requests: list[dict[str, Any]] = []
        if input_audit.get("missing_chunks") or input_audit.get("invalid_results"):
            requests.append(
                {
                    "request_id": f"REQ-{datetime.now().strftime('%Y%m%d')}-0001",
                    "to_agent": "information_processor",
                    "request_type": "Rerun or Add Evidence",
                    "priority": "high",
                    "blocking": True,
                    "status": "suggested",
                    "company": build_company_payload(metadata),
                    "question": "The digest has missing or invalid chunks. Complete them and regenerate llm_digest.",
                    "why_it_matters": "An incomplete digest reduces the reliability of financial-analysis conclusions.",
                    "impact_on_analysis": "The current report can be used only as a low-confidence preliminary analysis.",
                }
            )
        elif input_audit.get("input_quality") == "medium" and open_questions:
            requests.append(
                {
                    "request_id": f"REQ-{datetime.now().strftime('%Y%m%d')}-0001",
                    "to_agent": "information_processor",
                    "request_type": "Non-Blocking Evidence Request",
                    "priority": "medium",
                    "blocking": False,
                    "status": "suggested",
                    "company": build_company_payload(metadata),
                    "question": open_questions[0]["question"],
                    "why_it_matters": open_questions[0]["why_it_matters"],
                    "impact_on_analysis": "RAG coverage is adequate, so this does not block the current report, but more evidence can improve confidence in later versions.",
                }
            )
        return requests

    def build_decision_signals(
        self,
        ratings: dict[str, Any],
        metrics: dict[str, Any],
        findings: list[DigestFinding],
        ref_builder: SourceRefBuilder,
    ) -> dict[str, Any]:
        """
        为下游不同风格买卖决策员生成结构化信号。

        参数：
            ratings: 财务分析评级。
            metrics: 核心财务指标。
            findings: 标准化后的 digest 发现列表。
            ref_builder: 证据引用生成器。
        返回值：
            decision_signals 字典。
        """
        risk_rating = ratings.get("risk_rating")
        fundamental_rating = ratings.get("fundamental_rating")
        net_profit = metric_current_float(metrics.get("net_profit_attributable", {}))
        revenue_yoy = metric_yoy_float(metrics.get("revenue", {}))
        dividend_text = json.dumps([asdict(finding) for finding in select_findings(findings, ["利润分配", "现金红利", "分红"], limit=5)], ensure_ascii=False)
        signals: list[dict[str, Any]] = []

        if fundamental_rating in {"negative", "strong_negative"}:
            signals.append(
                {
                    "signal_type": "Fundamental Pressure",
                    "direction": "negative",
                    "reason": "Revenue decline, losses, or concentrated high-severity risks are present; downstream decision-makers should verify recovery evidence first.",
                    "source_refs": refs_from_findings(select_findings(findings, ["营业收入", "净利润", "亏损"], limit=2), ref_builder),
                }
            )
        if net_profit is not None and net_profit < 0:
            signals.append(
                {
                    "signal_type": "Earnings Quality",
                    "direction": "negative",
                    "reason": "Net profit attributable to shareholders is negative; the company should not be screened directly as a stable-earnings candidate.",
                    "source_refs": metrics.get("net_profit_attributable", {}).get("source_refs", []),
                }
            )
        if revenue_yoy is not None and revenue_yoy < -20:
            signals.append(
                {
                    "signal_type": "Growth",
                    "direction": "negative",
                    "reason": "Revenue declined materially year over year; growth-oriented decision-makers should wait for revenue recovery or order validation.",
                    "source_refs": metrics.get("revenue", {}).get("source_refs", []),
                }
            )
        if "不派发现金红利" in dividend_text or "不分配" in dividend_text:
            signals.append(
                {
                    "signal_type": "Dividend",
                    "direction": "negative",
                    "reason": "The reporting-period distribution plan pays no cash dividend, so the company is unsuitable as a dividend-cash-flow candidate.",
                    "source_refs": refs_from_findings(select_findings(findings, ["利润分配", "现金红利"], limit=2), ref_builder),
                }
            )
        if risk_rating in {"medium_high", "high", "critical"}:
            signals.append(
                {
                    "signal_type": "Risk Preference",
                    "direction": "negative",
                    "reason": "Risk is elevated; risk-averse decision-makers should avoid by default or require stronger evidence.",
                    "source_refs": refs_from_findings([finding for finding in findings if finding.item_type == "risk" and not is_non_material_risk_disclosure(finding.topic, finding.summary)][:3], ref_builder),
                }
            )

        return {
            "suitable_for_value_investor": "caution" if fundamental_rating in {"neutral", "negative", "strong_negative"} else "possible",
            "suitable_for_growth_investor": "unfavorable" if revenue_yoy is not None and revenue_yoy < 0 else "unknown",
            "suitable_for_dividend_investor": infer_dividend_suitability(dividend_text),
            "suitable_for_turnaround_investor": "possible_with_high_due_diligence" if fundamental_rating in {"negative", "strong_negative"} else "unknown",
            "avoid_for_risk_averse_investor": "yes" if risk_rating in {"medium_high", "high", "critical"} else "not_necessarily",
            "signals": signals,
        }

    def build_analyst_audit(
        self,
        input_paths: AnalystInputPaths,
        input_audit: dict[str, Any],
        evidence_check: dict[str, Any],
        upstream_requests: list[dict[str, Any]],
        report_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        生成本次财务分析过程的审计文件。

        参数：
            input_paths: 输入路径集合。
            input_audit: 输入审计结论。
            evidence_check: 证据核验结果。
            upstream_requests: 上游补证请求建议。
            report_payload: 已生成的分析报告主体。
        返回值：
            analyst_audit.json 的内容。
        """
        evidence_summary = evidence_check.get("summary", {}) or {}
        return {
            "generated_at": beijing_now(),
            "input_files": input_audit.get("input_files", {}),
            "input_complete": bool(input_paths.digest_json.exists() and input_paths.digest_audit.exists() and input_paths.rag_chunks.exists()),
            "analysis_complete": True,
            "evidence_checks_total": evidence_summary.get("checked_total", 0),
            "evidence_checks_verified": evidence_summary.get("verified_total", 0),
            "evidence_conflicts": evidence_check.get("conflicts", []),
            "upstream_requests_total": len(upstream_requests),
            "upstream_requests_blocking": len([request for request in upstream_requests if request.get("blocking")]),
            "upstream_requests_resolved": len([request for request in upstream_requests if request.get("status") == "completed"]),
            "limitations": report_payload.get("analysis_metadata", {}).get("limitations", []),
            "recommended_next_steps": build_recommended_next_steps(report_payload),
        }


def flatten_digest_findings(digest: dict[str, Any]) -> list[DigestFinding]:
    """
    将 llm_digest.json 的分段结果压平为统一发现列表。

    参数：
        digest: llm_digest.json 内容。
    返回值：
        DigestFinding 列表。
    """
    findings: list[DigestFinding] = []
    for result in digest.get("results", []) or []:
        chunk_id = str(result.get("chunk_id") or "")
        pages = normalize_pages(result.get("pages") or [])
        section = str(result.get("detected_section") or "")
        for finding in result.get("key_findings", []) or []:
            source_pages = normalize_pages(finding.get("source_pages") or pages)
            findings.append(
                DigestFinding(
                    item_type="finding",
                    topic=str(finding.get("topic") or "Unnamed Finding"),
                    summary=normalize_space(str(finding.get("summary") or "")),
                    numbers=list(finding.get("numbers") or []),
                    pages=source_pages,
                    chunk_id=chunk_id,
                    section=section,
                )
            )
        for risk in result.get("risks", []) or []:
            source_pages = normalize_pages(risk.get("source_pages") or pages)
            risk_type = str(risk.get("risk_type") or "Unnamed Risk")
            risk_summary = normalize_space(str(risk.get("summary") or ""))
            if is_non_material_risk_disclosure(risk_type, risk_summary):
                continue
            findings.append(
                DigestFinding(
                    item_type="risk",
                    topic=risk_type,
                    summary=risk_summary,
                    numbers=[],
                    pages=source_pages,
                    chunk_id=chunk_id,
                    section=section,
                    severity=normalize_risk_severity(risk_type, risk_summary, str(risk.get("severity") or "unknown")),
                )
            )
    return [finding for finding in findings if finding.summary or finding.topic]


def extract_metric(
    metric_key: str,
    definition: dict[str, Any],
    findings: list[DigestFinding],
    report_year: str,
    previous_year: str,
    ref_builder: SourceRefBuilder,
) -> dict[str, Any]:
    """
    基于指标别名从 digest 数字中抽取当前值、上期值和同比值。

    参数：
        metric_key: 指标字段名。
        definition: 指标定义。
        findings: 标准化后的 digest 发现列表。
        report_year: 报告年度。
        previous_year: 上一年度。
        ref_builder: 证据引用生成器。
    返回值：
        标准指标字典。
    """
    aliases = definition.get("aliases", []) or []
    exclude = definition.get("exclude", []) or []
    ratio_metric = bool(definition.get("ratio_metric"))
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        context = f"{finding.topic} {finding.summary}"
        if not text_matches_alias(context, aliases) and not any(text_matches_alias(str(number.get("name", "")), aliases) for number in finding.numbers):
            continue
        for number in finding.numbers:
            name = str(number.get("name") or "")
            if not text_matches_alias(name, aliases):
                continue
            number_context = f"{context} {name} {number.get('period', '')} {number.get('value', '')} {number.get('unit', '')}"
            if not text_matches_alias(number_context, aliases):
                continue
            if any(word in name for word in exclude):
                continue
            if is_year_noise_metric_value(metric_key, number):
                continue
            if metric_key in {"revenue", "net_profit_attributable", "deducted_net_profit", "operating_cash_flow", "total_assets", "equity", "cash_and_cash_equivalents", "accounts_receivable", "contract_assets", "inventory", "goodwill"}:
                if is_ratio_number(number) and not is_yoy_number(number, report_year, previous_year):
                    continue
            candidate_ref = ref_builder.add_ref(
                source_type="digest",
                pages=finding.pages or normalize_pages(number.get("source_pages") or []),
                chunk_id=finding.chunk_id,
                quote=f"{finding.topic}：{finding.summary}",
                section=finding.section,
            )
            candidates.append(
                {
                    "number": number,
                    "finding": finding,
                    "ref_id": candidate_ref,
                    "score": score_metric_candidate(number, finding, aliases, report_year, previous_year, ratio_metric),
                }
            )

    current = select_metric_number(candidates, "current", report_year, previous_year, ratio_metric)
    previous = select_metric_number(candidates, "previous", report_year, previous_year, ratio_metric)
    yoy = select_metric_number(candidates, "yoy", report_year, previous_year, ratio_metric)
    if not current and metric_key in {"revenue", "net_profit_attributable", "deducted_net_profit", "operating_cash_flow"}:
        fallback_metric = extract_metric_from_rag_source(metric_key, definition, findings, report_year, ref_builder)
        if fallback_metric:
            return fallback_metric

    source_refs = deduplicate([item["ref_id"] for item in [current, previous, yoy] if item])
    unit = infer_metric_unit(current, previous, yoy)
    return {
        "label": definition.get("label", metric_key),
        "current": render_metric_number(current),
        "previous": render_metric_number(previous),
        "yoy": render_metric_number(yoy),
        "unit": unit,
        "source_refs": source_refs,
        "extraction_status": "found" if current or previous or yoy else "not_found",
    }


def select_metric_number(
    candidates: list[dict[str, Any]],
    target: str,
    report_year: str,
    previous_year: str,
    ratio_metric: bool,
) -> dict[str, Any] | None:
    """
    从候选数字中选择最符合当前值、上期值或同比值的一项。

    参数：
        candidates: 候选数字列表。
        target: current、previous 或 yoy。
        report_year: 报告年度。
        previous_year: 上一年度。
        ratio_metric: 是否为比例指标。
    返回值：
        最佳候选数字；找不到则返回 None。
    """
    matched: list[dict[str, Any]] = []
    for candidate in candidates:
        number = candidate["number"]
        if target == "current" and is_current_period_number(number, report_year, previous_year, ratio_metric):
            matched.append(candidate)
        elif target == "previous" and is_previous_period_number(number, report_year, previous_year):
            matched.append(candidate)
        elif target == "yoy" and is_yoy_number(number, report_year, previous_year):
            matched.append(candidate)
    if not matched:
        return None
    matched.sort(key=lambda item: (-float(item["score"]), min(item["finding"].pages or [9999])))
    return matched[0]


def is_year_noise_metric_value(metric_key: str, number: dict[str, Any]) -> bool:
    """
    判断指标候选值是否只是年度数字噪声。

    参数：
        metric_key: 指标字段名。
        number: 数字记录。
    返回值：
        属于年份噪声返回 True，否则返回 False。
    """
    if metric_key not in {
        "revenue",
        "net_profit_attributable",
        "deducted_net_profit",
        "operating_cash_flow",
        "gross_margin",
        "total_assets",
        "equity",
        "cash_and_cash_equivalents",
        "accounts_receivable",
        "contract_assets",
        "inventory",
        "goodwill",
        "interest_bearing_debt",
    }:
        return False
    value = str(number.get("value") or "").replace(",", "")
    unit = str(number.get("unit") or "")
    return value in {"2023", "2024", "2025", "2026"} and unit in {"", "%", "股", "元", "万元", "原文未明确单位"}


def is_current_period_number(number: dict[str, Any], report_year: str, previous_year: str, ratio_metric: bool) -> bool:
    """
    判断一个数字是否代表报告期当前值。

    参数：
        number: 数字记录。
        report_year: 报告年度。
        previous_year: 上一年度。
        ratio_metric: 是否为比例指标。
    返回值：
        是当前值返回 True，否则返回 False。
    """
    period = str(number.get("period") or "")
    name = str(number.get("name") or "")
    text = f"{name} {period} {number.get('value', '')} {number.get('unit', '')}"
    if is_yoy_text(text, report_year, previous_year):
        return False
    if is_quarter_period(period):
        return False
    if previous_year and previous_year in period and report_year not in period:
        return False
    if report_year and report_year in period:
        return True
    if "本报告期" in text or "期末" in text:
        return True
    return False


def is_previous_period_number(number: dict[str, Any], report_year: str, previous_year: str) -> bool:
    """
    判断一个数字是否代表上年或上期期末值。

    参数：
        number: 数字记录。
        report_year: 报告年度。
        previous_year: 上一年度。
    返回值：
        是上期值返回 True，否则返回 False。
    """
    period = str(number.get("period") or "")
    name = str(number.get("name") or "")
    text = f"{name} {period} {number.get('value', '')} {number.get('unit', '')}"
    if is_yoy_text(text, report_year, previous_year) or is_quarter_period(period):
        return False
    return bool(previous_year and previous_year in period and report_year not in period)


def is_quarter_period(period: str) -> bool:
    """
    判断期间字段是否属于季度数据。

    参数：
        period: 数字记录中的期间字段。
    返回值：
        季度数据返回 True，否则返回 False。
    """
    return any(keyword in period for keyword in ["第一季度", "第二季度", "第三季度", "第四季度", "一季度", "二季度", "三季度", "四季度"])


def is_yoy_number(number: dict[str, Any], report_year: str, previous_year: str) -> bool:
    """
    判断一个数字是否代表同比变化。

    参数：
        number: 数字记录。
        report_year: 报告年度。
        previous_year: 上一年度。
    返回值：
        是同比值返回 True，否则返回 False。
    """
    text = f"{number.get('name', '')} {number.get('period', '')} {number.get('value', '')} {number.get('unit', '')}"
    return is_yoy_text(text, report_year, previous_year)


def is_yoy_text(text: str, report_year: str, previous_year: str) -> bool:
    """
    判断文本是否描述同比或增减变化。

    参数：
        text: 数字上下文文本。
        report_year: 报告年度。
        previous_year: 上一年度。
    返回值：
        描述同比变化返回 True，否则返回 False。
    """
    yoy_words = ["同比", "比上年", "本报告期比", "增减", "增长", "下降", "减少", "增加", "变动"]
    if any(word in text for word in yoy_words) and ("%" in text or "百分点" in text):
        return True
    return bool(report_year and previous_year and report_year in text and previous_year in text and "%" in text)


def is_ratio_number(number: dict[str, Any]) -> bool:
    """
    判断数字是否为比例型数字。

    参数：
        number: 数字记录。
    返回值：
        比例型返回 True，否则返回 False。
    """
    return "%" in str(number.get("value") or "") or "%" in str(number.get("unit") or "") or "百分点" in str(number.get("unit") or "")


def score_metric_candidate(
    number: dict[str, Any],
    finding: DigestFinding,
    aliases: list[str],
    report_year: str,
    previous_year: str,
    ratio_metric: bool,
) -> float:
    """
    给指标候选数字打分，优先选择口径更直接、页码更靠前的主表数字。

    参数：
        number: 数字记录。
        finding: 来源发现。
        aliases: 指标别名。
        report_year: 报告年度。
        previous_year: 上一年度。
        ratio_metric: 是否为比例指标。
    返回值：
        候选分数。
    """
    name = str(number.get("name") or "")
    period = str(number.get("period") or "")
    value = str(number.get("value") or "")
    text = f"{finding.topic} {finding.summary} {name} {period} {value} {number.get('unit', '')}"
    score = 0.0
    for alias in aliases:
        if name == alias:
            score += 20
        elif alias in name:
            score += 12
        elif alias in text:
            score += 4
    if report_year and report_year in period:
        score += 4
    if previous_year and previous_year in period:
        score += 2
    if is_yoy_text(text, report_year, previous_year):
        score += 2
    if is_quarter_period(period):
        score -= 20
    if "年度" in period or period == f"{report_year}年":
        score += 5
    if any(word in text for word in ["主要会计数据", "主要财务指标", "年度主要会计数据"]):
        score += 4
    if ratio_metric and is_ratio_number(number):
        score += 3
    if not ratio_metric and not is_ratio_number(number):
        score += 3
    if min(finding.pages or [9999]) <= 12:
        score += 2
    if any(word in name for word in ["调增", "调减", "扣除项目", "占比", "门槛"]):
        score -= 8
    if "合计" in name:
        score += 1
    return score


def render_metric_number(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    将候选数字渲染为报告中的标准指标字段。

    参数：
        candidate: 指标候选项。
    返回值：
        标准数字字典；没有候选时返回 None。
    """
    if not candidate:
        return None
    number = candidate["number"]
    finding = candidate["finding"]
    return {
        "name": number.get("name", ""),
        "value": number.get("value", ""),
        "period": number.get("period", ""),
        "unit": number.get("unit", ""),
        "source_pages": normalize_pages(number.get("source_pages") or finding.pages),
        "source_refs": [candidate["ref_id"]],
    }


def infer_metric_unit(*items: dict[str, Any] | None) -> str | None:
    """
    从当前值、上期值和同比值中推断指标单位。

    参数：
        items: 若干标准数字字典。
    返回值：
        单位字符串；无法推断时返回 None。
    """
    for item in items:
        if item and item.get("number"):
            unit = item["number"].get("unit")
            if unit:
                return str(unit)
    return None


def extract_metric_from_rag_source(
    metric_key: str,
    definition: dict[str, Any],
    findings: list[DigestFinding],
    report_year: str,
    ref_builder: SourceRefBuilder,
) -> dict[str, Any] | None:
    """
    从 digest 摘要文本中按表格片段兜底抽取核心指标。

    参数：
        metric_key: 指标字段名。
        definition: 指标定义。
        findings: 标准化后的 digest 发现列表。
        report_year: 报告年度。
        ref_builder: 证据引用生成器。
    返回值：
        指标字典；无法可靠兜底时返回 None。
    """
    if metric_key not in {"revenue", "net_profit_attributable", "deducted_net_profit", "operating_cash_flow"}:
        return None
    aliases = definition.get("aliases", []) or []
    for finding in findings:
        text = normalize_space(f"{finding.topic} {finding.summary}")
        if not any(alias in text for alias in aliases):
            continue
        match = find_metric_value_in_text(metric_key, aliases, text)
        if not match:
            continue
        ref_id = ref_from_finding(finding, ref_builder)
        current = {
            "name": definition.get("label", metric_key),
            "value": match["value"],
            "period": f"{report_year}年" if report_year else "报告期",
            "unit": match["unit"],
            "source_pages": finding.pages,
            "source_refs": [ref_id],
        }
        return {
            "label": definition.get("label", metric_key),
            "current": current,
            "previous": None,
            "yoy": None,
            "unit": match["unit"],
            "source_refs": [ref_id],
            "extraction_status": "found",
        }
    return None


def find_metric_value_in_text(metric_key: str, aliases: list[str], text: str) -> dict[str, str] | None:
    """
    在表格化文本片段中抽取指定指标的报告期数值。

    参数：
        metric_key: 指标字段名。
        aliases: 指标别名。
        text: 待解析文本。
    返回值：
        value/unit 字典；无法可靠匹配时返回 None。
    """
    unit = infer_unit_from_text(text)
    if metric_key == "revenue":
        patterns = [r"营业收入[^\d]{0,40}(\d{1,3}(?:,\d{3})+(?:\.\d+)?)", r"营业收入\s*(\d{4,}(?:\.\d+)?)"]
    elif metric_key == "net_profit_attributable":
        patterns = [r"归属于上市公司股东的净利润[^\d-]{0,40}(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?)", r"归母净利润[^\d-]{0,40}(-?\d{4,}(?:\.\d+)?)"]
    elif metric_key == "deducted_net_profit":
        patterns = [r"扣除非经常性损益[^\d-]{0,60}(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?)", r"扣非[^\d-]{0,40}(-?\d{4,}(?:\.\d+)?)"]
    else:
        patterns = [r"经营活动产生的现金流量净额[^\d-]{0,60}(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?)", r"经营现金流[^\d-]{0,40}(-?\d{4,}(?:\.\d+)?)"]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1)
            if value.replace(",", "") in {"2025", "2024", "2023"}:
                continue
            return {"value": value, "unit": unit}
    return None


def infer_unit_from_text(text: str) -> str:
    """
    从文本片段推断财务指标单位。

    参数：
        text: 财报文本片段。
    返回值：
        单位说明。
    """
    if "单位：百万元" in text or "单位:百万元" in text:
        return "百万元"
    if "单位：万元" in text or "单位:万元" in text:
        return "万元"
    if "单位：元" in text or "单位:元" in text:
        return "元"
    return "原文未明确单位"


def rank_rag_chunks(rag_chunks: list[dict[str, Any]], query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """
    使用确定性关键词规则在 RAG chunk 中检索证据。

    参数：
        rag_chunks: RAG chunk 列表。
        query: 检索问题或关键词。
        top_k: 返回条数。
    返回值：
        排序后的命中 chunk 列表。
    """
    query_terms = expand_query_terms(query)
    query_numbers = extract_number_tokens(query)
    results: list[dict[str, Any]] = []
    for chunk in rag_chunks:
        text = normalize_space(str(chunk.get("text") or ""))
        if not text:
            continue
        score = 0.0
        matched_terms: list[str] = []
        if query and query in text:
            score += 8.0
            matched_terms.append(query)
        for term in query_terms:
            count = text.count(term)
            if count:
                score += 2.0 + math.log1p(count)
                matched_terms.append(term)
        for number in query_numbers:
            if value_token_matches(number, text):
                score += 3.0
                matched_terms.append(number)
        score += PRIORITY_SCORE.get(str(chunk.get("priority_hint") or "").upper(), 0) * 0.4
        if str(chunk.get("chunk_type") or "") in {"metric", "table"}:
            score += 1.0
        if score <= 0:
            continue
        results.append(
            {
                **chunk,
                "score": round(score, 4),
                "matched_terms": deduplicate(matched_terms),
                "snippet": make_snippet(text, query_terms),
            }
        )
    results.sort(key=lambda item: (-float(item["score"]), min(normalize_pages(item.get("pages") or []) or [9999])))
    return results[:top_k]


def rag_ref(hit: dict[str, Any], ref_builder: SourceRefBuilder) -> str:
    """
    把 RAG 命中结果登记为证据引用。

    参数：
        hit: RAG 检索命中。
        ref_builder: 证据引用生成器。
    返回值：
        证据引用编号。
    """
    return ref_builder.add_ref(
        source_type="rag",
        pages=normalize_pages(hit.get("pages") or []),
        chunk_id=str(hit.get("chunk_id") or ""),
        chunk_type=str(hit.get("chunk_type") or ""),
        section=str(hit.get("section") or ""),
        quote=str(hit.get("snippet") or hit.get("text") or "")[:500],
        confidence="high" if float(hit.get("score") or 0) >= 8 else "medium",
    )


def build_company_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    """
    构建公司基础信息输出。

    参数：
        metadata: document_metadata。
    返回值：
        公司信息字典。
    """
    report_year = str(metadata.get("report_year") or "")
    report_type = str(metadata.get("report_type") or "")
    period_labels = {
        "annual": f"Fiscal Year {report_year}",
        "q1": f"{report_year} First Quarter (3M cumulative)",
        "semiannual": f"{report_year} First Half (6M cumulative)",
        "q3": f"{report_year} First Nine Months (9M cumulative)",
    }
    return {
        "stock_code": str(metadata.get("stock_code") or ""),
        "company_name": str(metadata.get("company_name") or ""),
        "report_year": report_year,
        "report_type": report_type,
        "report_period": period_labels.get(report_type, report_year),
        "pdf_stem": str(metadata.get("pdf_stem") or ""),
        "announcement_id": str(metadata.get("announcement_id") or ""),
        "published_at": str(metadata.get("published_at") or ""),
    }


def build_overall_view(ratings: dict[str, Any], metrics: dict[str, Any], findings: list[DigestFinding]) -> str:
    """
    生成结论摘要中的总体观点。

    参数：
        ratings: 标准评级。
        metrics: 核心财务指标。
        findings: 标准化后的 digest 发现列表。
    返回值：
        总体观点文本。
    """
    revenue = metrics.get("revenue", {})
    net_profit = metrics.get("net_profit_attributable", {})
    ocf = metrics.get("operating_cash_flow", {})
    parts = [
        f"Fundamental rating: {ratings.get('fundamental_rating')}; financial quality rating: {ratings.get('financial_quality_rating')}; risk rating: {ratings.get('risk_rating')}.",
        f"Revenue {metric_brief(revenue)}; net profit attributable to shareholders {metric_brief(net_profit)}; operating cash flow {metric_brief(ocf)}.",
    ]
    high_risks = [
        finding
        for finding in findings
        if finding.item_type == "risk"
        and SEVERITY_SCORE.get(finding.severity, 0) >= 4
        and not is_non_material_risk_disclosure(finding.topic, finding.summary)
    ]
    if high_risks:
        parts.append("Primary pressure comes from: " + "；".join(finding.topic for finding in high_risks[:4]) + "。")
    return "".join(parts)


def build_profitability_view(metrics: dict[str, Any], findings: list[DigestFinding]) -> str:
    """
    生成盈利能力分析结论。

    参数：
        metrics: 核心财务指标。
        findings: 相关发现。
    返回值：
        盈利能力观点。
    """
    revenue_yoy = metric_yoy_float(metrics.get("revenue", {}))
    net_profit = metric_current_float(metrics.get("net_profit_attributable", {}))
    deducted_profit = metric_current_float(metrics.get("deducted_net_profit", {}))
    if net_profit is not None and net_profit < 0:
        return "Net profit attributable to shareholders is negative, and recurring profit also requires close attention. Earnings are under material pressure, with revenue decline, gross margin, and impairment jointly affecting performance."
    if revenue_yoy is not None and revenue_yoy < 0:
        return "Revenue declined year over year; profitability requires further decomposition across gross margin, expense ratios, and impairment losses."
    if deducted_profit is not None and deducted_profit > 0:
        return "Recurring profit remains positive, but earnings quality still requires confirmation through cash flow and asset quality."
    return summarize_findings(findings, "The current digest does not provide sufficiently complete profitability evidence.", max_items=2)


def build_growth_view(metrics: dict[str, Any], findings: list[DigestFinding]) -> str:
    """
    生成增长质量分析结论。

    参数：
        metrics: 核心财务指标。
        findings: 相关发现。
    返回值：
        增长质量观点。
    """
    revenue_yoy = metric_yoy_float(metrics.get("revenue", {}))
    if revenue_yoy is not None and revenue_yoy < -20:
        return "Revenue declined materially year over year, so the company is not currently in a high-quality growth state. Determine whether the decline reflects industry demand, stronger competition, project timing, or weakening competitiveness."
    if revenue_yoy is not None and revenue_yoy > 10:
        return "Revenue is growing rapidly, but confirm whether cash flow and receivables quality are improving in parallel."
    return summarize_findings(findings, "Growth-quality evidence is insufficient; add multi-period data and order information.", max_items=2)


def build_cashflow_view(metrics: dict[str, Any], findings: list[DigestFinding]) -> str:
    """
    生成现金流质量分析结论。

    参数：
        metrics: 核心财务指标。
        findings: 相关发现。
    返回值：
        现金流质量观点。
    """
    ocf = metric_current_float(metrics.get("operating_cash_flow", {}))
    ocf_yoy = metric_yoy_float(metrics.get("operating_cash_flow", {}))
    net_profit = metric_current_float(metrics.get("net_profit_attributable", {}))
    if ocf is not None and net_profit is not None and ocf > 0 > net_profit:
        if ocf_yoy is not None and ocf_yoy < -40:
            return "Operating cash flow remains positive and exceeds loss-making earnings, but declined materially year over year. Cash flow is not out of control, although its quality has weakened significantly at the margin."
        return "Operating cash flow is positive and stronger than loss-making earnings, providing some buffer against the profit decline."
    if ocf is not None and ocf < 0:
        return "Operating cash flow is negative, creating a clear red flag for earnings-to-cash conversion quality."
    return summarize_findings(findings, "Cash-flow evidence is insufficient; add drivers of operating cash-flow changes and the collection structure.", max_items=2)


def build_balance_sheet_view(metrics: dict[str, Any], findings: list[DigestFinding]) -> str:
    """
    生成资产负债表质量分析结论。

    参数：
        metrics: 核心财务指标。
        findings: 相关发现。
    返回值：
        资产负债表质量观点。
    """
    text = json.dumps([asdict(finding) for finding in findings], ensure_ascii=False)
    flags: list[str] = []
    for keyword in ["应收账款", "合同资产", "减值", "货币资金", "短期借款", "长期借款", "商誉", "存货"]:
        if keyword in text:
            flags.append(keyword)
    if flags:
        return "The balance sheet requires close monitoring of " + "、".join(deduplicate(flags)[:6]) + ", with particular attention to collections, impairment allowances, and true liquidity."
    return "The current digest does not show sufficient evidence of balance-sheet anomalies, but receivables, inventory, goodwill, and interest-bearing debt still require RAG verification."


def build_capital_allocation_view(findings: list[DigestFinding]) -> str:
    """
    生成资本配置分析结论。

    参数：
        findings: 资本配置相关发现。
    返回值：
        资本配置观点。
    """
    text = json.dumps([asdict(finding) for finding in findings], ensure_ascii=False)
    if "不派发现金红利" in text or "不分配" in text:
        return "The reporting-period distribution plan pays no cash dividend, issues no bonus shares, and makes no capital conversion. The capital-return signal is weak and should be interpreted alongside losses and cash-flow pressure."
    if findings:
        return summarize_findings(findings, "Capital-allocation information is limited.", max_items=2)
    return "The current digest does not contain sufficiently complete information on dividends, buybacks, financing, and capital expenditure."


def build_governance_view(findings: list[DigestFinding]) -> str:
    """
    生成治理与审计分析结论。

    参数：
        findings: 治理审计相关发现。
    返回值：
        治理与审计观点。
    """
    text = json.dumps([asdict(finding) for finding in findings], ensure_ascii=False)
    if "会计差错" in text or "追溯调整" in text:
        return "Governance and audit analysis should focus on prior-period accounting corrections and retrospective adjustments, which affect revenue recognition, historical comparability, and financial-reporting quality."
    if "标准无保留" in text and "内部控制重大缺陷" not in text:
        return "Audit and internal-control disclosures show no major anomaly, but reporting quality still requires assessment against key audit matters and accounting estimates."
    return summarize_findings(findings, "Governance and audit information is insufficient; add the audit opinion, key audit matters, and internal-control assessment.", max_items=2)


def infer_audit_opinion(findings: list[DigestFinding]) -> str | None:
    """
    从治理审计发现中推断审计意见。

    参数：
        findings: 治理审计相关发现。
    返回值：
        审计意见文本；无法判断时返回 None。
    """
    text = json.dumps([asdict(finding) for finding in findings], ensure_ascii=False)
    if "标准无保留" in text:
        return "Standard unqualified opinion"
    if "不存在非标准审计意见" in text or "未涉及非标准审计意见" in text:
        return "No non-standard audit opinion identified"
    if "非标准审计意见" in text:
        return "Wording related to a non-standard audit opinion appears; confirm against the original audit report"
    return None


def infer_internal_control(findings: list[DigestFinding]) -> str | None:
    """
    从治理审计发现中推断内部控制状态。

    参数：
        findings: 治理审计相关发现。
    返回值：
        内控状态文本；无法判断时返回 None。
    """
    text = json.dumps([asdict(finding) for finding in findings], ensure_ascii=False)
    if "未发现内部控制重大缺陷" in text:
        return "No material internal-control weakness identified during the reporting period"
    if "内部控制重大缺陷" in text:
        return "Wording related to a material internal-control weakness appears; review the original context"
    return None


def render_analyst_markdown(report: dict[str, Any]) -> str:
    """
    将结构化证据草稿渲染为人类可读 Markdown。

    参数：
        report: analyst_report.json 的内容。
    返回值：
        Markdown 文本。
    """
    company = report.get("company", {})
    summary = report.get("executive_summary", {})
    audit = report.get("input_audit", {})
    metrics = report.get("financial_metrics", {})
    sections = report.get("analysis_sections", {})
    lines: list[str] = []
    title_prefix = "Financial Analysis Evidence Draft"
    if audit.get("input_quality") != "high":
        title_prefix = "Financial Analysis Evidence Draft (Input Limitations Apply)"
    lines.append(f"# {title_prefix}：{company.get('stock_code', '')} {company.get('company_name', '')} {company.get('report_period', '')}")
    lines.append("")
    lines.append("> This rule-based file is generated from the information-processor digest and RAG evidence for LLM financial-analyst review. It is an evidence draft, not a formal research conclusion or investment advice.")
    lines.append("")
    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append(f"- Overall view: {summary.get('overall_view', '')}")
    lines.append(f"- Fundamental rating: {summary.get('fundamental_rating', '')}")
    lines.append(f"- Financial quality rating: {summary.get('financial_quality_rating', '')}")
    lines.append(f"- Risk rating: {summary.get('risk_rating', '')}")
    lines.append(f"- Confidence: {summary.get('confidence', '')}")
    lines.append("- Core reasons:")
    for reason in summary.get("key_reasons", []) or []:
        lines.append(f"  - {reason}")
    lines.append("")
    lines.append("## 2. Input Quality and Audit Status")
    lines.append("")
    lines.append(f"- digest_complete：{audit.get('digest_complete')}")
    lines.append(f"- input_quality：{audit.get('input_quality')}")
    lines.append(f"- digest_keyword_coverage：{audit.get('summary_keyword_coverage')}")
    lines.append(f"- digest_number_coverage：{audit.get('summary_number_coverage')}")
    lines.append(f"- rag_keyword_coverage：{audit.get('rag_keyword_coverage')}")
    lines.append(f"- rag_number_coverage：{audit.get('rag_number_coverage')}")
    for limitation in audit.get("limitations", []) or []:
        lines.append(f"- Limitation: {limitation}")
    lines.append("")
    lines.append("## 3. Business and Revenue Sources")
    lines.append("")
    business = report.get("business_profile", {})
    lines.append(str(business.get("main_business", "")))
    append_bullets(lines, "Revenue Drivers", business.get("revenue_drivers", []))
    append_bullets(lines, "Industry Context", business.get("industry_context", []))
    append_bullets(lines, "Business Model", business.get("business_model_notes", []))
    lines.append("")
    lines.append("## 4. Core Financial Metrics")
    lines.append("")
    lines.append("| Metric | Current | Prior | YoY / Change | Evidence |")
    lines.append("|---|---:|---:|---:|---|")
    for key in ["revenue", "net_profit_attributable", "deducted_net_profit", "gross_margin", "roe", "eps", "operating_cash_flow", "total_assets", "equity", "cash_and_cash_equivalents", "accounts_receivable", "contract_assets", "inventory", "goodwill", "interest_bearing_debt"]:
        metric = metrics.get(key, {}) or {}
        lines.append(
            "| "
            + str(metric.get("label", key))
            + " | "
            + metric_cell(metric.get("current"))
            + " | "
            + metric_cell(metric.get("previous"))
            + " | "
            + metric_cell(metric.get("yoy"))
            + " | "
            + ", ".join(metric.get("source_refs", []) or [])
            + " |"
        )
    section_titles = [
        ("## 5. Profitability Analysis", "profitability"),
        ("## 6. Growth Quality Analysis", "growth_quality"),
        ("## 7. Cash-Flow Quality Analysis", "cash_flow_quality"),
        ("## 8. Balance-Sheet Quality Analysis", "balance_sheet_quality"),
        ("## 9. Capital Allocation Analysis", "capital_allocation"),
        ("## 10. Audit, Governance, and Compliance Risk", "governance_and_audit"),
    ]
    for title, key in section_titles:
        lines.append("")
        lines.append(title)
        lines.append("")
        section = sections.get(key, {}) or {}
        lines.append(str(section.get("view", "")))
        if section.get("positive_factors"):
            append_bullets(lines, "Positive Factors", section.get("positive_factors", []))
        if section.get("negative_factors"):
            append_bullets(lines, "Negative Factors", section.get("negative_factors", []))
        if section.get("red_flags"):
            append_bullets(lines, "Red Flags", section.get("red_flags", []))
        if key == "capital_allocation":
            for field in ["dividend", "buyback", "financing", "capex"]:
                if section.get(field):
                    lines.append(f"- {field}: {section.get(field)}")
        if key == "governance_and_audit":
            lines.append(f"- Audit opinion: {section.get('audit_opinion')}")
            lines.append(f"- Internal control: {section.get('internal_control')}")
        if section.get("source_refs"):
            lines.append(f"- Evidence: {', '.join(section.get('source_refs', []))}")
    lines.append("")
    lines.append("## 11. Key Risks")
    lines.append("")
    for risk in report.get("risks", []) or []:
        lines.append(f"- **{risk.get('risk_type')}** ({risk.get('severity')}): {risk.get('description')} Evidence: {', '.join(risk.get('source_refs', []))}")
    lines.append("")
    lines.append("## 12. Opportunities and Improvement Signals")
    lines.append("")
    for opportunity in report.get("opportunities", []) or []:
        lines.append(f"- **{opportunity.get('opportunity_type')}** ({opportunity.get('strength')}): {opportunity.get('description')} Evidence: {', '.join(opportunity.get('source_refs', []))}")
    lines.append("")
    lines.append("## 13. Signals for Downstream Decision-Makers")
    lines.append("")
    decision = report.get("decision_signals", {})
    lines.append(f"- Value style: {decision.get('suitable_for_value_investor')}")
    lines.append(f"- Growth style: {decision.get('suitable_for_growth_investor')}")
    lines.append(f"- Dividend cash-flow style: {decision.get('suitable_for_dividend_investor')}")
    lines.append(f"- Turnaround style: {decision.get('suitable_for_turnaround_investor')}")
    lines.append(f"- Avoid for risk-averse investors: {decision.get('avoid_for_risk_averse_investor')}")
    for signal in decision.get("signals", []) or []:
        lines.append(f"  - {signal.get('signal_type')} / {signal.get('direction')}：{signal.get('reason')}")
    lines.append("")
    lines.append("## 14. Information Gaps and Follow-Up Questions")
    lines.append("")
    for question in report.get("open_questions", []) or []:
        lines.append(f"- Question: {question.get('question')}")
        lines.append(f"  - Why it matters: {question.get('why_it_matters')}")
        lines.append(f"  - Suggested evidence: {question.get('suggested_evidence')}")
    if report.get("upstream_requests"):
        lines.append("")
        lines.append("### Suggested Evidence Requests for the Information Processor")
        for request in report.get("upstream_requests", []) or []:
            lines.append(f"- {request.get('request_id')}（{request.get('request_type')}，blocking={request.get('blocking')}）：{request.get('question')}")
    lines.append("")
    lines.append("## 15. Evidence Index")
    lines.append("")
    for ref in report.get("source_ref_index", []) or []:
        pages = ",".join(str(page) for page in ref.get("pages", []) or [])
        lines.append(f"- {ref.get('ref_id')}：{ref.get('source_type')}；chunk={ref.get('chunk_id')}；pages={pages}；quote={ref.get('quote')}")
    lines.append("")
    return "\n".join(lines)


def refs_from_findings(findings: list[DigestFinding], ref_builder: SourceRefBuilder) -> list[str]:
    """
    批量将 digest 发现登记为证据引用。

    参数：
        findings: digest 发现列表。
        ref_builder: 证据引用生成器。
    返回值：
        证据引用编号列表。
    """
    return deduplicate([ref_from_finding(finding, ref_builder) for finding in findings])


def ref_from_finding(finding: DigestFinding, ref_builder: SourceRefBuilder) -> str:
    """
    将单条 digest 发现登记为证据引用。

    参数：
        finding: digest 发现。
        ref_builder: 证据引用生成器。
    返回值：
        证据引用编号。
    """
    return ref_builder.add_ref(
        source_type="digest",
        pages=finding.pages,
        chunk_id=finding.chunk_id,
        quote=f"{finding.topic}：{finding.summary}",
        section=finding.section,
        confidence="high",
    )


def select_findings(findings: list[DigestFinding], keywords: list[str], limit: int = 5) -> list[DigestFinding]:
    """
    按关键词从 digest 发现中选择最相关条目。

    参数：
        findings: digest 发现列表。
        keywords: 关键词列表。
        limit: 最多返回条数。
    返回值：
        相关发现列表。
    """
    scored: list[tuple[int, DigestFinding]] = []
    for finding in findings:
        text = f"{finding.topic} {finding.summary} {json.dumps(finding.numbers, ensure_ascii=False)}"
        score = sum(1 for keyword in keywords if keyword and keyword in text)
        if score:
            if finding.item_type == "risk":
                score += 1
            scored.append((score, finding))
    scored.sort(key=lambda item: (-item[0], min(item[1].pages or [9999])))
    result: list[DigestFinding] = []
    seen: set[str] = set()
    for _, finding in scored:
        key = f"{finding.topic}:{finding.summary[:80]}"
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
        if len(result) >= limit:
            break
    return result


def summarize_findings(findings: list[DigestFinding], fallback: str, max_items: int = 3) -> str:
    """
    将若干发现压缩为一段报告文字。

    参数：
        findings: digest 发现列表。
        fallback: 无发现时的兜底表述。
        max_items: 最多拼接条数。
    返回值：
        摘要文字。
    """
    if not findings:
        return fallback
    return "；".join(f"{finding.topic}：{finding.summary}" for finding in findings[:max_items])


def bullet_summaries(findings: list[DigestFinding]) -> list[str]:
    """
    将发现列表转换为短 bullet 文本。

    参数：
        findings: digest 发现列表。
    返回值：
        bullet 文本列表。
    """
    return [f"{finding.topic}：{finding.summary}" for finding in findings]


def positive_factors(findings: list[DigestFinding]) -> list[str]:
    """
    从相关发现中提取正面因素。

    参数：
        findings: 相关发现列表。
    返回值：
        正面因素列表。
    """
    positives: list[str] = []
    for finding in findings:
        text = f"{finding.topic} {finding.summary}"
        if any(keyword in text for keyword in ["增长", "提升", "改善", "为正", "客户", "平台", "未发现", "不存在重大"]):
            positives.append(f"{finding.topic}：{finding.summary}")
    return positives[:5]


def negative_factors(findings: list[DigestFinding]) -> list[str]:
    """
    从相关发现中提取负面因素。

    参数：
        findings: 相关发现列表。
    返回值：
        负面因素列表。
    """
    negatives: list[str] = []
    for finding in findings:
        text = f"{finding.topic} {finding.summary} {finding.severity}"
        if finding.item_type == "risk" or any(keyword in text for keyword in ["下降", "减少", "亏损", "为负", "承压", "减值", "风险", "差错"]):
            negatives.append(f"{finding.topic}：{finding.summary}")
    return negatives[:5]


def risk_summaries(findings: list[DigestFinding]) -> list[str]:
    """
    从发现中提取红旗事项。

    参数：
        findings: 相关发现列表。
    返回值：
        红旗事项列表。
    """
    return negative_factors(findings)[:6]


def find_first_summary(findings: list[DigestFinding], keywords: list[str]) -> str | None:
    """
    查找第一个命中关键词的发现摘要。

    参数：
        findings: 相关发现列表。
        keywords: 关键词列表。
    返回值：
        发现摘要；未命中则返回 None。
    """
    for finding in findings:
        text = f"{finding.topic} {finding.summary}"
        if any(keyword in text for keyword in keywords):
            return f"{finding.topic}：{finding.summary}"
    return None


def infer_financial_impact(finding: DigestFinding) -> str:
    """
    根据风险主题推断可能财务影响。

    参数：
        finding: 风险发现。
    返回值：
        财务影响描述。
    """
    text = f"{finding.topic} {finding.summary}"
    if any(keyword in text for keyword in ["应收", "回款", "坏账", "信用减值"]):
        return "May affect operating cash flow, bad-debt allowances, and earnings quality."
    if any(keyword in text for keyword in ["存货", "跌价"]):
        return "May create inventory write-down losses and depress gross margin."
    if any(keyword in text for keyword in ["商誉", "减值", "固定资产"]):
        return "May create asset-impairment losses and reduce net profit."
    if any(keyword in text for keyword in ["亏损", "盈利", "毛利"]):
        return "May directly affect income-statement performance and the valuation base."
    if any(keyword in text for keyword in ["会计差错", "追溯", "收入确认"]):
        return "May affect historical comparability and the reliability of revenue recognition."
    if any(keyword in text for keyword in ["担保", "诉讼", "资金占用", "违规"]):
        return "May create contingent liabilities, cash outflows, or a governance discount."
    return "May affect the fundamental assessment and requires verification against the original text and subsequent announcements."


def build_metric_claim(label: str, metric: dict[str, Any]) -> str:
    """
    将指标字典转换为证据核验用 claim。

    参数：
        label: 指标名称。
        metric: 指标字典。
    返回值：
        claim 文本。
    """
    current = metric.get("current") or {}
    yoy = metric.get("yoy") or {}
    if current:
        claim = f"{label} current value is {current.get('value')} ({current.get('period')}, unit: {current.get('unit')})"
        if yoy:
            claim += f"; YoY/change is {yoy.get('value')}"
        return claim
    return f"{label} current value was not reliably extracted from the digest."


def build_method_limitations(input_audit: dict[str, Any], evidence_check: dict[str, Any]) -> list[str]:
    """
    汇总本轮自动分析方法限制。

    参数：
        input_audit: 输入审计结论。
        evidence_check: 证据核验结果。
    返回值：
        限制说明列表。
    """
    limitations = list(input_audit.get("limitations", []) or [])
    unverified_total = int((evidence_check.get("summary") or {}).get("unverified_total") or 0)
    if unverified_total:
        limitations.append(f"{unverified_total} evidence checks did not directly match RAG and require manual or information-processor follow-up.")
    limitations.append("This file is generated by deterministic extraction and ranking rules. It is only an evidence draft and verification checklist for the LLM financial analyst, not a formal research conclusion.")
    limitations.append("Formal financial analysis must be produced by the LLM agent after multi-round research using the digest, RAG, evidence responses, and contrary-evidence checks.")
    limitations.append("This draft does not include market price, valuation, industry peers, or post-filing events, so it does not provide trading, position-sizing, or target-price conclusions.")
    return deduplicate(limitations)


def build_recommended_next_steps(report_payload: dict[str, Any]) -> list[str]:
    """
    生成分析完成后的建议后续动作。

    参数：
        report_payload: 证据草稿主体。
    返回值：
        建议动作列表。
    """
    steps = ["Ask the information processor to address high-priority items in open_questions."]
    rating = (report_payload.get("executive_summary") or {}).get("risk_rating")
    if rating in {"medium_high", "high", "critical"}:
        steps.append("Downstream decision-makers should apply risk constraints before considering style-specific views.")
    steps.append("Before the portfolio-manager stage, add valuation, industry peers, market price, and the latest announcements.")
    return steps


def append_bullets(lines: list[str], title: str, items: list[str]) -> None:
    """
    向 Markdown 行列表追加小节 bullet。

    参数：
        lines: Markdown 行列表。
        title: 小节标题。
        items: bullet 内容。
    返回值：
        无。
    """
    if not items:
        return
    lines.append("")
    lines.append(f"**{title}**")
    for item in items:
        lines.append(f"- {item}")


def metric_cell(metric_number: dict[str, Any] | None) -> str:
    """
    渲染 Markdown 指标表中的单元格。

    参数：
        metric_number: 标准数字字段。
    返回值：
        Markdown 单元格文本。
    """
    if not metric_number:
        return "Not Extracted"
    value = metric_number.get("value", "")
    unit = metric_number.get("unit", "")
    period = metric_number.get("period", "")
    return f"{format_value_with_unit(value, unit)}（{period}）"


def metric_brief(metric: dict[str, Any]) -> str:
    """
    将指标转换为摘要短语。

    参数：
        metric: 指标字典。
    返回值：
        指标短语。
    """
    current = metric.get("current") or {}
    yoy = metric.get("yoy") or {}
    if not current:
        return "Not Reliably Extracted"
    text = format_value_with_unit(current.get("value", ""), current.get("unit", ""))
    if yoy:
        text += "; change " + format_value_with_unit(yoy.get("value", ""), yoy.get("unit", ""))
    return text


def format_value_with_unit(value: Any, unit: Any) -> str:
    """
    合并指标值和单位，避免百分号或中文单位重复。

    参数：
        value: 指标值。
        unit: 指标单位。
    返回值：
        合并后的指标文本。
    """
    value_text = str(value or "")
    unit_text = str(unit or "")
    if not unit_text or value_text.endswith(unit_text) or (unit_text == "%" and "%" in value_text):
        return value_text
    return f"{value_text}{unit_text}"



def metric_current_float(metric: dict[str, Any]) -> float | None:
    """
    读取指标当前值并转为浮点数。

    参数：
        metric: 指标字典。
    返回值：
        浮点数；无法解析返回 None。
    """
    current = metric.get("current") or {}
    return parse_float(current.get("value"))


def metric_yoy_float(metric: dict[str, Any]) -> float | None:
    """
    读取指标同比值并转为浮点数。

    参数：
        metric: 指标字典。
    返回值：
        浮点数；无法解析返回 None。
    """
    yoy = metric.get("yoy") or {}
    return parse_float(yoy.get("value"))


def parse_float(value: Any) -> float | None:
    """
    从字符串中解析数字。

    参数：
        value: 原始数字值。
    返回值：
        float；无法解析返回 None。
    """
    text = str(value or "")
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def value_token_matches(value: str, text: str) -> bool:
    """
    判断数值 token 是否在文本中出现，兼容逗号和百分号写法。

    参数：
        value: 待匹配值。
        text: 被检索文本。
    返回值：
        命中返回 True，否则返回 False。
    """
    if not value:
        return False
    raw = str(value)
    normalized_value = raw.replace(",", "")
    normalized_text = str(text).replace(",", "")
    return raw in text or normalized_value in normalized_text


def format_number_for_reason(value: float | None, unit: str = "") -> str:
    """
    将数字格式化为理由文本。

    参数：
        value: 数字值。
        unit: 单位。
    返回值：
        格式化文本。
    """
    if value is None:
        return "Unknown"
    return f"{value:.2f}{unit}"


def max_rating(current: str, candidate: str) -> str:
    """
    在风险评级中取更高风险的评级。

    参数：
        current: 当前评级。
        candidate: 候选评级。
    返回值：
        更高风险评级。
    """
    order = {"unknown": 0, "low": 1, "medium_low": 2, "medium": 3, "medium_high": 4, "high": 5, "critical": 6}
    return candidate if order.get(candidate, 0) > order.get(current, 0) else current


def is_non_material_risk_disclosure(risk_type: str, summary: str) -> bool:
    """
    判断风险条目是否只是年报模板化否定披露。

    参数：
        risk_type: 风险主题。
        summary: 风险摘要。
    返回值：
        属于非实质性风险披露返回 True，否则返回 False。
    """
    text = normalize_space(f"{risk_type} {summary}")
    negative_patterns = [
        "□适用√不适用",
        "□适用 √不适用",
        "不适用",
        "不存在",
        "无重大诉讼",
        "无重大仲裁",
        "本年度公司无重大诉讼",
        "本年度公司无重大诉讼、仲裁事项",
        "违规担保情况 □适用√不适用",
        "非经营性占用资金",
        "诚信状况良好",
        "不存在未弥补亏损",
    ]
    if any(pattern in text for pattern in negative_patterns):
        material_overrides = ["带强调事项", "重大缺陷", "立案", "逾期债务", "已发生", "计提", "损失"]
        return not any(keyword in text for keyword in material_overrides)
    if "不存在除债券外的有息债务逾期" in text:
        return True
    if "只是限定了用途，并未被冻结、质押" in text:
        return True
    if "仲裁员" in text and "重大仲裁" not in text:
        return True
    if "审计程序" in text or "错报风险" in text:
        return True
    if "关键审计事项" in text and "固有风险" in text:
        return True
    return False


def normalize_risk_severity(risk_type: str, summary: str, severity: str) -> str:
    """
    对规则抽取产生的风险等级进行保守归一。

    参数：
        risk_type: 风险主题。
        summary: 风险摘要。
        severity: 原始风险等级。
    返回值：
        修正后的风险等级。
    """
    text = normalize_space(f"{risk_type} {summary}")
    if "关键审计事项" in text and "固有风险" in text:
        return "medium"
    if severity == "high" and not any(keyword in text for keyword in ["重大", "逾期债务", "处罚", "立案", "亏损", "减值", "无法", "否定意见", "保留意见"]):
        return "medium"
    return severity or "unknown"


def infer_dividend_suitability(dividend_text: str) -> str:
    """
    根据利润分配文本判断Dividend型决策员适配度。

    参数：
        dividend_text: 与利润分配相关的 digest 文本。
    返回值：
        favorable、unfavorable 或 unknown。
    """
    if "不派发现金红利" in dividend_text or "不分配" in dividend_text:
        return "unfavorable"
    if any(keyword in dividend_text for keyword in ["现金红利", "派发", "分红总额", "每股派发"]):
        return "favorable"
    return "unknown"


def has_actual_nonstandard_audit_signal(text: str) -> bool:
    """
    判断文本是否存在实际非标准审计或内控意见信号。

    参数：
        text: 已合并的报告文本。
    返回值：
        存在实际非标信号返回 True，否则返回 False。
    """
    negative_contexts = [
        "标准无保留意见",
        "不存在非标准审计意见",
        "未涉及非标准审计意见",
        "不是财务报表审计报告的非标准审计意见",
        "无保留意见的审计报告",
    ]
    positive_contexts = ["带强调事项段的无保留意见", "否定意见", "无法表示意见", "持续经营重大不确定性"]
    if any(context in text for context in positive_contexts):
        return True
    if re.search(r"(?<!无)保留意见", text) and "标准无保留意见" not in text:
        return True
    if any(context in text for context in negative_contexts):
        return False
    return False



def text_matches_alias(text: str, aliases: list[str]) -> bool:
    """
    判断文本是否命中任一别名。

    参数：
        text: 待匹配文本。
        aliases: 别名列表。
    返回值：
        命中返回 True，否则返回 False。
    """
    return any(alias and alias in text for alias in aliases)


def expand_query_terms(query: str) -> list[str]:
    """
    为 RAG 检索生成基础扩展词。

    参数：
        query: 检索问题。
    返回值：
        去重后的查询词列表。
    """
    terms = [normalize_space(query)]
    for aliases in CORE_RAG_QUERIES.values():
        if any(alias in query for alias in aliases):
            terms.extend(aliases)
    terms.extend(re.findall(r"[A-Za-z0-9_\-.%]+", query))
    terms.extend(re.findall(r"[一-鿿]{2,}", query))
    return deduplicate([term for term in terms if len(term) >= 2])


def extract_number_tokens(text: str) -> list[str]:
    """
    从文本中抽取数字 token。

    参数：
        text: 输入文本。
    返回值：
        数字 token 列表。
    """
    return deduplicate(re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?%?", str(text or "")))


def make_snippet(text: str, terms: list[str], window: int = 180) -> str:
    """
    根据命中词生成证据摘录。

    参数：
        text: 原文文本。
        terms: 命中词列表。
        window: 摘录窗口长度。
    返回值：
        摘录文本。
    """
    if not text:
        return ""
    positions = [text.find(term) for term in terms if term and text.find(term) >= 0]
    if not positions:
        return text[: window * 2]
    start = max(min(positions) - window, 0)
    end = min(min(positions) + window, len(text))
    return text[start:end]


def normalize_pages(pages: Any) -> list[int]:
    """
    标准化页码数组。

    参数：
        pages: 原始页码数据。
    返回值：
        去重排序后的整数页码列表。
    """
    if not isinstance(pages, list):
        return []
    result: list[int] = []
    for page in pages:
        try:
            result.append(int(page))
        except (TypeError, ValueError):
            continue
    return sorted(set(result))


def normalize_space(text: str) -> str:
    """
    清洗空白字符。

    参数：
        text: 原始文本。
    返回值：
        清洗后的文本。
    """
    return re.sub(r"\s+", " ", str(text or "")).strip()


def deduplicate(items: list[Any]) -> list[Any]:
    """
    保序去重。

    参数：
        items: 原始列表。
    返回值：
        去重后的列表。
    """
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def dedupe_question_list(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    对 open_questions 进行保序去重。

    参数：
        questions: 问题列表。
    返回值：
        去重后的问题列表。
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question in questions:
        key = str(question.get("question") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(question)
    return result


def format_ratio(value: Any) -> str:
    """
    将覆盖率数字格式化为百分比文本。

    参数：
        value: 覆盖率数字。
    返回值：
        百分比文本；缺失时返回 unknown。
    """
    if isinstance(value, (int, float)):
        return f"{value * 100:.2f}%"
    return "unknown"


def safe_path_part(value: str) -> str:
    """
    清理路径片段中的非法字符。

    参数：
        value: 原始路径片段。
    返回值：
        安全路径片段。
    """
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip()
    return cleaned or "unknown"


def beijing_now() -> str:
    """
    生成北京时间 ISO 字符串。

    参数：
        无。
    返回值：
        带 +08:00 时区的 ISO 时间。
    """
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    """
    读取 JSON 文件。

    参数：
        path: JSON 路径。
    返回值：
        JSON 字典。
    """
    return json.loads(path.read_text(encoding="utf-8"))


def load_optional_json(path: Path) -> dict[str, Any]:
    """
    读取可选 JSON 文件。

    参数：
        path: JSON 路径。
    返回值：
        文件存在时返回 JSON 字典，否则返回空字典。
    """
    if not path.exists():
        return {}
    return load_json(path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """
    读取 JSONL 文件。

    参数：
        path: JSONL 路径。
    返回值：
        每行 JSON 组成的列表。
    """
    chunks: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        chunks.append(json.loads(line))
    return chunks


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """
    写入格式化 JSON 文件。

    参数：
        path: 输出路径。
        payload: JSON 内容。
    返回值：
        无。
    """
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
