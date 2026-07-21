"""市场上下文采集 v1 命令行入口。

v1 的现实约束是：只能使用 Web Search，没有稳定行情、一致预期或付费数据库接口。
因此本脚本的目标不是生成高置信投资结论，而是把网页搜索结果整理成可追溯的
`market_context_package`，并明确标注它只能作为“市场叙事和预期代理”。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from market_context_collector_scripts.bocha_web_search_client import BochaWebSearchClient
    from market_context_collector_scripts.market_context_query_planner import MarketContextRequest, build_query_plan
except ModuleNotFoundError:  # pragma: no cover - 兼容直接从脚本目录执行。
    from bocha_web_search_client import BochaWebSearchClient
    from market_context_query_planner import MarketContextRequest, build_query_plan


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PACKAGE_FILES = [
    "market_context_package.json",
    "market_context_package.md",
    "market_context_sources.json",
    "collection_audit.json",
]


SOURCE_TIER_RULES = {
    "S": [
        "sse.com.cn",
        "szse.cn",
        "cninfo.com.cn",
        "gov.cn",
        "stats.gov.cn",
        "pbc.gov.cn",
        "csrc.gov.cn",
        "mof.gov.cn",
    ],
    "A": [
        "eastmoney.com",
        "stcn.com",
        "cnstock.com",
        "cs.com.cn",
        "yicai.com",
        "caixin.com",
        "21jingji.com",
        "cls.cn",
        "thepaper.cn",
    ],
    "B": [
        "xueqiu.com",
        "guba.eastmoney.com",
        "10jqka.com.cn",
        "hexun.com",
        "sina.com.cn",
        "qq.com",
        "163.com",
    ],
}


def collect_market_context(
    request: MarketContextRequest,
    *,
    project_root: Path = PROJECT_ROOT,
    output_dir: Path | None = None,
    count_per_query: int = 8,
    freshness: str | None = "oneMonth",
    dry_run: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """执行市场上下文采集并写入标准产物。

    参数：
        request: 市场上下文采集请求。
        project_root: Project root.
        output_dir: 显式输出目录；为空时写入默认 collector_workspace。
        count_per_query: 每个 query 请求的搜索结果数量。
        freshness: 搜索时效范围；None 表示不向 Bocha 发送 freshness。严格截止模式会强制使用 None。
        dry_run: 是否只生成查询计划和空包，不调用外部接口。
        force_refresh: 是否忽略本地 query 缓存。
    返回值：
        包含输出路径、状态和审计信息的字典。
    """
    query_plan = build_query_plan(request)
    target_output_dir = output_dir or default_package_dir(project_root, request)
    # 历史严格模式不能使用 oneMonth 等相对调用日的服务端过滤，否则目标年份的资料会在
    # 到达本地 cutoff 分类前被搜索服务丢弃。非严格模式继续保留调用方原有 freshness 行为。
    effective_freshness = None if request.strict_cutoff else freshness
    raw_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    client: BochaWebSearchClient | None = None
    if not dry_run:
        try:
            client = BochaWebSearchClient()
        except RuntimeError as error:
            # 缺少 Key 不应让公司研究全链路崩溃；产物会被标记为缺失/低置信。
            errors.append({"query": "__client_init__", "error": str(error)})

    for query_item in query_plan:
        query = str(query_item["query"])
        cache_path = query_cache_path(project_root, request, query, effective_freshness, count_per_query)
        if dry_run:
            raw_results.append(
                {
                    "query": query,
                    "bucket": query_item["bucket"],
                    "results": [],
                    "from_cache": False,
                    "retrieval_mode": "planned",
                    "query_status": "dry_run",
                }
            )
            continue
        if cache_path.exists() and not force_refresh:
            cached = load_json(cache_path)
            if isinstance(cached, dict) and is_cache_compatible(cached, request, effective_freshness, count_per_query):
                raw_results.append(
                    {
                        "query": query,
                        "bucket": query_item["bucket"],
                        "results": cached.get("results", []),
                        "from_cache": True,
                        "retrieval_mode": "cache",
                        "query_status": "success",
                    }
                )
                continue
        if client is None:
            raw_results.append(
                {
                    "query": query,
                    "bucket": query_item["bucket"],
                    "results": [],
                    "from_cache": False,
                    "retrieval_mode": "unavailable",
                    "query_status": "error",
                }
            )
            continue
        try:
            results = client.search(query, count=count_per_query, freshness=effective_freshness)
            raw_results.append(
                {
                    "query": query,
                    "bucket": query_item["bucket"],
                    "results": results,
                    "from_cache": False,
                    "retrieval_mode": "live",
                    "query_status": "success",
                }
            )
            write_json(
                cache_path,
                {
                    "query": query,
                    "freshness": effective_freshness,
                    "count": count_per_query,
                    "cutoff_policy": build_cutoff_policy(request),
                    "results": results,
                },
            )
        except RuntimeError as error:
            errors.append({"query": query, "error": str(error)})
            raw_results.append(
                {
                    "query": query,
                    "bucket": query_item["bucket"],
                    "results": [],
                    "from_cache": False,
                    "retrieval_mode": "live",
                    "query_status": "error",
                }
            )

    cutoff_policy = build_cutoff_policy(request)
    complete_source_table = build_source_table(raw_results, cutoff_policy=cutoff_policy)
    package = build_market_context_package(
        request,
        query_plan,
        raw_results,
        errors,
        dry_run=dry_run,
        effective_freshness=effective_freshness,
        complete_source_table=complete_source_table,
    )
    sources = {"cutoff_audit": package["cutoff_audit"], "sources": package["source_table"]}
    audit = build_collection_audit(
        request,
        query_plan,
        raw_results,
        errors,
        package,
        complete_source_table=complete_source_table,
        dry_run=dry_run,
    )

    write_package_files(target_output_dir, package, sources, audit, raw_results)
    return {
        "status": package["status"],
        "output_dir": str(target_output_dir),
        "generated_artifacts": {
            "market_context_package_json": str(target_output_dir / "market_context_package.json"),
            "market_context_package_md": str(target_output_dir / "market_context_package.md"),
            "market_context_sources_json": str(target_output_dir / "market_context_sources.json"),
            "collection_audit_json": str(target_output_dir / "collection_audit.json"),
            "raw_search_results_json": str(target_output_dir / "raw_search_results.json"),
        },
        "query_count": len(query_plan),
        "source_count": len(package["source_table"]),
        "quality_gate": package["quality_gate"],
    }


def build_market_context_package(
    request: MarketContextRequest,
    query_plan: list[dict[str, Any]],
    raw_results: list[dict[str, Any]],
    errors: list[dict[str, str]],
    *,
    dry_run: bool = False,
    effective_freshness: str | None = "oneMonth",
    complete_source_table: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把搜索结果整理成市场上下文包。

    参数：
        request: 市场上下文采集请求。
        query_plan: 查询计划。
        raw_results: 原始搜索结果，按 query 分组。
        errors: 搜索错误列表。
        dry_run: 是否为仅规划模式。
        effective_freshness: 实际发送给搜索服务的 freshness；严格模式下为 None。
        complete_source_table: 可选的完整去重来源表；传入后可避免重复归一化，同时供 claim 和审计使用。
    返回值：
        `market_context_package.json` 的内容；严格截止模式下 source_table 只包含可进入 claim 的来源。

    为什么这样做：
        严格历史研究需要同时满足两类边界：审计必须知道搜索发现过哪些未来或无日期来源，模型侧却不能
        看到这些来源的标题、摘要或链接，以免后续综合误用。完整来源表因此只参与内部 claim、质量 Gate
        和 cutoff 审计，写入 package 的来源表则执行单独的模型侧过滤。
    """
    cutoff_policy = build_cutoff_policy(request)
    full_source_table = (
        complete_source_table
        if complete_source_table is not None
        else build_source_table(raw_results, cutoff_policy=cutoff_policy)
    )
    claims = build_claims(full_source_table, strict_cutoff=request.strict_cutoff)
    cutoff_audit = build_cutoff_audit(full_source_table, claims, cutoff_policy)
    quality_gate = build_quality_gate(
        full_source_table,
        claims,
        errors,
        cutoff_audit=cutoff_audit,
        dry_run=dry_run,
    )
    source_table = select_model_facing_sources(full_source_table, strict_cutoff=request.strict_cutoff)
    status = decide_package_status(source_table, quality_gate, errors, dry_run=dry_run)

    package = {
        "schema_version": "1.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "target": request.target,
            "stock_code": request.stock_code,
            "company_name": request.company_name,
            "industry": request.industry,
            "as_of_date": request.as_of_date,
            "strict_cutoff": request.strict_cutoff,
        },
        "status": status,
        "cutoff_audit": cutoff_audit,
        "usage_boundary": {
            "data_type": "public_web_search_proxy",
            "can_support": ["Market narrative identification", "Market-attention proxy", "Public contrary signals", "Theme-mapping candidates"],
            "cannot_support_alone": ["Formal consensus expectations", "Precise market prices and returns", "High-confidence target prices", "Complete industry-database conclusions"],
            "why": "v1 uses web-search results only. Treat snippets as public narratives and weak proxies, not database-grade facts.",
        },
        "collection_scope": {
            "query_count": len(query_plan),
            "source_count": len(source_table),
            "total_discovered_source_count": len(full_source_table),
            "eligible_source_count": sum(1 for source in full_source_table if source.get("eligible_for_claim")),
            "excluded_source_count": sum(1 for source in full_source_table if not source.get("eligible_for_claim")),
            "search_engine": "bocha_web_search",
            "depth": request.depth,
            "strict_cutoff": request.strict_cutoff,
            "cutoff_date": cutoff_policy["cutoff_date"],
            "effective_freshness": effective_freshness,
            "freshness_assumption": (
                "strict_cutoff mode omits freshness and applies local cutoff filtering using published_at."
                if request.strict_cutoff
                else "Controlled by the command-line freshness parameter; default: oneMonth."
            ),
        },
        "market_regime": summarize_market_regime(claims),
        "target_market_narrative": summarize_target_narrative(claims),
        "theme_mapping": summarize_theme_mapping(claims),
        "peer_context": summarize_peer_context(claims),
        "global_trends": summarize_global_trends(claims),
        "narrative_to_fundamental_bridge": build_narrative_bridges(claims, request),
        "contradictory_signals": summarize_contradictory_signals(claims),
        "claims": claims,
        "source_table": source_table,
        "quality_gate": quality_gate,
        "open_questions": build_open_questions(quality_gate),
    }
    return package


def build_cutoff_policy(request: MarketContextRequest) -> dict[str, Any]:
    """生成供查询、缓存、来源分类和审计共同使用的截止策略。

    参数：
        request: 市场上下文采集请求。
    返回值：
        包含 strict_cutoff、cutoff_date 和 policy_id 的策略字典。

    为什么这样做：
        严格策略必须在缓存键、来源过滤和三个标准 JSON 产物中保持完全一致；集中构造可以
        避免某一层把同一请求误当成非严格请求，进而复用包含未来信息的缓存。
    """
    cutoff_text = str(request.as_of_date or "").strip()
    if request.strict_cutoff and not cutoff_text:
        raise ValueError("as_of_date is required when strict_cutoff=true.")
    if not cutoff_text:
        cutoff_text = datetime.now(timezone.utc).date().isoformat()
    try:
        cutoff = date.fromisoformat(cutoff_text)
    except ValueError as error:
        raise ValueError("as_of_date must use YYYY-MM-DD format.") from error
    mode = "strict" if request.strict_cutoff else "non_strict"
    return {
        "strict_cutoff": request.strict_cutoff,
        "cutoff_date": cutoff.isoformat(),
        "policy_id": f"{mode}:{cutoff.isoformat()}",
    }


def parse_published_date(value: str) -> date | None:
    """从常见搜索结果日期文本中解析发布日期。

    参数：
        value: published_at 原始文本。
    返回值：
        可确定到自然日时返回 date；缺失或无法可靠解析时返回 None。

    为什么这样做：
        strict-cutoff 宁可把模糊日期降级为 undated，也不能猜测一个日期后生成可能穿越的事实 claim。
    """
    text = str(value or "").strip()
    if not text:
        return None
    iso_candidate = text[:10]
    try:
        return date.fromisoformat(iso_candidate)
    except ValueError:
        pass
    matched = re.search(r"(?P<year>20\d{2})[年/.-](?P<month>\d{1,2})[月/.-](?P<day>\d{1,2})日?", text)
    if not matched:
        return None
    try:
        return date(int(matched.group("year")), int(matched.group("month")), int(matched.group("day")))
    except ValueError:
        return None


def classify_cutoff_status(published_at: str, cutoff_date: str) -> tuple[str, str]:
    """判断来源发布日期相对截止日的状态。

    参数：
        published_at: 来源发布日期原文。
        cutoff_date: YYYY-MM-DD 格式截止日。
    返回值：
        二元组：eligible/future/undated，以及标准化发布日期字符串。
    """
    published = parse_published_date(published_at)
    if published is None:
        return "undated", ""
    cutoff = date.fromisoformat(cutoff_date)
    if published > cutoff:
        return "future", published.isoformat()
    return "eligible", published.isoformat()


def build_source_table(
    raw_results: list[dict[str, Any]], *, cutoff_policy: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """从原始搜索结果生成去重来源表，并标记其与截止日的关系。

    参数：
        raw_results: 按 query 分组的搜索结果。
        cutoff_policy: 统一截止策略；为空时按非严格、当前 UTC 日期处理。
    返回值：
        来源表列表，每条来源包含 eligible、future 或 undated 的 cutoff_status。
    """
    policy = cutoff_policy or build_cutoff_policy(MarketContextRequest())
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for group in raw_results:
        bucket = str(group.get("bucket", ""))
        query = str(group.get("query", ""))
        for item in group.get("results", []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = str(item.get("title", "")).strip()
            snippet = str(item.get("snippet", "")).strip()
            published_at = str(item.get("published_at", "")).strip()
            cutoff_status, normalized_published_date = classify_cutoff_status(published_at, policy["cutoff_date"])
            tier, usage_limit = classify_source(url, title)
            if cutoff_status == "undated":
                # 无日期来源可以帮助发现叙事或候选来源，但无法证明信息在历史观察日已经存在。
                usage_limit = "discovery_only"
            elif cutoff_status == "future" and policy["strict_cutoff"]:
                usage_limit = "future_source_audit_only"
            sources.append(
                {
                    "source_id": f"SRC-{len(sources) + 1:03d}",
                    "bucket": bucket,
                    "query": query,
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "published_at": published_at,
                    "normalized_published_date": normalized_published_date,
                    "cutoff_status": cutoff_status,
                    "eligible_for_claim": cutoff_status == "eligible"
                    or (cutoff_status == "future" and not policy["strict_cutoff"]),
                    "site_name": str(item.get("site_name", "")).strip() or urlparse(url).netloc,
                    "source_tier": tier,
                    "usage_limit": usage_limit,
                    "signal_type": infer_signal_type(bucket, title, snippet),
                }
            )
    return sources


def select_model_facing_sources(
    complete_source_table: list[dict[str, Any]], *, strict_cutoff: bool
) -> list[dict[str, Any]]:
    """生成允许写入模型侧标准产物的来源表。

    参数：
        complete_source_table: 搜索发现并去重后的完整来源表。
        strict_cutoff: 是否启用严格历史截止。
    返回值：
        非严格模式返回完整来源表；严格模式仅返回 eligible_for_claim=true 的来源。

    为什么这样做：
        非严格模式需要保持既有来源发现语义；严格模式则必须阻断未来和无日期来源进入 package、Markdown
        与 sources JSON。过滤仅发生在输出边界，完整列表仍可用于 cutoff 计数和紧凑排除索引。
    """
    if not strict_cutoff:
        return list(complete_source_table)
    return [source for source in complete_source_table if source.get("eligible_for_claim") is True]


def classify_source(url: str, title: str = "") -> tuple[str, str]:
    """按照域名和标题粗分来源等级。

    参数：
        url: 来源 URL。
        title: 搜索结果标题。
    返回值：
        二元组：source_tier、usage_limit。
    """
    host = urlparse(url).netloc.lower()
    title_text = title.lower()
    # 先识别官方/交易所来源，再识别社区和门户弱来源，最后识别财经媒体。
    # 这样做是为了避免 `guba.eastmoney.com` 被宽泛的 `eastmoney.com` 规则误判为 A 级来源。
    if any(rule in host for rule in SOURCE_TIER_RULES["S"]):
        return _tier_usage("S")
    if any(rule in host for rule in SOURCE_TIER_RULES["B"]):
        return _tier_usage("B")
    if any(rule in host for rule in SOURCE_TIER_RULES["A"]):
        return _tier_usage("A")
    if any(keyword in title_text for keyword in ["公告", "投资者关系", "业绩说明会", "年度报告", "交易所"]):
        return _tier_usage("A")
    return _tier_usage("C")


def _tier_usage(tier: str) -> tuple[str, str]:
    """根据来源等级生成使用边界。

    参数：
        tier: S、A、B 或 C。
    返回值：
        二元组：等级、使用边界。
    """
    mapping = {
        "S": "can_support_fact_if_content_matches",
        "A": "can_support_inference",
        "B": "market_narrative_only",
        "C": "sentiment_or_discovery_only",
    }
    return tier, mapping.get(tier, "sentiment_or_discovery_only")


def infer_signal_type(bucket: str, title: str, snippet: str) -> str:
    """推断搜索结果代表的信号类型。

    参数：
        bucket: 查询桶。
        title: 标题。
        snippet: 摘要。
    返回值：
        信号类型字符串。
    """
    text = f"{title} {snippet}"
    if bucket == "negative_signals" or any(word in text for word in ["风险", "利空", "下滑", "不及预期", "产能过剩", "质疑"]):
        return "contradictory_signal"
    if bucket == "theme_mapping" or any(word in text for word in ["AI", "机器人", "算力", "低空", "高股息", "出海"]):
        return "theme_mapping"
    if bucket == "peer_context":
        return "peer_context"
    if bucket == "market_hotspots":
        return "market_regime"
    if bucket == "global_trends":
        return "global_trend"
    if bucket == "sector_context":
        return "sector_context"
    return "target_market_narrative"


def build_claims(source_table: list[dict[str, Any]], *, strict_cutoff: bool = False) -> list[dict[str, Any]]:
    """把允许进入事实层的来源转换成结构化 claim。

    参数：
        source_table: 去重后的来源表。
        strict_cutoff: 是否执行历史严格截断。
    返回值：
        claim 列表；严格模式排除 future，所有模式均排除 undated。
    """
    claims: list[dict[str, Any]] = []
    for source in source_table:
        cutoff_status = source.get("cutoff_status")
        # undated 只能用于发现；strict 模式下 future 也只能留在来源表和审计中。
        if cutoff_status == "undated" or (strict_cutoff and cutoff_status == "future"):
            continue
        snippet = source.get("snippet") or source.get("title") or ""
        claim_text = build_claim_text(source)
        claims.append(
            {
                "claim_id": f"MC-{len(claims) + 1:03d}",
                "claim": claim_text,
                "source_id": source["source_id"],
                "source_url": source["url"],
                "source_title": source["title"],
                "source_tier": source["source_tier"],
                "signal_type": source["signal_type"],
                "confidence": infer_claim_confidence(source),
                "usage_limit": source["usage_limit"],
                "cutoff_status": cutoff_status,
                "evidence_excerpt": snippet[:240],
                "fundamental_bridge": infer_fundamental_bridge(source),
            }
        )
    return claims


def build_claim_text(source: dict[str, Any]) -> str:
    """基于搜索标题和摘要生成保守 claim。

    参数：
        source: 来源对象。
    返回值：
        claim 文本。
    """
    title = str(source.get("title", "")).strip()
    snippet = str(source.get("snippet", "")).strip()
    signal_type = source.get("signal_type", "market_narrative")
    if signal_type == "contradictory_signal":
        prefix = "Public web results indicate a contrary or risk signal"
    elif signal_type == "theme_mapping":
        prefix = "Public web results indicate a theme-mapping signal"
    elif signal_type == "market_regime":
        prefix = "Public web results indicate a market-style or hotspot signal"
    elif signal_type == "peer_context":
        prefix = "Public web results indicate a peer-comparison signal"
    else:
        prefix = "Public web results indicate a market-narrative signal"
    detail = snippet or title
    return f"{prefix}：{detail[:160]}"


def infer_claim_confidence(source: dict[str, Any]) -> str:
    """根据来源等级给 claim 置信度上限。

    参数：
        source: 来源对象。
    返回值：
        high、medium、low 或 very_low。
    """
    tier = source.get("source_tier")
    if tier == "S":
        return "medium"
    if tier == "A":
        return "medium_low"
    if tier == "B":
        return "low"
    return "very_low"


def infer_fundamental_bridge(source: dict[str, Any]) -> dict[str, str]:
    """把市场叙事映射到需要验证的基本面变量。

    参数：
        source: 来源对象。
    返回值：
        基本面桥接字段。
    """
    signal_type = source.get("signal_type", "")
    if signal_type == "theme_mapping":
        return {
            "variable": "Revenue mix, orders, customer certification, gross margin, and capacity for the relevant business",
            "status": "needs_company_validation",
            "why": "Theme momentum must map to business exposure and earnings sensitivity; otherwise it remains theme-only mapping.",
        }
    if signal_type == "market_regime":
        return {
            "variable": "Valuation multiples, dividend yield, risk appetite, and capital style",
            "status": "needs_price_and_valuation_validation",
            "why": "Market style can affect valuation premiums or discounts but cannot independently prove fundamental improvement.",
        }
    if signal_type == "contradictory_signal":
        return {
            "variable": "Revenue growth, margins, cash flow, asset quality, or industry pricing",
            "status": "use_as_falsifier_candidate",
            "why": "Contrary signals should enter the falsification checklist for later validation with financial, valuation, or industry evidence.",
        }
    if signal_type == "peer_context":
        return {
            "variable": "Peer valuation, ROE, growth, business purity, and risk differences",
            "status": "needs_peer_validation",
            "why": "Web peer signals can help identify samples but cannot replace formal cross-sectional comparison.",
        }
    return {
        "variable": "Differences in earnings, growth, risk discount, or dividends",
        "status": "needs_financial_validation",
        "why": "Market narratives must map back to modelable financial variables before entering the investment thesis.",
    }


def build_cutoff_audit(
    source_table: list[dict[str, Any]], claims: list[dict[str, Any]], cutoff_policy: dict[str, Any]
) -> dict[str, Any]:
    """汇总 strict-cutoff 过滤结果，供三个标准产物统一复用。

    参数：
        source_table: 包含全部 eligible、future 和 undated 来源的来源表。
        claims: 实际生成的 claim。
        cutoff_policy: 当前请求的统一截止策略。
    返回值：
        截止审计字典。
    """
    source_status_by_id = {source.get("source_id"): source.get("cutoff_status") for source in source_table}
    future_claim_count = sum(
        1 for claim in claims if source_status_by_id.get(claim.get("source_id")) == "future"
    )
    undated_fact_claim_count = sum(
        1 for claim in claims if source_status_by_id.get(claim.get("source_id")) == "undated"
    )
    accepted_source_count = sum(1 for source in source_table if source.get("cutoff_status") == "eligible")
    future_source_count = sum(1 for source in source_table if source.get("cutoff_status") == "future")
    undated_discovery_count = sum(1 for source in source_table if source.get("cutoff_status") == "undated")
    strict_cutoff = bool(cutoff_policy.get("strict_cutoff"))
    return {
        **cutoff_policy,
        "total_source_count": len(source_table),
        "accepted_source_count": accepted_source_count,
        "future_source_count": future_source_count,
        "future_excluded_count": future_source_count if strict_cutoff else 0,
        "undated_discovery_count": undated_discovery_count,
        "future_fact_claim_count": future_claim_count,
        "undated_fact_claim_count": undated_fact_claim_count,
        # 非严格模式允许 future 延续原行为；严格模式则要求所有事实 claim 都来自截止日前有日期来源。
        "cutoff_compliant": (not strict_cutoff) or (future_claim_count == 0 and undated_fact_claim_count == 0),
    }


def build_quality_gate(
    source_table: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    errors: list[dict[str, str]],
    *,
    cutoff_audit: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """生成市场上下文质量 Gate。

    参数：
        source_table: 来源表。
        claims: claim 列表。
        errors: 搜索错误列表。
        cutoff_audit: 截止过滤审计；Gate 的来源和信号统计只使用 eligible 来源。
        dry_run: 是否为仅规划模式。
    返回值：
        质量 Gate 字典。
    """
    eligible_sources = [source for source in source_table if source.get("cutoff_status") == "eligible"]
    eligible_source_ids = {source.get("source_id") for source in eligible_sources}
    eligible_claims = [claim for claim in claims if claim.get("source_id") in eligible_source_ids]
    tiers = {
        tier: sum(1 for source in eligible_sources if source.get("source_tier") == tier)
        for tier in ["S", "A", "B", "C"]
    }
    signal_types = {claim.get("signal_type") for claim in eligible_claims}
    has_negative = "contradictory_signal" in signal_types
    has_target = any(claim.get("signal_type") == "target_market_narrative" for claim in eligible_claims)
    has_market = "market_regime" in signal_types
    can_support_proxy = bool(
        eligible_sources and has_negative and has_target and has_market and (tiers["S"] + tiers["A"] >= 3)
    )
    # v1 只有网页搜索，没有正式行情、一致预期和数据库证据。
    # 因此即便来源覆盖较完整，也只能支持市场预期代理，不能单独支撑 actionable thesis。
    can_support_actionable = False
    return {
        "market_expectation_status": "proxy_only" if eligible_sources else "missing",
        "source_tier_counts": tiers,
        "accepted_source_count": cutoff_audit["accepted_source_count"],
        "future_excluded_count": cutoff_audit["future_excluded_count"],
        "undated_discovery_count": cutoff_audit["undated_discovery_count"],
        "undated_fact_claim_count": cutoff_audit["undated_fact_claim_count"],
        "cutoff_compliant": cutoff_audit["cutoff_compliant"],
        "has_market_regime_signal": has_market,
        "has_target_narrative_signal": has_target,
        "has_contradictory_search": has_negative,
        "search_error_count": len(errors),
        "dry_run": dry_run,
        "can_support_market_expectation_proxy": can_support_proxy,
        "can_support_actionable_thesis": can_support_actionable,
        "max_confidence": "medium_low" if can_support_proxy else "low",
        "required_downgrade": "public_web_proxy_only",
    }


def decide_package_status(
    source_table: list[dict[str, Any]], quality_gate: dict[str, Any], errors: list[dict[str, str]], *, dry_run: bool
) -> str:
    """根据来源数量和质量 Gate 决定包状态。

    参数：
        source_table: 来源表。
        quality_gate: 质量 Gate。
        errors: 搜索错误列表。
        dry_run: 是否为仅规划模式。
    返回值：
        状态字符串。
    """
    if dry_run:
        return "query_plan_only"
    if not source_table and errors:
        return "missing_due_to_search_error"
    if not source_table:
        return "missing"
    if quality_gate.get("can_support_market_expectation_proxy"):
        return "ready_public_proxy"
    return "partial_with_public_sources"


def summarize_market_regime(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总市场风格线索。

    参数：
        claims: claim 列表。
    返回值：
        市场风格摘要。
    """
    items = [claim for claim in claims if claim.get("signal_type") == "market_regime"][:5]
    return {
        "status": "proxy_only" if items else "missing",
        "dominant_style": "unknown_from_web_search_v1",
        "hotspot_signals": [claim["claim"] for claim in items],
        "confidence": "medium_low" if items else "low",
    }


def summarize_target_narrative(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总目标公司市场叙事。

    参数：
        claims: claim 列表。
    返回值：
        目标公司叙事摘要。
    """
    positives = [claim for claim in claims if claim.get("signal_type") == "target_market_narrative"][:5]
    negatives = [claim for claim in claims if claim.get("signal_type") == "contradictory_signal"][:5]
    return {
        "status": "proxy_only" if positives or negatives else "missing",
        "bull_case_proxy": [claim["claim"] for claim in positives],
        "bear_case_proxy": [claim["claim"] for claim in negatives],
        "market_concerns": [claim["evidence_excerpt"] for claim in negatives[:3]],
    }


def summarize_theme_mapping(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总主题映射线索。

    参数：
        claims: claim 列表。
    返回值：
        主题映射列表。
    """
    items = [claim for claim in claims if claim.get("signal_type") == "theme_mapping"][:8]
    return [
        {
            "theme_proxy": claim["evidence_excerpt"],
            "exposure_status": "unverified_theme_mapping",
            "fundamental_bridge": claim["fundamental_bridge"],
            "usage_limit": claim["usage_limit"],
            "source_id": claim["source_id"],
        }
        for claim in items
    ]


def summarize_peer_context(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总同行比较线索。

    参数：
        claims: claim 列表。
    返回值：
        同行线索摘要。
    """
    items = [claim for claim in claims if claim.get("signal_type") == "peer_context"][:6]
    return {
        "status": "candidate_only" if items else "missing",
        "comparison_status": "needs_formal_peer_validation" if items else "missing",
        "peer_candidate_signals": [claim["claim"] for claim in items],
    }


def summarize_global_trends(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总全球趋势线索。

    参数：
        claims: claim 列表。
    返回值：
        全球趋势列表。
    """
    items = [claim for claim in claims if claim.get("signal_type") == "global_trend"][:6]
    return [
        {
            "trend_proxy": claim["claim"],
            "mapping_status": "needs_industry_or_company_validation",
            "source_id": claim["source_id"],
        }
        for claim in items
    ]


def build_narrative_bridges(claims: list[dict[str, Any]], request: MarketContextRequest) -> list[dict[str, Any]]:
    """生成“叙事到基本面”的桥接表。

    参数：
        claims: claim 列表。
        request: 市场上下文采集请求。
    返回值：
        桥接表。
    """
    bridges: list[dict[str, Any]] = []
    for claim in claims[:12]:
        bridge = claim["fundamental_bridge"]
        bridges.append(
            {
                "narrative_claim_id": claim["claim_id"],
                "narrative": claim["claim"],
                "target_mapping": request.company_name or request.stock_code or request.target,
                "company_variable": bridge["variable"],
                "evidence_status": bridge["status"],
                "why_it_matters": bridge["why"],
            }
        )
    return bridges


def summarize_contradictory_signals(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总反方和证伪信号。

    参数：
        claims: claim 列表。
    返回值：
        反方信号列表。
    """
    items = [claim for claim in claims if claim.get("signal_type") == "contradictory_signal"][:8]
    return [
        {
            "signal": claim["claim"],
            "usage": "valuation_or_thesis_falsifier_candidate",
            "source_id": claim["source_id"],
            "confidence": claim["confidence"],
        }
        for claim in items
    ]


def build_open_questions(quality_gate: dict[str, Any]) -> list[str]:
    """根据质量 Gate 生成缺口清单。

    参数：
        quality_gate: 质量 Gate。
    返回值：
        缺口文本列表。
    """
    questions: list[str] = []
    if quality_gate.get("market_expectation_status") == "missing":
        questions.append("No public-web market-expectations proxy was obtained; the investment thesis must be downgraded to fundamental_only.")
    if not quality_gate.get("has_contradictory_search"):
        questions.append("Valid contrary search results are missing, so coverage of major risks cannot be confirmed.")
    if quality_gate.get("source_tier_counts", {}).get("S", 0) == 0:
        questions.append("Official or exchange-level sources are missing; web results cannot independently support factual conclusions.")
    if not quality_gate.get("can_support_actionable_thesis"):
        questions.append("Market context is only a public-narrative proxy and must be used with financial, valuation, and peer evidence at reduced confidence.")
    return questions


def build_query_telemetry(
    query_plan: list[dict[str, Any]], raw_results: list[dict[str, Any]]
) -> dict[str, int]:
    """汇总缓存、实时请求和返回状态遥测。

    参数：
        query_plan: 计划执行的查询列表。
        raw_results: 每条查询的完整原始结果分组。
    返回值：
        查询总数、缓存命中数、实时请求数、成功数、空结果数、失败数和 dry-run 数。

    为什么这样做：
        成功查询允许返回零条结果，因此 successful_query_count 与 empty_query_count 可以重叠；前者衡量
        查询调用是否正常完成，后者衡量完成后是否拿到候选来源。把两者分开可区分服务故障与正常空结果。
    """
    successful_groups = [group for group in raw_results if group.get("query_status") == "success"]
    return {
        "total_query_count": len(query_plan),
        "cache_query_count": sum(1 for group in raw_results if group.get("retrieval_mode") == "cache"),
        "live_query_count": sum(1 for group in raw_results if group.get("retrieval_mode") == "live"),
        "successful_query_count": len(successful_groups),
        "empty_query_count": sum(1 for group in successful_groups if not group.get("results")),
        "failed_query_count": sum(1 for group in raw_results if group.get("query_status") == "error"),
        "dry_run_query_count": sum(1 for group in raw_results if group.get("query_status") == "dry_run"),
    }


def build_excluded_source_index(complete_source_table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成不含摘要正文的紧凑排除来源索引。

    参数：
        complete_source_table: 完整来源表。
    返回值：
        eligible_for_claim=false 来源的最小审计字段列表，不包含 snippet 或 query。

    为什么这样做：
        审计需要证明哪些未来或无日期来源被排除，但复制搜索摘要会扩大产物并增加误用风险。索引只保留
        定位、日期、截止分类和排除原因，足以复核过滤结果，同时不会把被排除内容重新暴露给模型侧包。
    """
    excluded: list[dict[str, Any]] = []
    for source in complete_source_table:
        if source.get("eligible_for_claim") is True:
            continue
        cutoff_status = str(source.get("cutoff_status", ""))
        exclusion_reason = {
            "future": "published_after_cutoff",
            "undated": "published_date_unverified",
        }.get(cutoff_status, "not_eligible_for_claim")
        excluded.append(
            {
                "source_id": source.get("source_id"),
                "bucket": source.get("bucket"),
                "url": source.get("url"),
                "published_at": source.get("published_at"),
                "normalized_published_date": source.get("normalized_published_date"),
                "cutoff_status": cutoff_status,
                "source_tier": source.get("source_tier"),
                "usage_limit": source.get("usage_limit"),
                "exclusion_reason": exclusion_reason,
            }
        )
    return excluded


def build_collection_audit(
    request: MarketContextRequest,
    query_plan: list[dict[str, Any]],
    raw_results: list[dict[str, Any]],
    errors: list[dict[str, str]],
    package: dict[str, Any],
    *,
    complete_source_table: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    """生成采集审计文件。

    参数：
        request: 市场上下文采集请求。
        query_plan: 查询计划。
        raw_results: 原始搜索结果。
        errors: 搜索错误列表。
        package: 市场上下文包。
        complete_source_table: 过滤模型侧来源之前的完整来源表。
        dry_run: 是否为仅规划模式。
    返回值：
        审计字典，包含完整来源计数、紧凑排除索引和查询执行遥测。
    """
    eligible_source_count = sum(1 for source in complete_source_table if source.get("eligible_for_claim"))
    excluded_source_index = build_excluded_source_index(complete_source_table)
    return {
        "schema_version": "1.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "request": asdict(request),
        "search_engine": "bocha_web_search",
        "credential_policy": "The API key is read only from the BOCHA_WEB_SEARCH_API_KEY environment variable and is not written to artifacts.",
        "dry_run": dry_run,
        "query_plan": query_plan,
        "query_count": len(query_plan),
        "raw_result_groups": len(raw_results),
        "query_telemetry": build_query_telemetry(query_plan, raw_results),
        "source_count": len(package.get("source_table", [])),
        "total_discovered_source_count": len(complete_source_table),
        "eligible_source_count": eligible_source_count,
        "excluded_source_count": len(complete_source_table) - eligible_source_count,
        "excluded_source_index": excluded_source_index,
        "errors": errors,
        "status": package.get("status"),
        "cutoff_audit": package.get("cutoff_audit", {}),
        "quality_gate": package.get("quality_gate", {}),
    }


def write_package_files(
    output_dir: Path,
    package: dict[str, Any],
    sources: dict[str, Any],
    audit: dict[str, Any],
    raw_results: list[dict[str, Any]],
) -> None:
    """写入市场上下文标准产物。

    参数：
        output_dir: 输出目录。
        package: 市场上下文包。
        sources: 来源表。
        audit: 采集审计。
        raw_results: 原始搜索结果。
    返回值：
        无。
    """
    write_json(output_dir / "market_context_package.json", package)
    write_text(output_dir / "market_context_package.md", render_market_context_markdown(package))
    write_json(output_dir / "market_context_sources.json", sources)
    write_json(output_dir / "collection_audit.json", audit)
    write_json(output_dir / "raw_search_results.json", {"raw_results": raw_results})


def render_market_context_markdown(package: dict[str, Any]) -> str:
    """把市场上下文包渲染为 Markdown。

    参数：
        package: 市场上下文包。
    返回值：
        Markdown 文本。
    """
    target = package.get("target", {})
    lines = [
        f"# {target.get('company_name') or target.get('stock_code') or target.get('target') or 'Unknown Target'} Market Context Package",
        "",
        f"- Status: {package.get('status')}",
        f"- As-of date: {target.get('as_of_date') or 'Not specified'}",
        f"- Data boundary: {package.get('usage_boundary', {}).get('data_type')}",
        f"- Source count: {package.get('collection_scope', {}).get('source_count', 0)}",
        f"- Strict cutoff: {package.get('cutoff_audit', {}).get('strict_cutoff', False)}",
        f"- Cutoff date: {package.get('cutoff_audit', {}).get('cutoff_date', 'Not specified')}",
        f"- Accepted pre-cutoff sources: {package.get('cutoff_audit', {}).get('accepted_source_count', 0)}",
        f"- Maximum confidence: {package.get('quality_gate', {}).get('max_confidence')}",
        "",
        "## Market Style Signals",
    ]
    for item in package.get("market_regime", {}).get("hotspot_signals", []):
        lines.append(f"- {item}")
    lines.extend(["", "## Target Company Market-Narrative Proxy"])
    narrative = package.get("target_market_narrative", {})
    for item in narrative.get("bull_case_proxy", []):
        lines.append(f"- Bullish / attention signal: {item}")
    for item in narrative.get("bear_case_proxy", []):
        lines.append(f"- Contrary / risk signal: {item}")
    lines.extend(["", "## Theme Mapping"])
    for item in package.get("theme_mapping", []):
        lines.append(f"- {item.get('theme_proxy')}; usage boundary: {item.get('usage_limit')}")
    lines.extend(["", "## Contrary and Falsification Signals"])
    for item in package.get("contradictory_signals", []):
        lines.append(f"- {item.get('signal')}（{item.get('usage')}）")
    lines.extend(["", "## Quality Gate", ""])
    gate = package.get("quality_gate", {})
    for key, value in gate.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Gaps", ""])
    for item in package.get("open_questions", []):
        lines.append(f"- {item}")
    lines.extend(["", "## Source Table", ""])
    for source in package.get("source_table", [])[:30]:
        lines.append(
            f"- [{source.get('source_id')}] {source.get('title')} — {source.get('url')}"
            f"（{source.get('source_tier')}；cutoff={source.get('cutoff_status')}）"
        )
    lines.append("")
    return "\n".join(lines)


def default_package_dir(project_root: Path, request: MarketContextRequest) -> Path:
    """生成默认市场上下文包目录。

    参数：
        project_root: Project root.
        request: 市场上下文采集请求。
    返回值：
        默认输出目录。
    """
    stock_code = request.stock_code or sanitize_path_part(request.target) or "unknown_target"
    as_of_date = request.as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return project_root / "market_context_collector_scripts" / "collector_workspace" / "packages" / stock_code / as_of_date


def query_cache_path(
    project_root: Path, request: MarketContextRequest, query: str, freshness: str | None, count: int
) -> Path:
    """生成 query 级缓存路径。

    参数：
        project_root: Project root.
        request: 市场上下文采集请求。
        query: 搜索关键词。
        freshness: 实际搜索时效范围；None 表示请求中省略 freshness。
        count: 搜索结果条数。
    返回值：
        缓存文件路径。
    """
    cutoff_policy = build_cutoff_policy(request)
    as_of_date = cutoff_policy["cutoff_date"]
    policy_dir = "strict_cutoff" if request.strict_cutoff else "non_strict"
    # policy_id 同时进入目录和摘要，双重隔离可以阻止 strict 请求命中旧版非严格缓存。
    cache_identity = f"{query}|{freshness}|{count}|{cutoff_policy['policy_id']}"
    digest = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()[:24]
    return (
        project_root
        / "market_context_collector_scripts"
        / "collector_workspace"
        / "cache"
        / "queries"
        / policy_dir
        / as_of_date
        / f"{digest}.json"
    )


def is_cache_compatible(
    cached: dict[str, Any], request: MarketContextRequest, freshness: str | None, count: int
) -> bool:
    """确认 query 缓存与当前截止策略及搜索参数完全兼容。

    参数：
        cached: 已读取的缓存对象。
        request: 当前采集请求。
        freshness: 当前实际 freshness。
        count: 当前单查询结果数量。
    返回值：
        参数和 cutoff policy 全部一致时返回 True。
    """
    return (
        cached.get("cutoff_policy") == build_cutoff_policy(request)
        and cached.get("freshness") == freshness
        and cached.get("count") == count
        and isinstance(cached.get("results"), list)
    )


def sanitize_path_part(value: str) -> str:
    """清理路径片段，避免公司名中的特殊字符破坏目录结构。

    参数：
        value: 原始路径片段。
    返回值：
        安全路径片段。
    """
    text = str(value or "").strip()
    return "".join(ch for ch in text if ch.isalnum() or ch in {"-", "_"})[:80]


def load_json(path: Path) -> Any:
    """安全读取 JSON 文件。

    参数：
        path: JSON 路径。
    返回值：
        JSON 对象；不存在或解析失败时返回空字典。
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_json(path: Path, payload: Any) -> None:
    """写入 JSON 文件，并自动创建父目录。

    参数：
        path: 输出路径。
        payload: JSON 可序列化对象。
    返回值：
        无。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    """写入文本文件，并自动创建父目录。

    参数：
        path: 输出路径。
        content: 文本内容。
    返回值：
        无。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    参数：
        无。
    返回值：
        ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="Collect company market context with Bocha Web Search and generate market_context_package.")
    parser.add_argument("--target", default="", help="Company name or stock code.")
    parser.add_argument("--stock-code", default="", help="Stock code.")
    parser.add_argument("--company-name", default="", help="Company name.")
    parser.add_argument("--industry", default="", help="Industry or sector.")
    parser.add_argument("--as-of-date", default="", help="As-of date, e.g. 2026-07-08.")
    parser.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard", help="Collection depth.")
    parser.add_argument("--focus", default="", help="User focus; separate multiple topics with commas.")
    parser.add_argument(
        "--strict-cutoff",
        action="store_true",
        help="Enable strict historical as-of-date cutoff; requires --as-of-date and automatically omits Bocha freshness.",
    )
    parser.add_argument(
        "--freshness",
        default="oneMonth",
        help="Bocha freshness parameter; default: oneMonth. Pass none to omit it; strict-cutoff always omits it.",
    )
    parser.add_argument("--count-per-query", type=int, default=8, help="Number of results per query.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root.")
    parser.add_argument("--output-dir", default="", help="Explicit output directory.")
    parser.add_argument("--dry-run", action="store_true", help="Generate only the query plan and empty package without calling the external search API.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore query cache and call the search API again.")
    return parser


def normalize_freshness_argument(value: str | None) -> str | None:
    """把命令行 freshness 文本转换为客户端参数。

    参数：
        value: 命令行原始值。
    返回值：
        none/null/空字符串返回 None，其余返回去空格后的原值。
    """
    text = str(value or "").strip()
    return None if text.lower() in {"", "none", "null"} else text


def main() -> None:
    """命令行主入口。

    参数：
        无。
    返回值：
        无。
    """
    configure_stdout_encoding()
    args = build_parser().parse_args()
    request = MarketContextRequest(
        target=args.target,
        stock_code=args.stock_code,
        company_name=args.company_name,
        industry=args.industry,
        as_of_date=args.as_of_date,
        depth=args.depth,
        focus=args.focus,
        strict_cutoff=args.strict_cutoff,
    )
    result = collect_market_context(
        request,
        project_root=Path(args.project_root).resolve(),
        output_dir=Path(args.output_dir).resolve() if args.output_dir else None,
        count_per_query=args.count_per_query,
        freshness=normalize_freshness_argument(args.freshness),
        dry_run=args.dry_run,
        force_refresh=args.force_refresh,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def configure_stdout_encoding() -> None:
    """配置 Windows 终端 UTF-8 输出，避免中文乱码。

    参数：
        无。
    返回值：
        无。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    main()
