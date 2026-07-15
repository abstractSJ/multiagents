"""市场上下文搜索 Query Planner。

该模块把公司研究请求拆成可执行的网页搜索矩阵。v1 只依赖 Web Search，
因此查询规划必须覆盖“市场正在交易什么、公司被如何叙事、行业处于什么状态、
有没有反方证据”四类最小问题，而不是只围绕公司名做单点搜索。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any


DEPTH_QUERY_LIMITS = {"quick": 12, "standard": 32, "deep": 72}


@dataclass(frozen=True)
class MarketContextRequest:
    """市场上下文采集请求。

    参数：
        target: 用户原始目标，可以是公司名或股票代码。
        stock_code: 股票代码。
        company_name: 公司名称。
        industry: 行业或板块；为空时使用公司相关查询兜底。
        as_of_date: 观察日。
        depth: quick、standard 或 deep。
        focus: 用户关注点，多个重点用逗号分隔。
        strict_cutoff: 是否启用历史时点严格截断；启用后查询只使用观察日及其年份作时间锚点。
    返回值：
        dataclass 实例，无额外返回值。
    """

    target: str = ""
    stock_code: str = ""
    company_name: str = ""
    industry: str = ""
    as_of_date: str = ""
    depth: str = "standard"
    focus: str = ""
    strict_cutoff: bool = False


def build_query_plan(request: MarketContextRequest) -> list[dict[str, Any]]:
    """生成网页搜索计划。

    参数：
        request: 市场上下文采集请求。
    返回值：
        查询计划列表；每项包含 bucket、query、priority、reason。
    """
    normalized_depth = normalize_depth(request.depth)
    identity = build_target_identity(request)
    industry = (request.industry or "").strip()
    focus_terms = parse_focus_terms(request.focus)

    queries: list[dict[str, Any]] = []
    queries.extend(_market_hotspot_queries())
    queries.extend(_target_narrative_queries(identity, focus_terms))
    queries.extend(_sector_context_queries(industry, identity))
    queries.extend(_theme_mapping_queries(identity, focus_terms))
    queries.extend(_peer_context_queries(industry, identity))
    queries.extend(_negative_signal_queries(identity, industry, focus_terms))
    if normalized_depth == "deep":
        queries.extend(_global_trend_queries(industry, focus_terms))

    if request.strict_cutoff:
        # 严格历史模式必须在去重前统一改写时间表达，避免“今日/近期/最近”或代码中的当前年份
        # 把搜索结果重新拉回到今天，同时也让同义查询在最终文本上正确去重。
        queries = apply_strict_cutoff_anchors(queries, request.as_of_date)

    deduped = dedupe_query_plan(queries)
    return limit_query_plan_by_depth(deduped, normalized_depth)


def apply_strict_cutoff_anchors(queries: list[dict[str, Any]], as_of_date: str) -> list[dict[str, Any]]:
    """把查询计划改写为仅面向历史观察日的严格时间表达。

    参数：
        queries: 尚未去重的查询计划。
        as_of_date: 严格截止日，格式必须为 YYYY-MM-DD。
    返回值：
        每条 query 均带截止日和年份锚点、且不含相对今天表达的查询计划。

    为什么这样做：
        搜索引擎会把“今日、近期、最近”和硬编码的当前年份解释为调用时点，而不是研究观察日。
        统一后处理可以覆盖所有信息桶，也能防止未来新增查询遗漏严格截止规则。
    """
    cutoff = parse_strict_cutoff_date(as_of_date)
    cutoff_year = str(cutoff.year)
    anchored: list[dict[str, Any]] = []
    for item in queries:
        query = str(item.get("query", ""))
        query = re.sub(r"(?:今日|近期|最近)", "", query)
        # 查询模板中可能含有开发时的当前年份；严格模式下全部替换为观察日年份，
        # 避免历史研究意外检索未来年度材料。
        query = re.sub(r"\b20\d{2}\b", cutoff_year, query)
        query = " ".join(query.split())
        anchor = f"截至 {cutoff.isoformat()} {cutoff_year}年及以前"
        anchored_item = dict(item)
        anchored_item["query"] = f"{query} {anchor}".strip()
        anchored_item["cutoff_anchor"] = cutoff.isoformat()
        anchored.append(anchored_item)
    return anchored


def parse_strict_cutoff_date(as_of_date: str) -> date:
    """解析并校验严格截止日。

    参数：
        as_of_date: YYYY-MM-DD 格式的观察日。
    返回值：
        解析后的日期对象。
    异常：
        ValueError: 严格模式未提供观察日或日期格式无效。
    """
    text = str(as_of_date or "").strip()
    if not text:
        raise ValueError("strict_cutoff=true 时必须提供 as_of_date。")
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError("strict_cutoff=true 时 as_of_date 必须使用 YYYY-MM-DD 格式。") from error


def limit_query_plan_by_depth(queries: list[dict[str, Any]], depth: str) -> list[dict[str, Any]]:
    """按深度截取查询计划，并保证 quick 模式覆盖核心信息桶。

    参数：
        queries: 去重后的查询计划。
        depth: quick、standard 或 deep。
    返回值：
        截取后的查询计划。
    """
    limit = DEPTH_QUERY_LIMITS[depth]
    if depth != "quick":
        return queries[:limit]

    # quick 模式最容易因为查询数量受限而只剩市场热点和公司叙事。
    # 因此这里用最小配额保证反方、行业、主题和同行至少被触达一次，避免输出看似完整但缺少证伪搜索。
    quotas = {
        "market_hotspots": 3,
        "target_narrative": 3,
        "sector_context": 2,
        "theme_mapping": 1,
        "peer_context": 1,
        "negative_signals": 2,
    }
    selected: list[dict[str, Any]] = []
    selected_queries: set[str] = set()
    for bucket, quota in quotas.items():
        bucket_items = [item for item in queries if item.get("bucket") == bucket]
        for item in bucket_items[:quota]:
            query = str(item.get("query", ""))
            if query not in selected_queries:
                selected.append(item)
                selected_queries.add(query)
    for item in queries:
        if len(selected) >= limit:
            break
        query = str(item.get("query", ""))
        if query not in selected_queries:
            selected.append(item)
            selected_queries.add(query)
    return selected[:limit]


def normalize_depth(value: str) -> str:
    """标准化深度参数。

    参数：
        value: 原始深度字符串。
    返回值：
        quick、standard 或 deep。
    """
    text = str(value or "standard").strip().lower()
    return text if text in DEPTH_QUERY_LIMITS else "standard"


def build_target_identity(request: MarketContextRequest) -> str:
    """生成适合搜索的公司身份字符串。

    参数：
        request: 市场上下文采集请求。
    返回值：
        同时包含公司名和股票代码的搜索字符串；缺一项时自动降级。
    """
    parts = [part for part in [request.company_name.strip(), request.stock_code.strip()] if part]
    if parts:
        return " ".join(parts)
    return request.target.strip()


def parse_focus_terms(value: str) -> list[str]:
    """解析用户关注点。

    参数：
        value: 逗号、中文逗号或分号分隔的 focus 字符串。
    返回值：
        去重后的关注点列表。
    """
    raw = str(value or "").replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    terms: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = item.strip()
        if text and text not in seen:
            terms.append(text)
            seen.add(text)
    return terms


def dedupe_query_plan(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 query 文本去重，同时保留先出现的高优先级查询。

    参数：
        queries: 原始查询列表。
    返回值：
        去重后的查询列表。
    """
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in queries:
        query = str(item.get("query", "")).strip()
        if not query or query in seen:
            continue
        deduped.append(item)
        seen.add(query)
    return deduped


def _query(bucket: str, query: str, priority: str, reason: str) -> dict[str, Any]:
    """创建标准查询对象。

    参数：
        bucket: 查询所属信息桶。
        query: 搜索关键词。
        priority: high、medium 或 low。
        reason: 为什么需要该查询。
    返回值：
        查询对象。
    """
    return {"bucket": bucket, "query": query, "priority": priority, "reason": reason}


def _market_hotspot_queries() -> list[dict[str, Any]]:
    """生成全市场热点查询。

    参数：
        无。
    返回值：
        查询对象列表。
    """
    return [
        _query("market_hotspots", "A股 今日 市场主线 热点 板块 资金 风格", "high", "识别当前 A 股正在交易的市场主线。"),
        _query("market_hotspots", "A股 近期 热点 行业 高股息 AI 机器人 低空经济", "medium", "捕捉跨行业主题和风格偏好。"),
        _query("market_hotspots", "A股 市场风格 成长 价值 高股息 小盘 大盘", "medium", "判断风险偏好和风格环境。"),
        _query("market_hotspots", "沪深两市 今日 涨幅 居前 板块 原因", "medium", "用当日板块表现验证热点是否仍在交易。"),
    ]


def _target_narrative_queries(identity: str, focus_terms: list[str]) -> list[dict[str, Any]]:
    """生成目标公司市场叙事查询。

    参数：
        identity: 公司搜索身份字符串。
        focus_terms: 用户关注点。
    返回值：
        查询对象列表。
    """
    if not identity:
        return []
    queries = [
        _query("target_narrative", f"{identity} 最近 为什么 涨 跌 股价 异动 原因", "high", "识别公司近期被市场交易的直接原因。"),
        _query("target_narrative", f"{identity} 投资逻辑 市场预期 分歧", "high", "寻找市场正反两方的核心分歧。"),
        _query("target_narrative", f"{identity} 研报 评级 目标价 预期", "medium", "用公开研报摘要作为一致预期弱代理。"),
        _query("target_narrative", f"{identity} 投资者关系 业绩说明会 互动易", "medium", "从投资者提问中提取市场关注点。"),
    ]
    for term in focus_terms[:4]:
        queries.append(
            _query("target_narrative", f"{identity} {term} 市场预期 投资者 关注", "high", "验证用户 focus 是否也是市场关注点。")
        )
    return queries


def _sector_context_queries(industry: str, identity: str) -> list[dict[str, Any]]:
    """生成行业上下文查询。

    参数：
        industry: 行业或板块。
        identity: 公司搜索身份字符串。
    返回值：
        查询对象列表。
    """
    if industry:
        subject = industry
        return [
            _query("sector_context", f"{subject} 景气度 供需 价格 库存 开工率", "high", "识别行业景气和核心量化变量。"),
            _query("sector_context", f"{subject} 政策 监管 产业趋势 2026", "medium", "识别政策和监管变化。"),
            _query("sector_context", f"{subject} 竞争格局 龙头 公司 对比", "medium", "为后续横向比较准备候选样本。"),
            _query("sector_context", f"{subject} 最近 热点 事件 催化", "medium", "捕捉行业事件驱动和主题催化。"),
        ]
    if identity:
        return [
            _query("sector_context", f"{identity} 所属行业 景气度 竞争格局", "medium", "行业未知时用公司反查所属行业和景气。"),
            _query("sector_context", f"{identity} 主营业务 行业 产业链 上下游", "medium", "行业未知时从公司业务定位推断产业链。"),
        ]
    return []


def _theme_mapping_queries(identity: str, focus_terms: list[str]) -> list[dict[str, Any]]:
    """生成主题映射查询。

    参数：
        identity: 公司搜索身份字符串。
        focus_terms: 用户关注点。
    返回值：
        查询对象列表。
    """
    if not identity:
        return []
    default_themes = ["AI", "机器人", "算力", "低空经济", "出海", "高股息"]
    theme_terms = focus_terms or default_themes
    return [
        _query("theme_mapping", f"{identity} {theme} 业务 收入 占比 订单 客户", "medium", "判断公司是否只是题材映射，还是有真实业务敞口。")
        for theme in theme_terms[:6]
    ]


def _peer_context_queries(industry: str, identity: str) -> list[dict[str, Any]]:
    """生成同行比较查询。

    参数：
        industry: 行业或板块。
        identity: 公司搜索身份字符串。
    返回值：
        查询对象列表。
    """
    queries: list[dict[str, Any]] = []
    if identity:
        queries.append(_query("peer_context", f"{identity} 同行业 公司 对比 估值 ROE 增速", "medium", "寻找可比公司和相对优劣。"))
    if industry:
        queries.extend(
            [
                _query("peer_context", f"{industry} A股 龙头 公司 估值 对比", "medium", "从行业角度识别横向比较样本。"),
                _query("peer_context", f"{industry} 受益 公司 排序 弹性 标的", "low", "识别市场叙事中的受益排序，但默认仅作弱代理。"),
            ]
        )
    return queries


def _negative_signal_queries(identity: str, industry: str, focus_terms: list[str]) -> list[dict[str, Any]]:
    """生成反方和证伪查询。

    参数：
        identity: 公司搜索身份字符串。
        industry: 行业或板块。
        focus_terms: 用户关注点。
    返回值：
        查询对象列表。
    """
    queries: list[dict[str, Any]] = []
    if identity:
        queries.extend(
            [
                _query("negative_signals", f"{identity} 风险 利空 质疑", "high", "主动寻找反方证据，避免只吃利好叙事。"),
                _query("negative_signals", f"{identity} 业绩 不及预期 毛利率 下滑 应收账款 风险", "high", "寻找会传导到财务和估值的负面变量。"),
            ]
        )
        for term in focus_terms[:3]:
            queries.append(_query("negative_signals", f"{identity} {term} 风险 不及预期", "medium", "对用户 focus 做反向验证。"))
    if industry:
        queries.append(_query("negative_signals", f"{industry} 风险 产能过剩 价格下跌 需求疲弱", "high", "验证行业层反方变量。"))
    return queries


def _global_trend_queries(industry: str, focus_terms: list[str]) -> list[dict[str, Any]]:
    """生成深度档全球趋势查询。

    参数：
        industry: 行业或板块。
        focus_terms: 用户关注点。
    返回值：
        查询对象列表。
    """
    subjects = focus_terms[:4] or ([industry] if industry else ["AI", "semiconductor", "robotics", "data center capex"])
    queries = [
        _query("global_trends", "AI capex 2026 cloud capital expenditure Microsoft Amazon Google Meta", "medium", "跟踪海外云厂商资本开支主线。"),
        _query("global_trends", "NVIDIA TSMC ASML China supply chain demand export restrictions 2026", "medium", "跟踪全球半导体和出口限制变量。"),
    ]
    for subject in subjects:
        queries.append(_query("global_trends", f"{subject} global trend 2026 China A-share supply chain", "low", "把全球趋势映射到 A 股产业链。"))
    return queries
