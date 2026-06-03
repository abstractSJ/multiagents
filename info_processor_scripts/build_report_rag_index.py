"""
财报本地 RAG 索引构建与关键词检索脚本。

第一版只依赖 content.json 生成 JSONL chunk，并提供确定性的关键词检索能力。
这样做的原因是先验证 chunk 粒度、页码追溯和证据命中质量，再决定是否接入向量库或重排模型。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOP_K = 8
MAX_SECTION_CHARS = 24000

SECTION_PATTERNS = [
    ("重要提示", re.compile(r"重要提示")),
    ("公司简介和主要财务指标", re.compile(r"公司简介|主要会计数据|主要财务指标|非经常性损益")),
    ("管理层讨论与分析", re.compile(r"管理层讨论与分析|经营情况讨论与分析|报告期内公司所处行业情况|主营业务分析")),
    ("公司治理", re.compile(r"公司治理|股东大会|董事会|监事会|独立董事")),
    ("环境与社会责任", re.compile(r"环境与社会责任|环境信息|社会责任|ESG|污染物|碳排放")),
    ("重要事项", re.compile(r"重要事项|重大诉讼|仲裁|担保|关联交易|资金占用|承诺事项")),
    ("股东信息", re.compile(r"股份变动|股东情况|前十名股东|控股股东|实际控制人|质押|冻结")),
    ("审计报告", re.compile(r"审计报告|审计意见|关键审计事项|持续经营|强调事项")),
    ("财务报表", re.compile(r"合并资产负债表|合并利润表|合并现金流量表|所有者权益变动表|母公司资产负债表")),
    ("财务报表附注", re.compile(r"财务报表附注|重要会计政策|会计估计|收入确认|应收账款|存货|商誉|关联方")),
]

PRIORITY_KEYWORDS = {
    "S": [
        "审计意见",
        "关键审计事项",
        "持续经营",
        "合并资产负债表",
        "合并利润表",
        "合并现金流量表",
        "营业收入",
        "归属于上市公司股东的净利润",
        "扣除非经常性损益",
        "经营活动产生的现金流量净额",
        "应收账款",
        "存货",
        "商誉",
        "资产减值",
        "信用减值",
        "有息负债",
        "受限资产",
        "资金占用",
        "违规担保",
        "重大诉讼",
        "关联交易",
        "会计政策变更",
        "分红",
        "回购",
    ],
    "A": [
        "主营业务",
        "商业模式",
        "行业情况",
        "产品",
        "地区",
        "客户",
        "供应商",
        "研发投入",
        "产能",
        "资本开支",
        "未来发展",
        "控股股东",
        "实际控制人",
        "内部控制",
        "环境保护",
    ],
    "B": [
        "是否存在",
        "√适用",
        "√不适用",
        "利润分配预案",
        "董事会",
        "监事会",
        "股东大会",
    ],
}

METRIC_KEYWORDS = [
    "营业收入",
    "归属于上市公司股东的净利润",
    "扣除非经常性损益",
    "经营活动产生的现金流量净额",
    "基本每股收益",
    "稀释每股收益",
    "加权平均净资产收益率",
    "总资产",
    "净资产",
    "货币资金",
    "应收账款",
    "存货",
    "商誉",
    "短期借款",
    "长期借款",
    "分红",
    "现金红利",
]

RISK_KEYWORDS = [
    "风险",
    "担保",
    "诉讼",
    "仲裁",
    "资金占用",
    "关联交易",
    "监管处罚",
    "行政处罚",
    "内部控制重大缺陷",
    "违规",
    "质押",
    "冻结",
    "逾期",
    "或有事项",
    "受限资产",
    "减值",
]

FINANCE_QUERY_EXPANSIONS = {
    "利润质量": ["净利润", "扣非", "经营活动产生的现金流量净额", "非经常性损益", "毛利率"],
    "现金流": ["经营活动产生的现金流量净额", "现金及现金等价物", "销售商品、提供劳务收到的现金"],
    "分红": ["利润分配", "现金红利", "股利", "分红率"],
    "应收": ["应收账款", "账龄", "坏账准备", "信用减值"],
    "存货": ["存货", "跌价准备", "库存商品", "原材料"],
    "商誉": ["商誉", "减值测试", "资产组"],
    "担保": ["对外担保", "违规担保", "逾期担保"],
    "关联交易": ["关联方", "关联交易", "资金占用"],
    "审计": ["审计意见", "关键审计事项", "持续经营", "强调事项"],
}

EXPLANATION_QUERY_KEYWORDS = ["为什么", "原因", "下降", "减少", "增加", "变动", "影响", "导致", "主要"]
EXPLANATION_TEXT_KEYWORDS = ["原因", "主要系", "主要是", "由于", "受", "影响", "导致", "下降", "减少", "增加", "变动"]
CHUNK_TYPE_SCORE_BOOST = {"metric": 1.2, "table": 1.0, "page": 0.8, "risk": 0.8, "image": 0.5, "section": 0.2}


@dataclass
class RagChunk:
    """
    单个 RAG chunk 记录。

    参数：
        doc_id: 文档稳定编号。
        chunk_id: chunk 稳定编号。
        chunk_type: chunk 类型，例如 page、section、table、metric、risk、image。
        section: 所属财报章节。
        priority_hint: 规则给出的优先级提示。
        pages: 来源页码。
        text: 用于检索和回答引用的文本。
        metadata: 额外元数据。
    返回值：
        dataclass 实例，无额外返回值。
    """

    doc_id: str
    chunk_id: str
    chunk_type: str
    section: str
    priority_hint: str
    pages: list[int]
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def main() -> None:
    """
    命令行主入口。

    参数：
        无。

    返回值：
        无。
    """
    configure_stdout_encoding()
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "build":
        build_index(args)
    elif args.command == "search":
        search_index(args)
    else:
        parser.print_help()


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    参数：
        无。

    返回值：
        配置完成的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="从财报 content.json 构建本地 RAG JSONL 索引，并提供关键词检索。")
    subparsers = parser.add_subparsers(dest="command")

    build_parser_obj = subparsers.add_parser("build", help="从 content.json 生成 rag_chunks.jsonl 和 rag_index_meta.json。")
    build_parser_obj.add_argument("--content-json", required=True, help="信息处理员生成的 content.json 路径。")
    build_parser_obj.add_argument("--index-dir", default="", help="RAG 索引目录；默认在财报解析目录下创建 rag_index。")
    build_parser_obj.add_argument("--overwrite", action="store_true", help="允许覆盖已有索引文件。")

    search_parser_obj = subparsers.add_parser("search", help="在本地 RAG JSONL 索引中做关键词检索。")
    search_parser_obj.add_argument("--index-dir", required=True, help="build 阶段生成的 rag_index 目录。")
    search_parser_obj.add_argument("--query", required=True, help="检索问题或关键词。")
    search_parser_obj.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="返回结果数量。")
    search_parser_obj.add_argument("--chunk-type", default="", help="可选过滤 chunk_type，例如 table、metric、risk。")
    search_parser_obj.add_argument("--priority", default="", help="可选过滤优先级 S/A/B/C。")
    search_parser_obj.add_argument("--json", action="store_true", help="以 JSON 格式输出检索结果。")
    return parser


def build_index(args: argparse.Namespace) -> None:
    """
    从 content.json 构建本地 RAG 索引。

    参数：
        args: 命令行参数。

    返回值：
        无。
    """
    content_json_path = Path(args.content_json).resolve()
    if not content_json_path.exists():
        raise FileNotFoundError(f"content.json 不存在: {content_json_path}")

    report_dir = content_json_path.parent
    index_dir = Path(args.index_dir).resolve() if args.index_dir else report_dir / "rag_index"
    chunks_path = index_dir / "rag_chunks.jsonl"
    meta_path = index_dir / "rag_index_meta.json"
    if index_dir.exists() and chunks_path.exists() and not args.overwrite:
        raise FileExistsError(f"RAG 索引已存在，如需重建请加 --overwrite: {index_dir}")

    index_dir.mkdir(parents=True, exist_ok=True)
    report = json.loads(content_json_path.read_text(encoding="utf-8"))
    chunks = build_chunks(report, content_json_path)
    write_jsonl(chunks_path, chunks)
    meta = build_index_meta(report, content_json_path, index_dir, chunks)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"RAG 索引构建完成：chunk 数={len(chunks)}")
    print(f"chunks: {chunks_path}")
    print(f"meta: {meta_path}")


def build_chunks(report: dict[str, Any], content_json_path: Path) -> list[RagChunk]:
    """
    基于混合粒度生成 RAG chunk。

    参数：
        report: content.json 解析后的字典。
        content_json_path: content.json 路径。

    返回值：
        RAG chunk 列表。
    """
    metadata = report.get("document_metadata", {}) or {}
    doc_id = build_doc_id(metadata, content_json_path)
    pages = report.get("pages", []) or []
    chunks: list[RagChunk] = []
    page_units = []

    for page in pages:
        page_number = int(page.get("page_number") or 0)
        page_text = build_page_text(page)
        section = detect_section(page_text)
        priority = score_priority(section, page_text)
        page_units.append({"page_number": page_number, "section": section, "priority": priority, "text": page_text})
        chunks.append(
            RagChunk(
                doc_id=doc_id,
                chunk_id=f"page_{page_number:03d}",
                chunk_type="page",
                section=section,
                priority_hint=priority,
                pages=[page_number],
                text=page_text,
                metadata=build_common_metadata(metadata, content_json_path),
            )
        )
        chunks.extend(build_table_chunks(doc_id, metadata, content_json_path, page, section))
        chunks.extend(build_image_chunks(doc_id, metadata, content_json_path, page, section))
        chunks.extend(build_keyword_line_chunks(doc_id, metadata, content_json_path, page_number, page_text, section, "metric", METRIC_KEYWORDS))
        chunks.extend(build_keyword_line_chunks(doc_id, metadata, content_json_path, page_number, page_text, section, "risk", RISK_KEYWORDS))

    chunks.extend(build_section_chunks(doc_id, metadata, content_json_path, page_units))
    return [chunk for chunk in chunks if normalize_space(chunk.text)]


def build_table_chunks(
    doc_id: str,
    metadata: dict[str, Any],
    content_json_path: Path,
    page: dict[str, Any],
    section: str,
) -> list[RagChunk]:
    """
    为单页表格生成独立 chunk。

    参数：
        doc_id: 文档编号。
        metadata: 文档元数据。
        content_json_path: content.json 路径。
        page: 当前页数据。
        section: 当前页章节。

    返回值：
        表格 chunk 列表。
    """
    page_number = int(page.get("page_number") or 0)
    chunks: list[RagChunk] = []
    for table_index, table in enumerate(page.get("tables", []) or [], start=1):
        table_id = str(table.get("table_id") or f"p{page_number:03d}_t{table_index:02d}")
        table_text = str(table.get("markdown") or "")
        if not table_text:
            rows = table.get("rows", []) or []
            table_text = "\n".join(" | ".join(str(cell) for cell in row) for row in rows)
        priority = score_priority(section, table_text)
        chunks.append(
            RagChunk(
                doc_id=doc_id,
                chunk_id=f"page_{page_number:03d}_table_{table_index:02d}",
                chunk_type="table",
                section=section,
                priority_hint=priority,
                pages=[page_number],
                text=table_text,
                metadata={**build_common_metadata(metadata, content_json_path), "table_id": table_id, "bbox": table.get("bbox", [])},
            )
        )
    return chunks


def build_image_chunks(
    doc_id: str,
    metadata: dict[str, Any],
    content_json_path: Path,
    page: dict[str, Any],
    section: str,
) -> list[RagChunk]:
    """
    为被保留图片的摘要生成 chunk。

    参数：
        doc_id: 文档编号。
        metadata: 文档元数据。
        content_json_path: content.json 路径。
        page: 当前页数据。
        section: 当前页章节。

    返回值：
        图片 chunk 列表。
    """
    page_number = int(page.get("page_number") or 0)
    chunks: list[RagChunk] = []
    for image_index, image in enumerate(page.get("images", []) or [], start=1):
        if image.get("decision") != "keep":
            continue
        image_text = normalize_space("；".join([str(image.get("summary", "")), str(image.get("nearby_text", "")), str(image.get("reason", ""))]))
        chunks.append(
            RagChunk(
                doc_id=doc_id,
                chunk_id=f"page_{page_number:03d}_image_{image_index:02d}",
                chunk_type="image",
                section=section,
                priority_hint=score_priority(section, image_text),
                pages=[page_number],
                text=image_text,
                metadata={
                    **build_common_metadata(metadata, content_json_path),
                    "image_id": image.get("image_id", ""),
                    "image_relative_path": image.get("image_relative_path", ""),
                    "category": image.get("category", ""),
                },
            )
        )
    return chunks


def build_keyword_line_chunks(
    doc_id: str,
    metadata: dict[str, Any],
    content_json_path: Path,
    page_number: int,
    page_text: str,
    section: str,
    chunk_type: str,
    keywords: list[str],
) -> list[RagChunk]:
    """
    从页面文本中抽取命中关键词的局部行，生成 metric 或 risk chunk。

    参数：
        doc_id: 文档编号。
        metadata: 文档元数据。
        content_json_path: content.json 路径。
        page_number: 当前页码。
        page_text: 当前页文本。
        section: 当前页章节。
        chunk_type: 输出 chunk 类型。
        keywords: 触发关键词列表。

    返回值：
        局部关键词 chunk 列表。
    """
    lines = split_meaningful_lines(page_text)
    chunks: list[RagChunk] = []
    matched_groups: list[str] = []
    for index, line in enumerate(lines):
        if not any(keyword in line for keyword in keywords):
            continue
        start = max(index - 1, 0)
        end = min(index + 2, len(lines))
        context = "\n".join(lines[start:end])
        if context not in matched_groups:
            matched_groups.append(context)
    for group_index, context in enumerate(matched_groups, start=1):
        chunks.append(
            RagChunk(
                doc_id=doc_id,
                chunk_id=f"page_{page_number:03d}_{chunk_type}_{group_index:02d}",
                chunk_type=chunk_type,
                section=section,
                priority_hint=score_priority(section, context),
                pages=[page_number],
                text=context,
                metadata=build_common_metadata(metadata, content_json_path),
            )
        )
    return chunks


def build_section_chunks(
    doc_id: str,
    metadata: dict[str, Any],
    content_json_path: Path,
    page_units: list[dict[str, Any]],
) -> list[RagChunk]:
    """
    按连续章节聚合页面，生成 section chunk。

    参数：
        doc_id: 文档编号。
        metadata: 文档元数据。
        content_json_path: content.json 路径。
        page_units: 页面单元列表。

    返回值：
        章节 chunk 列表。
    """
    chunks: list[RagChunk] = []
    current_units: list[dict[str, Any]] = []
    current_section = "unknown"
    current_chars = 0

    def flush() -> None:
        """把当前累计的页面组写入章节 chunk。"""
        if not current_units:
            return
        pages = [int(unit["page_number"]) for unit in current_units]
        text = "\n\n".join(str(unit["text"]) for unit in current_units)
        section = str(current_units[0]["section"])
        priority = min((str(unit["priority"]) for unit in current_units), key=priority_sort_key)
        chunks.append(
            RagChunk(
                doc_id=doc_id,
                chunk_id=f"section_{len(chunks) + 1:04d}_p{pages[0]:03d}_p{pages[-1]:03d}",
                chunk_type="section",
                section=section,
                priority_hint=priority,
                pages=pages,
                text=text,
                metadata=build_common_metadata(metadata, content_json_path),
            )
        )

    for unit in page_units:
        unit_section = str(unit["section"])
        unit_chars = len(str(unit["text"]))
        should_flush = bool(current_units) and (unit_section != current_section or current_chars + unit_chars > MAX_SECTION_CHARS)
        if should_flush:
            flush()
            current_units = []
            current_chars = 0
        current_units.append(unit)
        current_section = unit_section
        current_chars += unit_chars
    flush()
    return chunks


def search_index(args: argparse.Namespace) -> None:
    """
    在本地 RAG 索引中检索证据 chunk。

    参数：
        args: 命令行参数。

    返回值：
        无。
    """
    index_dir = Path(args.index_dir).resolve()
    chunks_path = index_dir / "rag_chunks.jsonl"
    if not chunks_path.exists():
        raise FileNotFoundError(f"rag_chunks.jsonl 不存在: {chunks_path}")

    chunks = read_jsonl(chunks_path)
    filtered_chunks = filter_chunks(chunks, args.chunk_type, args.priority)
    results = rank_chunks(filtered_chunks, args.query, args.top_k)
    payload = {"query": args.query, "index_dir": str(index_dir), "top_k": args.top_k, "results": results}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_search_markdown(payload))


def rank_chunks(chunks: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
    """
    使用关键词、数字和优先级的混合分数排序 chunk。

    参数：
        chunks: 候选 chunk 列表。
        query: 查询文本。
        top_k: 返回数量。

    返回值：
        排序后的检索结果。
    """
    query_terms = expand_query_terms(query)
    query_numbers = extract_number_tokens(query)
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        text = str(chunk.get("text", ""))
        score, matched_terms = score_chunk(
            text,
            query,
            query_terms,
            query_numbers,
            str(chunk.get("priority_hint", "")),
            str(chunk.get("chunk_type", "")),
        )
        if score <= 0:
            continue
        snippet = make_snippet(text, query_terms, query)
        scored.append({**chunk, "score": round(score, 4), "matched_terms": matched_terms, "snippet": snippet})
    scored.sort(key=lambda item: (-float(item["score"]), priority_sort_key(str(item.get("priority_hint", ""))), min(item.get("pages") or [9999])))
    return scored[:top_k]


def score_chunk(
    text: str,
    query: str,
    query_terms: list[str],
    query_numbers: list[str],
    priority_hint: str,
    chunk_type: str,
) -> tuple[float, list[str]]:
    """
    计算单个 chunk 的检索分数。

    参数：
        text: chunk 文本。
        query: 原始查询。
        query_terms: 查询词及扩展词。
        query_numbers: 查询中的数字。
        priority_hint: chunk 优先级。
        chunk_type: chunk 类型。

    返回值：
        分数和命中词列表。
    """
    normalized_text = normalize_space(text)
    score = 0.0
    matched_terms: list[str] = []
    if query and query in normalized_text:
        score += 8.0
        matched_terms.append(query)
    for term in query_terms:
        if not term:
            continue
        count = normalized_text.count(term)
        if count:
            score += 2.0 + math.log1p(count)
            matched_terms.append(term)
    for number in query_numbers:
        if number and number in normalized_text:
            score += 3.0
            matched_terms.append(number)
    score += {"S": 1.5, "A": 0.8, "B": 0.3}.get(priority_hint.upper(), 0.0)
    score += CHUNK_TYPE_SCORE_BOOST.get(chunk_type, 0.0)
    if is_operating_cashflow_query(query):
        if "经营活动产生的现金流量净额" in normalized_text:
            score += 8.0
            matched_terms.append("经营活动产生的现金流量净额")
        elif "经营活动" in normalized_text and "现金流" in normalized_text:
            score += 3.0
            matched_terms.append("经营活动现金流")
    if is_explanation_query(query):
        explanation_hits = [keyword for keyword in EXPLANATION_TEXT_KEYWORDS if keyword in normalized_text]
        if explanation_hits:
            score += 2.0 + min(len(explanation_hits), 4) * 0.25
            matched_terms.extend(explanation_hits)
        if chunk_type == "section" and len(normalized_text) > 4000:
            score -= 4.0
    return score, deduplicate(matched_terms)


def is_explanation_query(query: str) -> bool:
    """
    判断查询是否在寻找变化原因或管理层解释。

    参数：
        query: 原始查询。

    返回值：
        如果问题包含原因、变化或影响类词语则返回 True。
    """
    return any(keyword in query for keyword in EXPLANATION_QUERY_KEYWORDS)


def is_operating_cashflow_query(query: str) -> bool:
    """
    判断查询是否指向经营现金流。

    参数：
        query: 原始查询。

    返回值：
        如果查询同时包含经营语义和现金流语义则返回 True。
    """
    return ("经营" in query and "现金" in query) or "经营活动现金流" in query or "经营现金流" in query


def expand_query_terms(query: str) -> list[str]:
    """
    为财报问题生成关键词扩展。

    参数：
        query: 原始查询。

    返回值：
        去重后的关键词列表。
    """
    terms: list[str] = []
    query = normalize_space(query)
    terms.append(query)
    for key, expansions in FINANCE_QUERY_EXPANSIONS.items():
        if key in query:
            terms.extend(expansions)
    if is_explanation_query(query):
        terms.extend(EXPLANATION_TEXT_KEYWORDS)
    terms.extend(re.findall(r"[A-Za-z0-9_\-.%]+", query))
    terms.extend(term for term in METRIC_KEYWORDS + RISK_KEYWORDS if term in query)
    chinese_terms = re.findall(r"[一-鿿]{2,}", query)
    terms.extend(chinese_terms)
    return deduplicate([term for term in terms if len(term) >= 2])


def extract_number_tokens(text: str) -> list[str]:
    """
    从文本中抽取数字 token。

    参数：
        text: 输入文本。

    返回值：
        数字字符串列表。
    """
    return deduplicate(re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?%?", text))


def filter_chunks(chunks: list[dict[str, Any]], chunk_type: str, priority: str) -> list[dict[str, Any]]:
    """
    按 chunk 类型和优先级过滤候选结果。

    参数：
        chunks: 原始 chunk 列表。
        chunk_type: 指定 chunk 类型。
        priority: 指定优先级。

    返回值：
        过滤后的 chunk 列表。
    """
    results = chunks
    if chunk_type:
        results = [chunk for chunk in results if str(chunk.get("chunk_type", "")) == chunk_type]
    if priority:
        results = [chunk for chunk in results if str(chunk.get("priority_hint", "")).upper() == priority.upper()]
    return results


def render_search_markdown(payload: dict[str, Any]) -> str:
    """
    将检索结果渲染为 Markdown。

    参数：
        payload: 检索结果 payload。

    返回值：
        Markdown 字符串。
    """
    lines = [f"## 检索问题", "", str(payload.get("query", "")), "", "## 证据", ""]
    results = payload.get("results", []) or []
    if not results:
        lines.append("（未命中。）")
        return "\n".join(lines)
    for index, item in enumerate(results, start=1):
        lines.append(
            f"{index}. score={item.get('score')}；chunk_id={item.get('chunk_id')}；"
            f"type={item.get('chunk_type')}；priority={item.get('priority_hint')}；页码={item.get('pages')}；章节={item.get('section')}"
        )
        lines.append(f"   - 命中词: {', '.join(item.get('matched_terms', []))}")
        lines.append(f"   - 摘录: {item.get('snippet', '')}")
    return "\n".join(lines)


def build_page_text(page: dict[str, Any]) -> str:
    """
    将单页正文、表格和保留图片摘要合并为页面检索文本。

    参数：
        page: content.json 中的单页记录。

    返回值：
        页面检索文本。
    """
    page_number = int(page.get("page_number") or 0)
    parts = [f"第 {page_number} 页", str(page.get("text") or "")]
    for table in page.get("tables", []) or []:
        markdown = str(table.get("markdown") or "")
        if markdown:
            parts.append(f"表格 {table.get('table_id', '')}\n{markdown}")
    for image in page.get("images", []) or []:
        if image.get("decision") == "keep":
            parts.append(f"图片 {image.get('image_id', '')}: {image.get('summary', '')}；{image.get('nearby_text', '')}")
    return "\n".join(part for part in parts if part)


def detect_section(text: str) -> str:
    """
    使用关键词规则识别文本所属章节。

    参数：
        text: 待识别文本。

    返回值：
        章节名称。
    """
    for section_name, pattern in SECTION_PATTERNS:
        if pattern.search(text):
            return section_name
    return "unknown"


def score_priority(section: str, text: str) -> str:
    """
    根据章节和关键词给 RAG chunk 标注优先级。

    参数：
        section: 章节名称。
        text: chunk 文本。

    返回值：
        S/A/B/C 优先级。
    """
    if section in {"审计报告", "财务报表", "财务报表附注", "管理层讨论与分析", "重要事项"}:
        return "S"
    for priority in ["S", "A", "B"]:
        if any(keyword in text for keyword in PRIORITY_KEYWORDS[priority]):
            return priority
    if any(keyword in text for keyword in ["目录", "备查文件", "前瞻性陈述", "敬请投资者注意"]):
        return "C"
    return "A"


def build_doc_id(metadata: dict[str, Any], content_json_path: Path) -> str:
    """
    构建文档稳定编号。

    参数：
        metadata: 文档元数据。
        content_json_path: content.json 路径。

    返回值：
        文档编号字符串。
    """
    parts = [metadata.get("stock_code", ""), metadata.get("company_name", ""), metadata.get("report_year", ""), metadata.get("report_type_label", "")]
    doc_id = "-".join(str(part) for part in parts if part)
    return doc_id or content_json_path.parent.name


def build_common_metadata(metadata: dict[str, Any], content_json_path: Path) -> dict[str, Any]:
    """
    构建每个 chunk 共享的元数据。

    参数：
        metadata: 文档元数据。
        content_json_path: content.json 路径。

    返回值：
        元数据字典。
    """
    return {
        "stock_code": metadata.get("stock_code", ""),
        "company_name": metadata.get("company_name", ""),
        "report_year": metadata.get("report_year", ""),
        "report_type": metadata.get("report_type", ""),
        "report_variant": metadata.get("report_variant", ""),
        "announcement_id": metadata.get("announcement_id", ""),
        "source_json_path": str(content_json_path),
    }


def build_index_meta(report: dict[str, Any], content_json_path: Path, index_dir: Path, chunks: list[RagChunk]) -> dict[str, Any]:
    """
    构建 RAG 索引元数据。

    参数：
        report: content.json 字典。
        content_json_path: content.json 路径。
        index_dir: 索引目录。
        chunks: 已生成 chunk 列表。

    返回值：
        索引元数据字典。
    """
    counts: dict[str, int] = {}
    priority_counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.chunk_type] = counts.get(chunk.chunk_type, 0) + 1
        priority_counts[chunk.priority_hint] = priority_counts.get(chunk.priority_hint, 0) + 1
    return {
        "generated_at": utc_now(),
        "content_json_path": str(content_json_path),
        "index_dir": str(index_dir),
        "document_metadata": report.get("document_metadata", {}),
        "chunk_count": len(chunks),
        "chunk_type_counts": counts,
        "priority_counts": priority_counts,
        "chunk_file": str(index_dir / "rag_chunks.jsonl"),
    }


def split_meaningful_lines(text: str) -> list[str]:
    """
    将文本切成适合局部证据抽取的行。

    参数：
        text: 原始文本。

    返回值：
        清洗后的非空行列表。
    """
    raw_lines = re.split(r"[\n\r]+", text)
    lines = [normalize_space(line) for line in raw_lines]
    return [line for line in lines if len(line) >= 4]


def normalize_space(text: str) -> str:
    """
    压缩文本空白。

    参数：
        text: 原始文本。

    返回值：
        清洗后的文本。
    """
    return re.sub(r"\s+", " ", str(text)).strip()


def make_snippet(text: str, query_terms: list[str], query: str, max_length: int = 320) -> str:
    """
    生成包含命中词附近上下文的短摘录。

    参数：
        text: chunk 文本。
        query_terms: 查询词列表。
        query: 原始查询。
        max_length: 最大摘录长度。

    返回值：
        摘录文本。
    """
    compact_text = normalize_space(text)
    anchors = [query] + query_terms
    positions = [compact_text.find(anchor) for anchor in anchors if anchor and compact_text.find(anchor) >= 0]
    if not positions:
        return compact_text[:max_length]
    start = max(min(positions) - 80, 0)
    return compact_text[start : start + max_length]


def deduplicate(items: list[str]) -> list[str]:
    """
    保持顺序去重。

    参数：
        items: 原始字符串列表。

    返回值：
        去重后的字符串列表。
    """
    seen: set[str] = set()
    results: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        results.append(item)
    return results


def priority_sort_key(priority: str) -> int:
    """
    将优先级转换为排序数值。

    参数：
        priority: S/A/B/C。

    返回值：
        排序数值，越小越重要。
    """
    return {"S": 0, "A": 1, "B": 2, "C": 3}.get(priority.upper(), 4)


def write_jsonl(path: Path, chunks: list[RagChunk]) -> None:
    """
    写出 JSONL chunk 文件。

    参数：
        path: 输出路径。
        chunks: RAG chunk 列表。

    返回值：
        无。
    """
    with path.open("w", encoding="utf-8") as file_obj:
        for chunk in chunks:
            file_obj.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """
    读取 JSONL 文件。

    参数：
        path: JSONL 文件路径。

    返回值：
        字典列表。
    """
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def utc_now() -> str:
    """
    获取当前 UTC 时间。

    参数：
        无。

    返回值：
        ISO 8601 时间字符串。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def configure_stdout_encoding() -> None:
    """
    将标准输出切换为 UTF-8，避免 Windows Bash 捕获中文时乱码。

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
