"""
全文提取结果与年报摘要公告的有效性比对工具。

该脚本把正式年报 content.json、LLM digest、RAG 索引和摘要版 PDF 放在一起检查。
核心目标不是证明摘要完全正确，而是用摘要公告作为外部参照，评估信息处理员是否覆盖了摘要中高价值的关键词和数字。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None  # type: ignore

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # pragma: no cover
    PdfReader = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COLLECTOR_WORKSPACE = PROJECT_ROOT / "info_collector_scripts" / "collector_workspace"
DEFAULT_COLLECTOR_MANIFEST = DEFAULT_COLLECTOR_WORKSPACE / "manifests" / "cninfo_all_reports.json"
DEFAULT_PROCESSOR_WORKSPACE = PROJECT_ROOT / "info_processor_scripts" / "processor_workspace"
SUMMARY_KEYWORDS = [
    "营业收入",
    "归属于上市公司股东的净利润",
    "归属于上市公司股东的扣除非经常性损益的净利润",
    "经营活动产生的现金流量净额",
    "基本每股收益",
    "稀释每股收益",
    "加权平均净资产收益率",
    "总资产",
    "归属于上市公司股东的净资产",
    "分红",
    "利润分配",
    "现金红利",
    "主营业务",
    "审计意见",
    "非标准审计意见",
    "关键审计事项",
    "控股股东",
    "实际控制人",
    "关联交易",
    "资金占用",
    "对外担保",
    "重大诉讼",
    "内部控制",
    "应收账款",
    "存货",
    "商誉",
    "研发投入",
    "非经常性损益",
]


@dataclass
class CoverageItem:
    """
    摘要参照项的覆盖结果。

    参数：
        item_type: 项目类型，keyword 或 number。
        value: 摘要中抽取的关键词或数字。
        in_digest: 是否被 digest 覆盖。
        in_rag: 是否被 RAG chunk 覆盖。
        rag_hits: RAG 命中的 chunk_id 列表。
    返回值：
        dataclass 实例，无额外返回值。
    """

    item_type: str
    value: str
    in_digest: bool
    in_rag: bool
    rag_hits: list[str] = field(default_factory=list)


def main() -> None:
    """
    命令行主入口。

    参数：
        无。

    返回值：
        无。
    """
    configure_stdout_encoding()
    args = build_parser().parse_args()
    compare(args)


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    参数：
        无。

    返回值：
        参数解析器。
    """
    parser = argparse.ArgumentParser(description="比对正式年报提取结果与摘要版 PDF，评估 Digest/RAG 覆盖有效性。")
    parser.add_argument("--content-json", required=True, help="正式年报 content.json 路径。")
    parser.add_argument("--summary-pdf", default="", help="摘要版 PDF 路径；不传时根据元数据和收集员清单自动寻找。")
    parser.add_argument("--digest-json", default="", help="llm_digest.json 路径；默认取 content.json 同目录。")
    parser.add_argument("--rag-index-dir", default="", help="RAG 索引目录；默认取 content.json 同目录下 rag_index。")
    parser.add_argument("--collector-manifest", default=str(DEFAULT_COLLECTOR_MANIFEST), help="信息收集员总清单。")
    parser.add_argument("--collector-workspace", default=str(DEFAULT_COLLECTOR_WORKSPACE), help="信息收集员工作区。")
    parser.add_argument("--output", default="", help="比对报告 JSON 输出路径；默认写到正式年报目录 summary_comparison.json。")
    parser.add_argument("--markdown-output", default="", help="比对报告 Markdown 输出路径；默认写到正式年报目录 summary_comparison.md。")
    return parser


def compare(args: argparse.Namespace) -> None:
    """
    执行正式年报提取结果与摘要版 PDF 的覆盖比对。

    参数：
        args: 命令行参数。

    返回值：
        无。
    """
    content_json_path = Path(args.content_json).resolve()
    if not content_json_path.exists():
        raise FileNotFoundError(f"正式年报 content.json 不存在: {content_json_path}")

    report = json.loads(content_json_path.read_text(encoding="utf-8"))
    metadata = report.get("document_metadata", {}) or {}
    summary_pdf_path = resolve_summary_pdf(args, metadata)
    summary_text = extract_pdf_text(summary_pdf_path)
    summary_keywords = extract_summary_keywords(summary_text)
    summary_numbers = extract_summary_numbers(summary_text)

    digest_json_path = Path(args.digest_json).resolve() if args.digest_json else content_json_path.parent / "llm_digest.json"
    digest_text = load_digest_text(digest_json_path)
    rag_index_dir = Path(args.rag_index_dir).resolve() if args.rag_index_dir else content_json_path.parent / "rag_index"
    rag_chunks = load_rag_chunks(rag_index_dir)

    coverage_items = build_coverage_items(summary_keywords, summary_numbers, digest_text, rag_chunks)
    digest_keyword_coverage = ratio([item for item in coverage_items if item.item_type == "keyword" and item.in_digest], [item for item in coverage_items if item.item_type == "keyword"])
    rag_keyword_coverage = ratio([item for item in coverage_items if item.item_type == "keyword" and item.in_rag], [item for item in coverage_items if item.item_type == "keyword"])
    digest_number_coverage = ratio([item for item in coverage_items if item.item_type == "number" and item.in_digest], [item for item in coverage_items if item.item_type == "number"])
    rag_number_coverage = ratio([item for item in coverage_items if item.item_type == "number" and item.in_rag], [item for item in coverage_items if item.item_type == "number"])

    payload = {
        "generated_at": utc_now(),
        "content_json_path": str(content_json_path),
        "summary_pdf_path": str(summary_pdf_path),
        "digest_json_path": str(digest_json_path),
        "rag_index_dir": str(rag_index_dir),
        "summary_text_chars": len(summary_text),
        "summary_keyword_count": len(summary_keywords),
        "summary_number_count": len(summary_numbers),
        "rag_chunk_count": len(rag_chunks),
        "coverage": {
            "digest_keyword_coverage": digest_keyword_coverage,
            "rag_keyword_coverage": rag_keyword_coverage,
            "digest_number_coverage": digest_number_coverage,
            "rag_number_coverage": rag_number_coverage,
        },
        "items": [asdict(item) for item in coverage_items],
        "missing_in_digest": [asdict(item) for item in coverage_items if not item.in_digest],
        "missing_in_rag": [asdict(item) for item in coverage_items if not item.in_rag],
        "interpretation": build_interpretation(digest_keyword_coverage, rag_keyword_coverage, digest_number_coverage, rag_number_coverage),
    }

    output_path = Path(args.output).resolve() if args.output else content_json_path.parent / "summary_comparison.json"
    markdown_output_path = Path(args.markdown_output).resolve() if args.markdown_output else content_json_path.parent / "summary_comparison.md"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_output_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"摘要比对完成：{markdown_output_path}")
    print(f"JSON: {output_path}")
    print(json.dumps(payload["coverage"], ensure_ascii=False, indent=2))


def resolve_summary_pdf(args: argparse.Namespace, metadata: dict[str, Any]) -> Path:
    """
    定位摘要版 PDF。

    参数：
        args: 命令行参数。
        metadata: 正式年报元数据。

    返回值：
        摘要版 PDF 路径。
    """
    if args.summary_pdf:
        summary_path = Path(args.summary_pdf).resolve()
        if not summary_path.exists():
            raise FileNotFoundError(f"摘要 PDF 不存在: {summary_path}")
        return summary_path

    manifest_path = Path(args.collector_manifest).resolve()
    collector_workspace = Path(args.collector_workspace).resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"信息收集员 manifest 不存在，无法自动寻找摘要 PDF: {manifest_path}")

    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    stock_code = str(metadata.get("stock_code", ""))
    report_year = str(metadata.get("report_year", ""))
    report_type = str(metadata.get("report_type", ""))
    candidates = []
    for record in records:
        if str(record.get("stock_code", "")) != stock_code:
            continue
        if str(record.get("report_year", "")) != report_year:
            continue
        if str(record.get("report_type", "")) != report_type:
            continue
        classification = str(record.get("title_classification", ""))
        title = str(record.get("title", ""))
        variant = str(record.get("report_variant", ""))
        if "summary" not in classification and "摘要" not in title and "摘要" not in variant:
            continue
        relative_path = str(record.get("local_relative_path", ""))
        if relative_path:
            candidates.append(collector_workspace / relative_path)
    existing_candidates = [path.resolve() for path in candidates if path.exists()]
    if not existing_candidates:
        raise FileNotFoundError(f"未找到 {stock_code} {report_year} {report_type} 的本地摘要 PDF。")
    return existing_candidates[0]


def extract_pdf_text(pdf_path: Path) -> str:
    """
    从摘要版 PDF 中提取文本。

    参数：
        pdf_path: PDF 路径。

    返回值：
        PDF 文本。
    """
    if fitz is not None:
        with fitz.open(pdf_path) as document:
            return "\n".join(page.get_text("text") for page in document)
    if PdfReader is None:
        raise RuntimeError("当前环境缺少 PyMuPDF 和 pypdf，无法提取摘要 PDF 文本。")
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_summary_keywords(summary_text: str) -> list[str]:
    """
    抽取摘要中出现的核心财报关键词。

    参数：
        summary_text: 摘要 PDF 文本。

    返回值：
        关键词列表。
    """
    return [keyword for keyword in SUMMARY_KEYWORDS if keyword in summary_text]


def extract_summary_numbers(summary_text: str) -> list[str]:
    """
    抽取摘要中的代表性数字。

    参数：
        summary_text: 摘要 PDF 文本。

    返回值：
        数字字符串列表，最多保留前 80 个。
    """
    numbers = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?%?", summary_text)
    normalized_numbers = []
    for number in numbers:
        if len(number.replace(",", "")) < 4 and not number.endswith("%"):
            continue
        normalized_numbers.append(number)
    return deduplicate(normalized_numbers)[:80]


def load_digest_text(digest_json_path: Path) -> str:
    """
    读取 digest JSON 并展开为可搜索文本。

    参数：
        digest_json_path: digest JSON 路径。

    返回值：
        digest 文本。
    """
    if not digest_json_path.exists():
        return ""
    payload = json.loads(digest_json_path.read_text(encoding="utf-8"))
    return json.dumps(payload, ensure_ascii=False)


def load_rag_chunks(rag_index_dir: Path) -> list[dict[str, Any]]:
    """
    读取 RAG JSONL chunk。

    参数：
        rag_index_dir: RAG 索引目录。

    返回值：
        chunk 字典列表。
    """
    chunks_path = rag_index_dir / "rag_chunks.jsonl"
    if not chunks_path.exists():
        return []
    chunks = []
    with chunks_path.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


def build_coverage_items(summary_keywords: list[str], summary_numbers: list[str], digest_text: str, rag_chunks: list[dict[str, Any]]) -> list[CoverageItem]:
    """
    构建关键词和数字覆盖结果。

    参数：
        summary_keywords: 摘要关键词。
        summary_numbers: 摘要数字。
        digest_text: digest 文本。
        rag_chunks: RAG chunk 列表。

    返回值：
        覆盖结果列表。
    """
    items: list[CoverageItem] = []
    for keyword in summary_keywords:
        rag_hits = find_rag_hits(keyword, rag_chunks)
        items.append(CoverageItem("keyword", keyword, keyword in digest_text, bool(rag_hits), rag_hits))
    for number in summary_numbers:
        normalized_number = normalize_number(number)
        digest_hit = number in digest_text or normalized_number in normalize_number(digest_text)
        rag_hits = find_rag_hits(number, rag_chunks)
        if not rag_hits and normalized_number != number:
            rag_hits = find_rag_hits(normalized_number, rag_chunks)
        items.append(CoverageItem("number", number, digest_hit, bool(rag_hits), rag_hits))
    return items


def find_rag_hits(value: str, rag_chunks: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """
    查找包含指定值的 RAG chunk。

    参数：
        value: 关键词或数字。
        rag_chunks: RAG chunk 列表。
        limit: 最多返回 chunk 数量。

    返回值：
        chunk_id 列表。
    """
    hits = []
    normalized_value = normalize_number(value)
    for chunk in rag_chunks:
        text = str(chunk.get("text", ""))
        normalized_text = normalize_number(text)
        if value in text or (normalized_value and normalized_value in normalized_text):
            hits.append(str(chunk.get("chunk_id", "")))
        if len(hits) >= limit:
            break
    return hits


def normalize_number(text: str) -> str:
    """
    统一数字字符串中的千分位逗号。

    参数：
        text: 原始文本。

    返回值：
        去除数字千分位逗号后的文本。
    """
    return re.sub(r"(?<=\d),(?=\d)", "", str(text))


def ratio(numerator: list[Any], denominator: list[Any]) -> float:
    """
    安全计算覆盖率。

    参数：
        numerator: 命中项。
        denominator: 总项。

    返回值：
        覆盖率。
    """
    if not denominator:
        return 0.0
    return round(len(numerator) / len(denominator), 4)


def build_interpretation(digest_keyword: float, rag_keyword: float, digest_number: float, rag_number: float) -> list[str]:
    """
    生成覆盖率解释。

    参数：
        digest_keyword: digest 关键词覆盖率。
        rag_keyword: RAG 关键词覆盖率。
        digest_number: digest 数字覆盖率。
        rag_number: RAG 数字覆盖率。

    返回值：
        解释文本列表。
    """
    messages = []
    if rag_keyword >= 0.85 and rag_number >= 0.75:
        messages.append("RAG 对摘要关键词和代表性数字覆盖较好，说明 content.json 到 RAG chunk 的证据保留有效。")
    else:
        messages.append("RAG 覆盖率偏低时，应优先检查摘要 PDF 解析质量、数字格式差异和 chunk 切分粒度。")
    if digest_keyword >= 0.65 and digest_number >= 0.45:
        messages.append("Digest 已覆盖摘要中的主要高价值信息，适合作为财务分析员的压缩输入。")
    else:
        messages.append("Digest 覆盖率偏低时，不代表底层解析失败，可能是 subagent 尚未全量处理或摘要信息被归入未处理 chunk。")
    return messages


def render_markdown(payload: dict[str, Any]) -> str:
    """
    渲染 Markdown 比对报告。

    参数：
        payload: 比对结果 payload。

    返回值：
        Markdown 字符串。
    """
    lines = ["# 年报全文提取结果与摘要公告比对", ""]
    lines.extend(
        [
            "## 1. 输入",
            "",
            f"- 正式年报 content.json: {payload.get('content_json_path', '')}",
            f"- 摘要 PDF: {payload.get('summary_pdf_path', '')}",
            f"- digest JSON: {payload.get('digest_json_path', '')}",
            f"- RAG 索引目录: {payload.get('rag_index_dir', '')}",
            "",
            "## 2. 覆盖率",
            "",
        ]
    )
    for key, value in payload.get("coverage", {}).items():
        lines.append(f"- {key}: {value:.2%}")
    lines.extend(["", "## 3. 解释", ""])
    for item in payload.get("interpretation", []):
        lines.append(f"- {item}")
    lines.extend(["", "## 4. 摘要项目覆盖明细", ""])
    for item in payload.get("items", []):
        lines.append(
            f"- {item.get('item_type')}: {item.get('value')}；"
            f"digest={item.get('in_digest')}；RAG={item.get('in_rag')}；hits={item.get('rag_hits')}"
        )
    return "\n".join(lines) + "\n"


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
