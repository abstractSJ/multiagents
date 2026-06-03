"""
财报 LLM Digest 构建流水线。

该脚本采用“代码切片 + subagent 分段处理 + 代码合并”的架构：
1. 主进程只读取 content.json，切成可独立处理的 chunk 文件；
2. 每个 subagent 只读取一个 chunk，并把结构化分析结果写入独立 JSON；
3. 主进程只读取 subagent 的结构化结果，合并生成 llm_digest.json、llm_digest.md 和 digest_audit.json。

这样设计的核心原因是避免在同一个会话中不断累积整份财报上下文，导致上下文压缩后丢失信息。
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

PRIORITY_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "UNKNOWN": 4}

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

S_KEYWORDS = [
    "审计意见",
    "关键审计事项",
    "持续经营",
    "强调事项",
    "非标准",
    "保留意见",
    "无法表示意见",
    "否定意见",
    "资产负债表",
    "利润表",
    "现金流量表",
    "营业收入",
    "归属于上市公司股东的净利润",
    "扣除非经常性损益",
    "经营活动产生的现金流量净额",
    "毛利率",
    "应收账款",
    "存货",
    "商誉",
    "资产减值",
    "信用减值",
    "有息负债",
    "短期借款",
    "一年内到期",
    "受限资产",
    "对外担保",
    "资金占用",
    "关联交易",
    "重大诉讼",
    "或有事项",
    "会计政策变更",
    "会计估计变更",
    "差错更正",
    "分红",
    "回购",
]

A_KEYWORDS = [
    "主营业务",
    "行业情况",
    "商业模式",
    "产品",
    "地区",
    "客户",
    "供应商",
    "研发投入",
    "产能",
    "资本开支",
    "未来发展",
    "股东",
    "实际控制人",
    "控股股东",
    "董事",
    "高级管理人员",
    "内部控制",
    "社会责任",
    "环境保护",
]

B_FLAG_KEYWORDS = [
    "是否存在",
    "□适用",
    "√适用",
    "√不适用",
    "资金占用",
    "违规担保",
    "半数以上董事",
    "非标准审计意见",
    "内部控制缺陷",
    "利润分配预案",
]

C_KEYWORDS = [
    "本公司董事会及董事",
    "保证年度报告内容的真实性",
    "前瞻性陈述",
    "敬请投资者注意",
    "备查文件",
    "目录",
    "公司严格按照",
    "不断完善法人治理结构",
    "稳中求进",
    "提升管理水平",
]

HIGH_PRIORITY_SECTIONS = {"审计报告", "财务报表", "财务报表附注", "管理层讨论与分析", "重要事项"}
GOVERNANCE_SECTIONS = {"公司治理", "股东信息"}

TABLE_HIGH_VALUE_KEYWORDS = [
    "合并资产负债表",
    "合并利润表",
    "合并现金流量表",
    "主要会计数据",
    "主要财务指标",
    "分产品",
    "分地区",
    "应收账款",
    "存货",
    "商誉",
    "担保",
    "诉讼",
    "关联交易",
]

GOVERNANCE_EXCEPTION_KEYWORDS = [
    "非经营性占用资金且尚未清偿",
    "违规担保",
    "逾期担保",
    "资金占用余额",
    "无法保证",
    "异议",
    "反对票",
    "弃权票",
    "行政处罚",
    "监管处罚",
    "纪律处分",
    "公开谴责",
    "立案调查",
    "重大诉讼",
    "重大仲裁",
    "司法冻结",
    "质押比例",
    "实际控制人变更",
    "控股股东变更",
    "内部控制重大缺陷",
    "非标准内部控制审计意见",
]

DIGEST_SECTION_RULES = [
    ("audit", "## 2. 审计意见与关键审计事项", ["审计", "关键审计", "持续经营", "强调事项", "非标准", "保留意见", "否定意见", "无法表示意见"]),
    ("core_metrics", "## 3. 核心财务指标总览", ["主要会计数据", "主要财务指标", "营业收入", "归母净利润", "扣非", "每股收益", "净资产收益率", "ROE"]),
    ("income_quality", "## 4. 利润表与盈利质量", ["利润", "毛利", "净利", "营业成本", "销售费用", "管理费用", "财务费用", "研发", "减值", "非经常性损益"]),
    ("balance_sheet", "## 5. 资产负债表风险", ["资产", "负债", "货币资金", "应收", "存货", "商誉", "固定资产", "在建工程", "债权投资", "长期股权", "所有者权益", "净资产"]),
    ("cashflow", "## 6. 现金流质量", ["现金流", "现金及现金等价物", "经营活动", "投资活动", "筹资活动"]),
    ("business", "## 7. 收入结构、产品、地区与毛利率", ["收入结构", "分产品", "分地区", "分行业", "主营业务", "商业模式", "行业", "客户", "供应商", "产品", "地区", "渠道", "产能"]),
    ("capital_allocation", "## 8. 分红、回购、融资与资本开支", ["分红", "股利", "利润分配", "回购", "融资", "筹资", "资本开支", "投资", "并购"]),
    ("mda", "## 9. 管理层讨论与未来展望", ["管理层", "经营情况", "原因", "未来", "展望", "战略", "计划"]),
    ("risk_governance", "## 10. 风险、治理、内控与异常事项", ["风险", "担保", "诉讼", "仲裁", "关联交易", "资金占用", "内控", "处罚", "监管", "治理", "质押", "冻结", "董事", "股东"]),
]

DIGEST_RESULT_SCHEMA = {
    "chunk_id": "字符串，必须等于输入 chunk_id",
    "pages": "页码数组",
    "detected_section": "识别出的章节名称",
    "priority": "S/A/B/C 之一",
    "keep": "布尔值，是否有内容进入最终 digest 主体",
    "priority_reason": "优先级判断原因",
    "key_findings": [
        {
            "topic": "主题，例如营业收入、审计意见、应收账款",
            "summary": "只保留对财务分析有价值的信息",
            "numbers": [
                {
                    "name": "指标名",
                    "value": "数值原文",
                    "period": "期间",
                    "unit": "单位",
                    "source_pages": [1],
                }
            ],
            "source_pages": [1],
        }
    ],
    "risks": [
        {
            "risk_type": "风险类别",
            "summary": "具体风险内容",
            "severity": "high/medium/low/unknown",
            "source_pages": [1],
        }
    ],
    "flags": {},
    "discarded_content": [
        {
            "type": "template/noise/repeated/low_value",
            "summary": "被丢弃内容概述",
            "reason": "丢弃原因",
            "source_pages": [1],
        }
    ],
    "source_page_index": [
        {
            "page": 1,
            "used_for": "本页用于哪些发现或为何丢弃",
        }
    ],
}


@dataclass
class ChunkManifestItem:
    """
    单个 digest chunk 的清单记录。

    参数：
        chunk_id: chunk 稳定编号。
        sequence: chunk 顺序号。
        pages: chunk 覆盖的 PDF 页码。
        char_count: chunk JSON 中可分析文本的字符数量。
        detected_section: 规则识别出的章节。
        rule_priority_hint: 规则给出的初始优先级提示。
        hit_keywords: 命中的升权关键词。
        drop_keywords: 命中的降权关键词。
        chunk_path: chunk JSON 相对 pipeline 目录路径。
        prompt_path: subagent 提示文件相对 pipeline 目录路径。
        result_path: subagent 结果文件相对 pipeline 目录路径。
    返回值：
        dataclass 实例，无额外返回值。
    """

    chunk_id: str
    sequence: int
    pages: list[int]
    char_count: int
    detected_section: str
    rule_priority_hint: str
    hit_keywords: list[str] = field(default_factory=list)
    drop_keywords: list[str] = field(default_factory=list)
    chunk_path: str = ""
    prompt_path: str = ""
    result_path: str = ""


@dataclass
class PipelineState:
    """
    digest pipeline 的整体状态。

    参数：
        content_json_path: 原始 content.json 路径。
        pipeline_dir: digest pipeline 目录。
        report_dir: 当前财报解析目录。
        generated_at: 本次状态生成时间。
        document_metadata: 原始财报元数据。
        chunks: chunk 清单。
    返回值：
        dataclass 实例，无额外返回值。
    """

    content_json_path: str
    pipeline_dir: str
    report_dir: str
    generated_at: str
    document_metadata: dict[str, Any]
    chunks: list[ChunkManifestItem]


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
    if args.command == "prepare":
        prepare_pipeline(args)
    elif args.command == "merge":
        merge_pipeline(args)
    elif args.command == "status":
        show_status(args)
    elif args.command == "auto-digest":
        auto_digest_pipeline(args)
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
    parser = argparse.ArgumentParser(description="构建财报 LLM Digest 的切片、subagent 提示和合并产物。")
    subparsers = parser.add_subparsers(dest="command")

    prepare_parser = subparsers.add_parser("prepare", help="从 content.json 生成 chunk、prompt 和清单。")
    prepare_parser.add_argument("--content-json", required=True, help="信息处理员生成的 content.json 路径。")
    prepare_parser.add_argument("--pipeline-dir", default="", help="digest pipeline 输出目录；默认在财报目录下创建 digest_pipeline。")
    prepare_parser.add_argument("--max-chars-per-chunk", type=int, default=16000, help="每个 chunk 的目标最大字符数；单页超过该值时不会截断。")
    prepare_parser.add_argument("--max-pages-per-chunk", type=int, default=6, help="每个 chunk 的最大页数；避免单个 subagent 任务过宽。")
    prepare_parser.add_argument("--overwrite", action="store_true", help="是否覆盖已有 chunk、prompt 和清单。")

    merge_parser = subparsers.add_parser("merge", help="合并 subagent 结果，生成 llm_digest。")
    merge_parser.add_argument("--pipeline-dir", required=True, help="prepare 阶段生成的 digest_pipeline 目录。")
    merge_parser.add_argument("--allow-partial", action="store_true", help="允许部分 chunk 尚未处理时生成不完整 digest。")

    status_parser = subparsers.add_parser("status", help="查看 digest pipeline 的处理进度。")
    status_parser.add_argument("--pipeline-dir", required=True, help="prepare 阶段生成的 digest_pipeline 目录。")

    auto_parser = subparsers.add_parser("auto-digest", help="使用规则抽取为所有 chunk 生成基线 digest JSON。")
    auto_parser.add_argument("--pipeline-dir", required=True, help="prepare 阶段生成的 digest_pipeline 目录。")
    auto_parser.add_argument("--overwrite", action="store_true", help="覆盖已有 agent_results/*.digest.json。")
    return parser


def prepare_pipeline(args: argparse.Namespace) -> None:
    """
    从 content.json 生成 digest chunk 和 subagent prompt。

    参数：
        args: 命令行参数。

    返回值：
        无。
    """
    content_json_path = Path(args.content_json).resolve()
    if not content_json_path.exists():
        raise FileNotFoundError(f"content.json 不存在: {content_json_path}")

    report_dir = content_json_path.parent
    pipeline_dir = Path(args.pipeline_dir).resolve() if args.pipeline_dir else report_dir / "digest_pipeline"
    chunks_dir = pipeline_dir / "chunks"
    prompts_dir = pipeline_dir / "prompts"
    results_dir = pipeline_dir / "agent_results"

    if pipeline_dir.exists() and not args.overwrite and (pipeline_dir / "chunk_manifest.json").exists():
        raise FileExistsError(f"pipeline 已存在，如需重建请加 --overwrite: {pipeline_dir}")

    chunks_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    report = json.loads(content_json_path.read_text(encoding="utf-8"))
    document_metadata = report.get("document_metadata", {})
    pages = report.get("pages", [])
    page_units = [build_page_unit(page) for page in pages]
    grouped_units = group_page_units(page_units, args.max_chars_per_chunk, args.max_pages_per_chunk)

    manifest_items: list[ChunkManifestItem] = []
    for sequence, units in enumerate(grouped_units, start=1):
        chunk_id = f"chunk_{sequence:04d}_p{units[0]['page_number']:03d}_p{units[-1]['page_number']:03d}"
        chunk_text = "\n\n".join(unit["analysis_text"] for unit in units)
        detected_section = detect_section(chunk_text)
        priority_hint, hit_keywords, drop_keywords = score_priority(detected_section, chunk_text)

        chunk_payload = {
            "chunk_id": chunk_id,
            "sequence": sequence,
            "document_metadata": document_metadata,
            "pages": [unit["page_payload"] for unit in units],
            "analysis_text": chunk_text,
            "detected_section": detected_section,
            "rule_priority_hint": priority_hint,
            "hit_keywords": hit_keywords,
            "drop_keywords": drop_keywords,
            "instructions": {
                "goal": "请由 subagent 读取本 chunk，按专业财报分析优先级提取结构化 digest 结果。",
                "priority_order": "S/A/B/C，其中 S 进入最终核心 digest，C 默认丢弃或极限压缩。",
                "schema": DIGEST_RESULT_SCHEMA,
            },
        }

        chunk_path = chunks_dir / f"{chunk_id}.json"
        result_path = results_dir / f"{chunk_id}.digest.json"
        prompt_path = prompts_dir / f"{chunk_id}_prompt.md"
        chunk_path.write_text(json.dumps(chunk_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        prompt_path.write_text(build_subagent_prompt(chunk_path, result_path, chunk_payload), encoding="utf-8")

        manifest_items.append(
            ChunkManifestItem(
                chunk_id=chunk_id,
                sequence=sequence,
                pages=[unit["page_number"] for unit in units],
                char_count=len(chunk_text),
                detected_section=detected_section,
                rule_priority_hint=priority_hint,
                hit_keywords=hit_keywords,
                drop_keywords=drop_keywords,
                chunk_path=relative_to(chunk_path, pipeline_dir),
                prompt_path=relative_to(prompt_path, pipeline_dir),
                result_path=relative_to(result_path, pipeline_dir),
            )
        )

    state = PipelineState(
        content_json_path=str(content_json_path),
        pipeline_dir=str(pipeline_dir),
        report_dir=str(report_dir),
        generated_at=utc_now(),
        document_metadata=document_metadata,
        chunks=manifest_items,
    )
    (pipeline_dir / "chunk_manifest.json").write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    write_agent_batch_plan(pipeline_dir, manifest_items)

    print(f"prepare 完成：共生成 {len(manifest_items)} 个 chunk。")
    print(f"pipeline 目录：{pipeline_dir}")
    print(f"chunk 清单：{pipeline_dir / 'chunk_manifest.json'}")
    print(f"subagent 批量计划：{pipeline_dir / 'agent_batch_plan.md'}")


def build_page_unit(page: dict[str, Any]) -> dict[str, Any]:
    """
    将 content.json 中的单页结果转换为 chunk 构建单元。

    参数：
        page: content.json 的单页字典。

    返回值：
        包含 page_payload 和 analysis_text 的字典。
    """
    page_number = int(page.get("page_number") or 0)
    text = str(page.get("text") or "")
    tables = []
    table_sections: list[str] = []
    for table in page.get("tables", []):
        table_payload = {
            "table_id": table.get("table_id", ""),
            "page_number": table.get("page_number", page_number),
            "markdown": table.get("markdown", ""),
            "bbox": table.get("bbox", []),
        }
        tables.append(table_payload)
        if table_payload["markdown"]:
            table_sections.append(f"### 表格 {table_payload['table_id']}\n{table_payload['markdown']}")

    images = []
    image_sections: list[str] = []
    for image in page.get("images", []):
        image_payload = {
            "image_id": image.get("image_id", ""),
            "page_number": image.get("page_number", page_number),
            "decision": image.get("decision", ""),
            "category": image.get("category", ""),
            "summary": image.get("summary", ""),
            "reason": image.get("reason", ""),
            "image_relative_path": image.get("image_relative_path", ""),
            "nearby_text": image.get("nearby_text", ""),
        }
        images.append(image_payload)
        image_sections.append(
            f"### 图片 {image_payload['image_id']}\n"
            f"decision={image_payload['decision']}；category={image_payload['category']}；"
            f"summary={image_payload['summary']}；reason={image_payload['reason']}"
        )

    warnings = page.get("warnings", [])
    warning_text = "\n".join(f"- {warning}" for warning in warnings)
    analysis_parts = [f"## 第 {page_number} 页", "### 正文", text]
    if table_sections:
        analysis_parts.extend(["### 表格", *table_sections])
    if image_sections:
        analysis_parts.extend(["### 图片", *image_sections])
    if warning_text:
        analysis_parts.extend(["### 解析警告", warning_text])

    return {
        "page_number": page_number,
        "page_payload": {
            "page_number": page_number,
            "text": text,
            "tables": tables,
            "images": images,
            "warnings": warnings,
        },
        "analysis_text": "\n\n".join(part for part in analysis_parts if part),
    }


def group_page_units(page_units: list[dict[str, Any]], max_chars: int, max_pages: int) -> list[list[dict[str, Any]]]:
    """
    按字符数和页数把页面分组为 chunk。

    参数：
        page_units: 页面构建单元列表。
        max_chars: 每个 chunk 的目标最大字符数。
        max_pages: 每个 chunk 的目标最大页数。

    返回值：
        页面单元分组列表。
    """
    groups: list[list[dict[str, Any]]] = []
    current_group: list[dict[str, Any]] = []
    current_chars = 0
    for unit in page_units:
        unit_chars = len(unit["analysis_text"])
        would_exceed_chars = current_group and current_chars + unit_chars > max_chars
        would_exceed_pages = current_group and len(current_group) >= max_pages
        if would_exceed_chars or would_exceed_pages:
            groups.append(current_group)
            current_group = []
            current_chars = 0
        current_group.append(unit)
        current_chars += unit_chars
    if current_group:
        groups.append(current_group)
    return groups


def detect_section(text: str) -> str:
    """
    使用关键词规则识别 chunk 所属财报章节。

    参数：
        text: chunk 可分析文本。

    返回值：
        章节名称，无法识别时返回 unknown。
    """
    for section_name, pattern in SECTION_PATTERNS:
        if pattern.search(text):
            return section_name
    return "unknown"


def score_priority(section: str, text: str) -> tuple[str, list[str], list[str]]:
    """
    使用规则给 chunk 提供初始优先级提示。

    参数：
        section: 识别出的章节名称。
        text: chunk 可分析文本。

    返回值：
        三元组：优先级、升权关键词、降权关键词。
    """
    hit_keywords = [keyword for keyword in S_KEYWORDS + A_KEYWORDS + B_FLAG_KEYWORDS if keyword in text]
    drop_keywords = [keyword for keyword in C_KEYWORDS if keyword in text]
    table_hits = [keyword for keyword in TABLE_HIGH_VALUE_KEYWORDS if keyword in text]
    governance_exception_hits = [keyword for keyword in GOVERNANCE_EXCEPTION_KEYWORDS if keyword in text]
    hit_keywords.extend(keyword for keyword in table_hits + governance_exception_hits if keyword not in hit_keywords)

    if section in GOVERNANCE_SECTIONS:
        if governance_exception_hits:
            return "S", hit_keywords, drop_keywords
        if any(keyword in text for keyword in ["质押", "冻结", "回购", "实际控制人", "控股股东", "内部控制"]):
            return "A", hit_keywords, drop_keywords
        if drop_keywords:
            return "C", hit_keywords, drop_keywords
        return "B", hit_keywords, drop_keywords

    if section in HIGH_PRIORITY_SECTIONS:
        return "S", hit_keywords, drop_keywords
    if table_hits:
        return "S", hit_keywords, drop_keywords
    if any(keyword in text for keyword in S_KEYWORDS):
        return "S", hit_keywords, drop_keywords
    if any(keyword in text for keyword in A_KEYWORDS):
        return "A", hit_keywords, drop_keywords
    if any(keyword in text for keyword in B_FLAG_KEYWORDS):
        return "B", hit_keywords, drop_keywords
    if drop_keywords:
        return "C", hit_keywords, drop_keywords
    return "A", hit_keywords, drop_keywords


def build_subagent_prompt(chunk_path: Path, result_path: Path, chunk_payload: dict[str, Any]) -> str:
    """
    为单个 chunk 生成 subagent 执行提示。

    参数：
        chunk_path: chunk JSON 绝对路径。
        result_path: subagent 结果 JSON 绝对路径。
        chunk_payload: chunk 内容摘要。

    返回值：
        Markdown 提示词。
    """
    schema_text = json.dumps(DIGEST_RESULT_SCHEMA, ensure_ascii=False, indent=2)
    return f"""你是专业财务分析 subagent。你的任务是只处理一个财报 chunk，并把结果写入指定 JSON 文件。

## 输入文件
{chunk_path}

## 输出文件
{result_path}

## 当前 chunk 元数据
- chunk_id: {chunk_payload['chunk_id']}
- 页码: {[page['page_number'] for page in chunk_payload['pages']]}
- 规则识别章节: {chunk_payload['detected_section']}
- 规则优先级提示: {chunk_payload['rule_priority_hint']}
- 升权关键词: {', '.join(chunk_payload['hit_keywords']) if chunk_payload['hit_keywords'] else '无'}
- 降权关键词: {', '.join(chunk_payload['drop_keywords']) if chunk_payload['drop_keywords'] else '无'}

## 工作要求
1. 只读取输入文件，不要读取完整 content.json，也不要读取其他 chunk。
2. 你必须以专业财务分析师视角判断内容属于 S/A/B/C 哪一类。
3. S 级内容必须提取进 key_findings 或 risks；A 级内容摘要；B 级内容抽成 flags；C 级内容写入 discarded_content。
4. 只保留对财务分析、经营分析、风险分析、治理分析有价值的信息。
5. 模板话、页眉页脚、目录、免责声明、无异常问答、无数字无事件的口号式文字，应丢弃并说明原因。
6. 所有数字必须保留原文、单位、期间和来源页码；不要自行编造同比或单位。
7. 所有结论必须带 source_pages。
8. 如果某页没有有价值内容，也要在 source_page_index 中说明该页为何丢弃。
9. 不要在聊天回复中输出长摘要；完成后只简短说明写入了哪个文件。
10. 输出文件必须是合法 JSON，不能包含 Markdown 代码围栏。

## JSON 输出 schema
{schema_text}
"""


def write_agent_batch_plan(pipeline_dir: Path, manifest_items: list[ChunkManifestItem]) -> None:
    """
    写出 subagent 批量执行计划。

    参数：
        pipeline_dir: pipeline 目录。
        manifest_items: chunk 清单。

    返回值：
        无。
    """
    lines = [
        "# Subagent 批量执行计划",
        "",
        "每个 subagent 只处理一个 chunk，读取 prompts 中对应提示，写入 agent_results 中对应 JSON。",
        "主 agent 不需要读取 chunk 正文，只需要按需启动 subagent 并最后运行 merge。",
        "",
    ]
    for item in manifest_items:
        lines.extend(
            [
                f"## {item.chunk_id}",
                f"- 页码: {item.pages}",
                f"- 规则章节: {item.detected_section}",
                f"- 规则优先级: {item.rule_priority_hint}",
                f"- prompt: {item.prompt_path}",
                f"- output: {item.result_path}",
                "",
            ]
        )
    (pipeline_dir / "agent_batch_plan.md").write_text("\n".join(lines), encoding="utf-8")


def merge_pipeline(args: argparse.Namespace) -> None:
    """
    合并 subagent 结果，生成最终 digest 文件。

    参数：
        args: 命令行参数。

    返回值：
        无。
    """
    pipeline_dir = Path(args.pipeline_dir).resolve()
    state = load_pipeline_state(pipeline_dir)
    results: list[dict[str, Any]] = []
    missing_chunks: list[str] = []
    invalid_results: list[dict[str, str]] = []

    for item in state.chunks:
        result_path = pipeline_dir / item.result_path
        if not result_path.exists():
            missing_chunks.append(item.chunk_id)
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            invalid_results.append({"chunk_id": item.chunk_id, "error": f"{exc.__class__.__name__}: {exc}"})
            continue
        result.setdefault("chunk_id", item.chunk_id)
        result.setdefault("pages", item.pages)
        result.setdefault("detected_section", item.detected_section)
        result.setdefault("priority", item.rule_priority_hint)
        results.append(result)

    if (missing_chunks or invalid_results) and not args.allow_partial:
        raise RuntimeError(
            f"存在未完成或无效的 chunk，拒绝生成完整 digest。缺失={len(missing_chunks)}，无效={len(invalid_results)}。"
            "如需生成部分结果，请加 --allow-partial。"
        )

    report_dir = Path(state.report_dir)
    digest_payload = build_digest_payload(state, results, missing_chunks, invalid_results)
    digest_json_path = report_dir / "llm_digest.json"
    digest_md_path = report_dir / "llm_digest.md"
    audit_path = report_dir / "digest_audit.json"

    digest_json_path.write_text(json.dumps(digest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    digest_md_path.write_text(render_digest_markdown(digest_payload), encoding="utf-8")
    audit_path.write_text(json.dumps(build_audit_payload(state, results, missing_chunks, invalid_results), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"merge 完成：已合并 {len(results)} 个 chunk。")
    print(f"缺失 chunk: {len(missing_chunks)}；无效结果: {len(invalid_results)}。")
    print(f"llm_digest.json: {digest_json_path}")
    print(f"llm_digest.md: {digest_md_path}")
    print(f"digest_audit.json: {audit_path}")


def build_digest_payload(
    state: PipelineState,
    results: list[dict[str, Any]],
    missing_chunks: list[str],
    invalid_results: list[dict[str, str]],
) -> dict[str, Any]:
    """
    构建最终 digest JSON。

    参数：
        state: pipeline 状态。
        results: subagent 结果列表。
        missing_chunks: 缺失结果的 chunk_id 列表。
        invalid_results: 无效结果列表。

    返回值：
        digest JSON 字典。
    """
    sorted_results = sorted(
        results,
        key=lambda item: (
            PRIORITY_ORDER.get(str(item.get("priority", "UNKNOWN")), 4),
            min([int(page) for page in item.get("pages", [9999])] or [9999]),
        ),
    )
    return {
        "generated_at": utc_now(),
        "complete": not missing_chunks and not invalid_results,
        "document_metadata": state.document_metadata,
        "source_content_json": state.content_json_path,
        "pipeline_dir": state.pipeline_dir,
        "total_chunks": len(state.chunks),
        "processed_chunks": len(results),
        "missing_chunks": missing_chunks,
        "invalid_results": invalid_results,
        "results": sorted_results,
    }


def render_digest_markdown(digest_payload: dict[str, Any]) -> str:
    """
    将 digest JSON 渲染为面向投研阅读顺序的 Markdown。

    参数：
        digest_payload: 最终 digest JSON。

    返回值：
        Markdown 字符串。
    """
    metadata = digest_payload.get("document_metadata", {})
    title = metadata.get("title") or metadata.get("pdf_stem") or "财报 LLM Digest"
    lines = [f"# {title} LLM Digest", ""]
    lines.extend(
        [
            "## 0. 处理状态与完整性",
            "",
            f"- 生成时间: {digest_payload.get('generated_at', '')}",
            f"- 是否完整: {digest_payload.get('complete', False)}",
            f"- chunk 总数: {digest_payload.get('total_chunks', 0)}",
            f"- 已处理 chunk: {digest_payload.get('processed_chunks', 0)}",
            f"- 缺失 chunk: {len(digest_payload.get('missing_chunks', []))}",
            f"- 无效结果: {len(digest_payload.get('invalid_results', []))}",
            "",
            "## 1. 公司与报告基本信息",
            "",
        ]
    )
    for key in ["stock_code", "company_name", "report_type_label", "report_year", "report_variant", "announcement_id", "published_at", "source_pdf_url"]:
        value = metadata.get(key)
        if value:
            lines.append(f"- {key}: {value}")

    used_finding_ids = render_investment_sections(lines, digest_payload)
    render_unclassified_findings_section(lines, digest_payload, used_finding_ids)
    render_flags_section(lines, digest_payload)
    render_discarded_section(lines, digest_payload)
    render_source_index(lines, digest_payload)

    if digest_payload.get("missing_chunks"):
        lines.extend(["", "## 15. 尚未处理的 chunk", ""])
        lines.extend(f"- {chunk_id}" for chunk_id in digest_payload.get("missing_chunks", []))
    if digest_payload.get("invalid_results"):
        lines.extend(["", "## 16. 无效 subagent 结果", ""])
        for item in digest_payload.get("invalid_results", []):
            lines.append(f"- {item.get('chunk_id', '')}: {item.get('error', '')}")
    lines.append("")
    return "\n".join(lines)


def render_investment_sections(lines: list[str], digest_payload: dict[str, Any]) -> set[str]:
    """
    按投研阅读顺序渲染核心发现。

    参数：
        lines: Markdown 行列表。
        digest_payload: 最终 digest JSON。

    返回值：
        已经渲染过的 finding 稳定编号集合。
    """
    used_finding_ids: set[str] = set()
    for section_id, heading, _keywords in DIGEST_SECTION_RULES:
        lines.extend(["", heading, ""])
        has_content = False
        for result in digest_payload.get("results", []):
            chunk_id = str(result.get("chunk_id", ""))
            for finding_index, finding in enumerate(result.get("key_findings", []) or []):
                finding_id = f"{chunk_id}:finding:{finding_index}"
                if finding_id in used_finding_ids:
                    continue
                if classify_finding_section(finding, result) != section_id:
                    continue
                has_content = True
                used_finding_ids.add(finding_id)
                render_single_finding(lines, result, finding)
        if section_id == "risk_governance":
            has_content = render_risks_inline(lines, digest_payload) or has_content
        if not has_content:
            lines.append("（暂无。）")
    return used_finding_ids


def classify_finding_section(finding: dict[str, Any], result: dict[str, Any]) -> str:
    """
    将 subagent 抽取出的单条发现归入投研 digest 章节。

    参数：
        finding: 单条 key_finding。
        result: 该 finding 所属 chunk 的 digest 结果。

    返回值：
        DIGEST_SECTION_RULES 中定义的 section_id。
    """
    section = str(result.get("detected_section", ""))
    text = "\n".join(
        [
            str(finding.get("topic", "")),
            str(finding.get("summary", "")),
            section,
        ]
    )
    if "审计" in section or any(keyword in text for keyword in ["审计意见", "关键审计事项", "持续经营", "强调事项"]):
        return "audit"
    if any(keyword in text for keyword in ["经营活动", "投资活动", "筹资活动", "现金流", "现金及现金等价物"]):
        return "cashflow"
    if any(keyword in text for keyword in ["风险", "担保", "诉讼", "仲裁", "关联交易", "资金占用", "内控", "处罚", "监管", "质押", "冻结", "无法保证"]):
        return "risk_governance"
    if any(keyword in text for keyword in ["营业收入", "归母净利润", "扣非", "每股收益", "净资产收益率", "主要会计数据", "核心财务"]):
        return "core_metrics"
    if any(keyword in text for keyword in ["毛利", "净利", "营业成本", "费用", "研发", "减值", "非经常性损益", "利润表"]):
        return "income_quality"
    if any(keyword in text for keyword in ["资产", "负债", "应收", "存货", "商誉", "固定资产", "在建工程", "债权投资", "其他债权投资", "投资性房地产", "长期股权投资", "所有者权益", "净资产"]):
        return "balance_sheet"
    if any(keyword in text for keyword in ["利润分配", "分红", "股利", "回购", "融资", "资本开支"]):
        return "capital_allocation"
    if any(keyword in text for keyword in ["主营业务", "商业模式", "行业", "产品", "地区", "客户", "供应商", "产能", "渠道", "收入结构"]):
        return "business"
    if section == "管理层讨论与分析" or any(keyword in text for keyword in ["管理层", "未来", "展望", "战略", "计划", "经营情况"]):
        return "mda"
    return "core_metrics"


def render_single_finding(lines: list[str], result: dict[str, Any], finding: dict[str, Any]) -> None:
    """
    渲染单条财务发现。

    参数：
        lines: Markdown 行列表。
        result: finding 所属 chunk 的 digest 结果。
        finding: 单条 key_finding。

    返回值：
        无。
    """
    lines.append(
        f"- **{finding.get('topic', '未命名主题')}**: {finding.get('summary', '')} "
        f"（chunk={result.get('chunk_id', '')}；优先级={result.get('priority', '')}；页码={finding.get('source_pages') or result.get('pages', [])}）"
    )
    for number in finding.get("numbers", []) or []:
        lines.append(
            "  - 数字: "
            f"{number.get('name', '')}={number.get('value', '')} "
            f"{number.get('unit', '')}；期间={number.get('period', '')}；页码={number.get('source_pages', [])}"
        )


def render_risks_inline(lines: list[str], digest_payload: dict[str, Any]) -> bool:
    """
    在风险治理章节内渲染 risks 数组。

    参数：
        lines: Markdown 行列表。
        digest_payload: 最终 digest JSON。

    返回值：
        是否渲染了至少一条风险。
    """
    has_risks = False
    for result in digest_payload.get("results", []):
        for risk in result.get("risks", []) or []:
            has_risks = True
            lines.append(
                f"- **{risk.get('risk_type', '未分类风险')}**: {risk.get('summary', '')} "
                f"（severity={risk.get('severity', 'unknown')}；chunk={result.get('chunk_id', '')}；页码={risk.get('source_pages', [])}）"
            )
    return has_risks


def render_unclassified_findings_section(lines: list[str], digest_payload: dict[str, Any], used_finding_ids: set[str]) -> None:
    """
    渲染未能归入投研章节但仍被 subagent 保留的发现。

    参数：
        lines: Markdown 行列表。
        digest_payload: 最终 digest JSON。
        used_finding_ids: 已渲染 finding 的稳定编号集合。

    返回值：
        无。
    """
    lines.extend(["", "## 11. 其他保留发现", ""])
    has_content = False
    for result in digest_payload.get("results", []):
        chunk_id = str(result.get("chunk_id", ""))
        for finding_index, finding in enumerate(result.get("key_findings", []) or []):
            finding_id = f"{chunk_id}:finding:{finding_index}"
            if finding_id in used_finding_ids:
                continue
            has_content = True
            render_single_finding(lines, result, finding)
    if not has_content:
        lines.append("（暂无。）")


def render_priority_section(lines: list[str], heading: str, digest_payload: dict[str, Any], priority: str) -> None:
    """
    渲染指定优先级的发现。

    参数：
        lines: Markdown 行列表。
        heading: 章节标题。
        digest_payload: digest JSON。
        priority: 优先级。

    返回值：
        无。
    """
    lines.extend(["", heading, ""])
    matched_results = [result for result in digest_payload.get("results", []) if str(result.get("priority", "")).upper() == priority]
    if not matched_results:
        lines.append("（暂无。）")
        return
    for result in matched_results:
        chunk_id = result.get("chunk_id", "")
        pages = result.get("pages", [])
        section = result.get("detected_section", "")
        reason = result.get("priority_reason", "")
        lines.extend([f"### {chunk_id}（页码: {pages}；章节: {section}）", ""])
        if reason:
            lines.append(f"- 保留原因: {reason}")
        findings = result.get("key_findings", [])
        if not findings:
            lines.append("- 未提取到 key_findings。")
        for finding in findings:
            lines.append(f"- **{finding.get('topic', '未命名主题')}**: {finding.get('summary', '')}")
            if finding.get("source_pages"):
                lines.append(f"  - 来源页码: {finding.get('source_pages')}")
            for number in finding.get("numbers", []):
                lines.append(
                    "  - 数字: "
                    f"{number.get('name', '')}={number.get('value', '')} "
                    f"{number.get('unit', '')}；期间={number.get('period', '')}；页码={number.get('source_pages', [])}"
                )
        lines.append("")


def render_flags_section(lines: list[str], digest_payload: dict[str, Any]) -> None:
    """
    渲染结构化 flags。

    参数：
        lines: Markdown 行列表。
        digest_payload: digest JSON。

    返回值：
        无。
    """
    lines.extend(["", "## 12. B 级结构化状态", ""])
    has_flags = False
    for result in digest_payload.get("results", []):
        flags = result.get("flags") or {}
        if not flags:
            continue
        has_flags = True
        lines.append(f"### {result.get('chunk_id', '')}（页码: {result.get('pages', [])}）")
        for key, value in flags.items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    if not has_flags:
        lines.append("（暂无。）")


def render_risks_section(lines: list[str], digest_payload: dict[str, Any]) -> None:
    """
    渲染风险事项。

    参数：
        lines: Markdown 行列表。
        digest_payload: digest JSON。

    返回值：
        无。
    """
    lines.extend(["", "## 5. 风险、治理与异常事项", ""])
    has_risks = False
    for result in digest_payload.get("results", []):
        for risk in result.get("risks", []) or []:
            has_risks = True
            lines.append(
                f"- **{risk.get('risk_type', '未分类风险')}**: {risk.get('summary', '')} "
                f"（severity={risk.get('severity', 'unknown')}；页码={risk.get('source_pages', [])}）"
            )
    if not has_risks:
        lines.append("（暂无。）")


def render_discarded_section(lines: list[str], digest_payload: dict[str, Any]) -> None:
    """
    渲染被丢弃或极限压缩的内容审计。

    参数：
        lines: Markdown 行列表。
        digest_payload: digest JSON。

    返回值：
        无。
    """
    lines.extend(["", "## 13. 可忽略内容清单", ""])
    has_discarded = False
    for result in digest_payload.get("results", []):
        for item in result.get("discarded_content", []) or []:
            has_discarded = True
            lines.append(
                f"- {item.get('type', 'discarded')}: {item.get('summary', '')}；"
                f"原因={item.get('reason', '')}；页码={item.get('source_pages', [])}"
            )
    if not has_discarded:
        lines.append("（暂无。）")


def render_source_index(lines: list[str], digest_payload: dict[str, Any]) -> None:
    """
    渲染来源页码索引。

    参数：
        lines: Markdown 行列表。
        digest_payload: digest JSON。

    返回值：
        无。
    """
    lines.extend(["", "## 14. 来源页码索引", ""])
    for result in digest_payload.get("results", []):
        chunk_id = result.get("chunk_id", "")
        page_index = result.get("source_page_index", []) or []
        if not page_index:
            lines.append(f"- {chunk_id}: 页码 {result.get('pages', [])}，未提供 source_page_index。")
            continue
        for item in page_index:
            lines.append(f"- {chunk_id} / 第 {item.get('page', '')} 页: {item.get('used_for', '')}")


def build_audit_payload(
    state: PipelineState,
    results: list[dict[str, Any]],
    missing_chunks: list[str],
    invalid_results: list[dict[str, str]],
) -> dict[str, Any]:
    """
    构建 digest 审计文件。

    参数：
        state: pipeline 状态。
        results: subagent 结果。
        missing_chunks: 缺失结果列表。
        invalid_results: 无效结果列表。

    返回值：
        审计 JSON 字典。
    """
    return {
        "generated_at": utc_now(),
        "complete": not missing_chunks and not invalid_results,
        "content_json_path": state.content_json_path,
        "pipeline_dir": state.pipeline_dir,
        "total_chunks": len(state.chunks),
        "processed_chunks": len(results),
        "missing_chunks": missing_chunks,
        "invalid_results": invalid_results,
        "chunk_manifest": [asdict(item) for item in state.chunks],
    }


def show_status(args: argparse.Namespace) -> None:
    """
    打印 pipeline 当前进度。

    参数：
        args: 命令行参数。

    返回值：
        无。
    """
    pipeline_dir = Path(args.pipeline_dir).resolve()
    state = load_pipeline_state(pipeline_dir)
    done = 0
    missing: list[str] = []
    for item in state.chunks:
        if (pipeline_dir / item.result_path).exists():
            done += 1
        else:
            missing.append(item.chunk_id)
    print(f"pipeline: {pipeline_dir}")
    print(f"chunk 总数: {len(state.chunks)}")
    print(f"已完成: {done}")
    print(f"未完成: {len(missing)}")
    if missing:
        print("前 10 个未完成 chunk:")
        for chunk_id in missing[:10]:
            print(f"- {chunk_id}")


def auto_digest_pipeline(args: argparse.Namespace) -> None:
    """
    使用规则抽取为所有 chunk 生成基线 digest JSON。

    参数：
        args: 命令行参数。

    返回值：
        无。
    """
    pipeline_dir = Path(args.pipeline_dir).resolve()
    state = load_pipeline_state(pipeline_dir)
    written_count = 0
    skipped_count = 0
    for item in state.chunks:
        chunk_path = pipeline_dir / item.chunk_path
        result_path = pipeline_dir / item.result_path
        if result_path.exists() and not args.overwrite:
            skipped_count += 1
            continue
        chunk_payload = json.loads(chunk_path.read_text(encoding="utf-8"))
        result = build_rule_digest_result(chunk_payload)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        written_count += 1
    print(f"auto-digest 完成：写入 {written_count} 个，跳过 {skipped_count} 个。")


def build_rule_digest_result(chunk_payload: dict[str, Any]) -> dict[str, Any]:
    """
    从单个 chunk 中抽取基线 digest 结果。

    参数：
        chunk_payload: prepare 阶段生成的 chunk JSON。

    返回值：
        符合 digest schema 的结果字典。
    """
    chunk_id = str(chunk_payload.get("chunk_id", ""))
    pages_payload = chunk_payload.get("pages", []) or []
    pages = [int(page.get("page_number") or 0) for page in pages_payload]
    analysis_text = str(chunk_payload.get("analysis_text", ""))
    detected_section = str(chunk_payload.get("detected_section", "unknown"))
    priority = str(chunk_payload.get("rule_priority_hint", "A"))
    key_findings = extract_rule_key_findings(pages_payload, detected_section)
    risks = extract_rule_risks(pages_payload)
    flags = extract_rule_flags(analysis_text)
    discarded_content = extract_rule_discarded(pages_payload, key_findings, risks)
    return {
        "chunk_id": chunk_id,
        "pages": pages,
        "detected_section": detected_section,
        "priority": priority,
        "keep": bool(key_findings or risks or flags),
        "priority_reason": build_rule_priority_reason(detected_section, priority, key_findings, risks),
        "key_findings": key_findings,
        "risks": risks,
        "flags": flags,
        "discarded_content": discarded_content,
        "source_page_index": build_rule_source_page_index(pages_payload, key_findings, risks),
    }


def extract_rule_key_findings(pages_payload: list[dict[str, Any]], detected_section: str) -> list[dict[str, Any]]:
    """
    基于关键词和表格抽取财报核心发现。

    参数：
        pages_payload: chunk 内页数据。
        detected_section: 规则识别章节。

    返回值：
        key_findings 列表。
    """
    findings: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    topics = {
        "审计意见": ["标准无保留", "非标准", "保留意见", "否定意见", "无法表示意见", "关键审计事项"],
        "核心财务指标": ["营业收入", "归属于上市公司股东的净利润", "扣除非经常性损益", "基本每股收益", "加权平均净资产收益率"],
        "现金流": ["经营活动产生的现金流量净额", "现金及现金等价物", "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额"],
        "资产负债": ["总资产", "净资产", "应收账款", "存货", "商誉", "短期借款", "长期借款", "受限资产"],
        "收入结构": ["主营业务", "分行业", "分产品", "分地区", "毛利率", "客户", "供应商"],
        "资本配置": ["利润分配", "现金红利", "分红", "回购", "资本开支", "在建工程"],
        "研发投入": ["研发投入", "研发人员", "研发费用"],
        "重要附注": ["会计政策变更", "会计估计变更", "差错更正", "非经常性损益", "递延所得税", "政府补助"],
    }
    for page in pages_payload:
        page_number = int(page.get("page_number") or 0)
        page_text = build_rule_page_text(page)
        for topic, keywords in topics.items():
            if topic in seen_topics:
                continue
            matched_keywords = [keyword for keyword in keywords if keyword in page_text]
            if not matched_keywords:
                continue
            summary = summarize_rule_evidence(page_text, matched_keywords)
            numbers = extract_numbers_near_keywords(page_text, matched_keywords, page_number)
            findings.append({"topic": topic, "summary": summary, "numbers": numbers[:12], "source_pages": [page_number]})
            seen_topics.add(topic)
        if detected_section in {"财务报表", "财务报表附注", "管理层讨论与分析"}:
            for table in page.get("tables", []) or []:
                table_text = str(table.get("markdown") or "")
                matched_keywords = [keyword for keyword in S_KEYWORDS + A_KEYWORDS if keyword in table_text]
                if not matched_keywords:
                    continue
                topic = f"表格证据：{matched_keywords[0]}"
                finding_id = f"{topic}:{page_number}"
                if finding_id in seen_topics:
                    continue
                findings.append(
                    {
                        "topic": topic,
                        "summary": summarize_rule_evidence(table_text, matched_keywords),
                        "numbers": extract_numbers_near_keywords(table_text, matched_keywords, page_number)[:10],
                        "source_pages": [page_number],
                    }
                )
                seen_topics.add(finding_id)
                if len(findings) >= 12:
                    break
        if len(findings) >= 16:
            break
    return findings


def extract_rule_risks(pages_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    基于风险关键词抽取风险事项。

    参数：
        pages_payload: chunk 内页数据。

    返回值：
        risks 列表。
    """
    risk_keywords = ["风险", "担保", "诉讼", "仲裁", "资金占用", "关联交易", "行政处罚", "监管", "内控", "质押", "冻结", "逾期", "减值"]
    risks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages_payload:
        page_number = int(page.get("page_number") or 0)
        page_text = build_rule_page_text(page)
        lines = split_rule_lines(page_text)
        for index, line in enumerate(lines):
            matched = [keyword for keyword in risk_keywords if keyword in line]
            if not matched:
                continue
            context = " ".join(lines[max(index - 1, 0) : min(index + 2, len(lines))])
            summary = compact_text(context, 260)
            if summary in seen:
                continue
            seen.add(summary)
            risks.append({"risk_type": matched[0], "summary": summary, "severity": infer_risk_severity(summary), "source_pages": [page_number]})
            if len(risks) >= 8:
                return risks
    return risks


def extract_rule_flags(text: str) -> dict[str, Any]:
    """
    抽取 B 级结构化状态。

    参数：
        text: chunk 文本。

    返回值：
        flags 字典。
    """
    return {
        "mentions_non_standard_audit_opinion": "非标准审计意见" in text or "非标" in text,
        "mentions_fund_occupation": "资金占用" in text,
        "mentions_external_guarantee": "担保" in text,
        "mentions_major_litigation": "诉讼" in text or "仲裁" in text,
        "mentions_related_party_transaction": "关联交易" in text,
        "mentions_profit_distribution": "利润分配" in text or "现金红利" in text or "分红" in text,
        "mentions_internal_control": "内部控制" in text or "内控" in text,
    }


def extract_rule_discarded(
    pages_payload: list[dict[str, Any]],
    key_findings: list[dict[str, Any]],
    risks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    记录低价值页或模板内容。

    参数：
        pages_payload: chunk 内页数据。
        key_findings: 已抽取发现。
        risks: 已抽取风险。

    返回值：
        discarded_content 列表。
    """
    used_pages = {page for finding in key_findings for page in finding.get("source_pages", [])}
    used_pages.update(page for risk in risks for page in risk.get("source_pages", []))
    discarded = []
    for page in pages_payload:
        page_number = int(page.get("page_number") or 0)
        text = build_rule_page_text(page)
        if page_number in used_pages:
            continue
        if any(keyword in text for keyword in ["目录", "备查文件", "前瞻性陈述", "保证年度报告内容的真实性"]):
            discarded.append({"type": "template", "summary": "目录、声明或备查文件等模板内容。", "reason": "未包含具体金额、业务变化或风险事件。", "source_pages": [page_number]})
        elif len(text) < 200:
            discarded.append({"type": "low_value", "summary": "文本较短且未命中核心财报关键词。", "reason": "对财务分析增量有限。", "source_pages": [page_number]})
        if len(discarded) >= 6:
            break
    return discarded


def build_rule_priority_reason(detected_section: str, priority: str, key_findings: list[dict[str, Any]], risks: list[dict[str, Any]]) -> str:
    """
    构建规则抽取优先级说明。

    参数：
        detected_section: 章节名称。
        priority: 优先级。
        key_findings: 抽取出的核心发现。
        risks: 抽取出的风险。

    返回值：
        说明文本。
    """
    topics = [str(item.get("topic", "")) for item in key_findings[:5]]
    risk_types = [str(item.get("risk_type", "")) for item in risks[:3]]
    parts = [f"规则识别章节为{detected_section}，初始优先级为{priority}。"]
    if topics:
        parts.append(f"抽取到核心主题：{'、'.join(topics)}。")
    if risk_types:
        parts.append(f"抽取到风险主题：{'、'.join(risk_types)}。")
    return "".join(parts)


def build_rule_source_page_index(pages_payload: list[dict[str, Any]], key_findings: list[dict[str, Any]], risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    构建页码用途索引。

    参数：
        pages_payload: chunk 内页数据。
        key_findings: 抽取出的核心发现。
        risks: 抽取出的风险。

    返回值：
        页码索引列表。
    """
    page_reasons: dict[int, list[str]] = {}
    for finding in key_findings:
        for page in finding.get("source_pages", []) or []:
            page_reasons.setdefault(int(page), []).append(str(finding.get("topic", "核心发现")))
    for risk in risks:
        for page in risk.get("source_pages", []) or []:
            page_reasons.setdefault(int(page), []).append(str(risk.get("risk_type", "风险事项")))
    index = []
    for page in pages_payload:
        page_number = int(page.get("page_number") or 0)
        used_for = "、".join(page_reasons.get(page_number, [])) or "未命中高价值规则，作为低优先级背景或模板内容。"
        index.append({"page": page_number, "used_for": used_for})
    return index


def build_rule_page_text(page: dict[str, Any]) -> str:
    """
    合并单页正文、表格和图片摘要。

    参数：
        page: 单页 payload。

    返回值：
        合并后的页面文本。
    """
    parts = [str(page.get("text") or "")]
    for table in page.get("tables", []) or []:
        markdown = str(table.get("markdown") or "")
        if markdown:
            parts.append(markdown)
    for image in page.get("images", []) or []:
        if image.get("decision") == "keep":
            parts.append("；".join([str(image.get("summary", "")), str(image.get("nearby_text", ""))]))
    return "\n".join(part for part in parts if part)


def summarize_rule_evidence(text: str, matched_keywords: list[str]) -> str:
    """
    从文本中抽取命中关键词附近的证据摘要。

    参数：
        text: 原始文本。
        matched_keywords: 命中关键词。

    返回值：
        摘要文本。
    """
    lines = split_rule_lines(text)
    selected: list[str] = []
    for index, line in enumerate(lines):
        if any(keyword in line for keyword in matched_keywords):
            selected.extend(lines[max(index - 1, 0) : min(index + 2, len(lines))])
        if len(" ".join(selected)) > 420:
            break
    if not selected:
        selected = lines[:3]
    return compact_text(" ".join(selected), 520)


def extract_numbers_near_keywords(text: str, matched_keywords: list[str], page_number: int) -> list[dict[str, Any]]:
    """
    抽取关键词附近数字。

    参数：
        text: 原始文本。
        matched_keywords: 命中关键词。
        page_number: 来源页码。

    返回值：
        数字对象列表。
    """
    numbers: list[dict[str, Any]] = []
    lines = split_rule_lines(text)
    seen_values: set[str] = set()
    for index, line in enumerate(lines):
        if not any(keyword in line for keyword in matched_keywords):
            continue
        context = " ".join(lines[max(index - 1, 0) : min(index + 2, len(lines))])
        for value in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?%?", context):
            if value in seen_values:
                continue
            seen_values.add(value)
            numbers.append({"name": infer_number_name(context, matched_keywords), "value": value, "period": infer_period(context), "unit": infer_unit(context, value), "source_pages": [page_number]})
            if len(numbers) >= 16:
                return numbers
    return numbers


def infer_number_name(context: str, matched_keywords: list[str]) -> str:
    """
    推断数字名称。

    参数：
        context: 数字上下文。
        matched_keywords: 命中关键词。

    返回值：
        指标名称。
    """
    for keyword in matched_keywords:
        if keyword in context:
            return keyword
    return "相关数字"


def infer_period(context: str) -> str:
    """
    推断数字期间。

    参数：
        context: 数字上下文。

    返回值：
        期间文本。
    """
    match = re.search(r"20\d{2}\s*年(?:度|末|1—12月)?", context)
    return match.group(0).replace(" ", "") if match else "原文附近未明确期间"


def infer_unit(context: str, value: str) -> str:
    """
    推断数字单位。

    参数：
        context: 数字上下文。
        value: 数字原文。

    返回值：
        单位文本。
    """
    if value.endswith("%") or "百分" in context or "比率" in context:
        return "%"
    if "万元" in context:
        return "万元"
    if "元" in context:
        return "元"
    if "股" in context:
        return "股"
    return "原文未明确单位"


def infer_risk_severity(summary: str) -> str:
    """
    推断风险严重度。

    参数：
        summary: 风险摘要。

    返回值：
        high、medium、low 或 unknown。
    """
    if any(keyword in summary for keyword in ["重大", "违规", "处罚", "逾期", "冻结", "无法表示", "否定意见", "保留意见"]):
        return "high"
    if any(keyword in summary for keyword in ["担保", "诉讼", "仲裁", "减值", "资金占用", "关联交易"]):
        return "medium"
    return "low"


def split_rule_lines(text: str) -> list[str]:
    """
    切分并清洗文本行。

    参数：
        text: 原始文本。

    返回值：
        清洗后的文本行。
    """
    lines = [compact_text(line, 600) for line in re.split(r"[\n\r]+", text)]
    return [line for line in lines if len(line) >= 4]


def compact_text(text: str, max_length: int) -> str:
    """
    压缩空白并限制长度。

    参数：
        text: 原始文本。
        max_length: 最大长度。

    返回值：
        压缩后的文本。
    """
    compacted = re.sub(r"\s+", " ", str(text)).strip()
    return compacted[:max_length]


def load_pipeline_state(pipeline_dir: Path) -> PipelineState:
    """
    读取 pipeline 状态。

    参数：
        pipeline_dir: pipeline 目录。

    返回值：
        PipelineState 实例。
    """
    manifest_path = pipeline_dir / "chunk_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"chunk_manifest.json 不存在: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks = [ChunkManifestItem(**item) for item in payload.get("chunks", [])]
    return PipelineState(
        content_json_path=payload.get("content_json_path", ""),
        pipeline_dir=payload.get("pipeline_dir", str(pipeline_dir)),
        report_dir=payload.get("report_dir", ""),
        generated_at=payload.get("generated_at", ""),
        document_metadata=payload.get("document_metadata", {}),
        chunks=chunks,
    )


def configure_stdout_encoding() -> None:
    """
    将标准输出切换为 UTF-8，避免 Windows Bash 捕获中文时出现乱码。

    参数：
        无。

    返回值：
        无。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def relative_to(path: Path, root: Path) -> str:
    """
    将路径转换为相对路径。

    参数：
        path: 目标路径。
        root: 根目录。

    返回值：
        相对路径字符串。
    """
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def utc_now() -> str:
    """
    获取当前 UTC 时间。

    参数：
        无。

    返回值：
        ISO 8601 时间字符串。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
