"""
财报 PDF 信息处理核心模块。

该模块是“信息处理员”的第一阶段能力实现，目标是把信息收集员下载到本地的财报 PDF，
转换成 LLM 与普通代码都能稳定消费的结构化文本数据。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover - 依赖缺失时走 pypdf 降级链路。
    fitz = None  # type: ignore

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # pragma: no cover - 只有在 PyMuPDF 不可用时才需要该降级依赖。
    PdfReader = None  # type: ignore


DEFAULT_PROCESSOR_WORKSPACE = Path(__file__).resolve().parent / "processor_workspace"
PROCESSING_JSON_MANIFEST_NAME = "pdf_processing_manifest.json"
PROCESSING_CSV_MANIFEST_NAME = "pdf_processing_manifest.csv"

# 这些词通常意味着图片周边文字在描述图表、架构或业务流程，图片更可能承载有效财报信息。
INFORMATIVE_IMAGE_KEYWORDS = [
    "图",
    "表",
    "趋势",
    "结构",
    "占比",
    "变动",
    "变化",
    "收入",
    "利润",
    "成本",
    "费用",
    "资产",
    "负债",
    "现金流",
    "研发",
    "产能",
    "销量",
    "业务",
    "流程",
    "架构",
    "分布",
    "示意",
    "项目",
    "客户",
    "供应商",
    "区域",
    "行业",
]

# 这些词通常出现在签章页、封面装饰或页眉页脚附近，不直接提供财务分析所需信息。
DECORATIVE_IMAGE_KEYWORDS = [
    "签字",
    "签名",
    "盖章",
    "印章",
    "公章",
    "法定代表人",
    "负责人",
    "声明",
    "承诺",
    "二维码",
    "网址",
    "证券代码",
]

WHITESPACE_PATTERN = re.compile(r"[ \t　]+")
SAFE_FILENAME_PATTERN = re.compile(r"[\\/:*?\"<>|]+")


@dataclass
class ParsedTable:
    """
    单个 PDF 表格的结构化结果。

    参数：
        table_id: 表格在当前文档中的稳定编号，例如 p001_t01。
        page_number: 表格所在页码，从 1 开始。
        bbox: 表格在页面中的坐标，单位为 PDF 点。
        rows: 表格二维数组，每个元素都是已经清洗过的字符串。
        csv_relative_path: 表格 CSV 文件相对处理工作区的路径。
        markdown: 适合直接送入 LLM 的 Markdown 表格片段。
    返回值：
        dataclass 实例，无额外返回值。
    """

    table_id: str
    page_number: int
    bbox: list[float] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    csv_relative_path: str = ""
    markdown: str = ""


@dataclass
class ParsedImage:
    """
    单个 PDF 图片的结构化结果。

    参数：
        image_id: 图片在当前文档中的稳定编号，例如 p001_img01。
        page_number: 图片所在页码，从 1 开始。
        bbox: 图片在页面中的坐标，单位为 PDF 点；如果底层库无法获取则为空。
        width: 图片原始像素宽度。
        height: 图片原始像素高度。
        area_ratio: 图片显示面积占整页面积的比例，用于区分正文图表和装饰小图。
        digest: 图片内容哈希或底层库返回的图片摘要，用于识别重复图片。
        decision: keep 或 discard。
        category: 基于尺寸、位置和周边文字推断的图片类别。
        summary: 给 LLM 使用的中文图片说明。
        reason: 保留或丢弃的原因。
        image_relative_path: 保留图片导出后的相对路径；丢弃图片为空。
        nearby_text: 图片周边可见文字，用于后续人工或视觉模型复核。
    返回值：
        dataclass 实例，无额外返回值。
    """

    image_id: str
    page_number: int
    bbox: list[float] = field(default_factory=list)
    width: int = 0
    height: int = 0
    area_ratio: float = 0.0
    digest: str = ""
    decision: str = "discard"
    category: str = "unknown"
    summary: str = ""
    reason: str = ""
    image_relative_path: str = ""
    nearby_text: str = ""


@dataclass
class ParsedPage:
    """
    单页 PDF 的结构化结果。

    参数：
        page_number: 页码，从 1 开始。
        text: 页面正文文本，保留原始阅读顺序并做轻量空白清洗。
        tables: 当前页识别出的表格列表。
        images: 当前页识别出的图片列表，包含保留与丢弃两类。
        warnings: 当前页解析过程中出现的非致命问题。
    返回值：
        dataclass 实例，无额外返回值。
    """

    page_number: int
    text: str = ""
    tables: list[ParsedTable] = field(default_factory=list)
    images: list[ParsedImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProcessedReport:
    """
    单份财报 PDF 的完整处理结果。

    参数：
        source_pdf_path: 原始 PDF 的绝对路径。
        pdf_sha256: PDF 文件内容哈希，用于判断同一文件是否重复处理。
        processed_at: 处理完成时间，ISO 8601 UTC 格式。
        parser_engine: 本次实际使用的解析引擎。
        document_metadata: PDF 自带元数据与信息收集员传入的业务元数据。
        page_count: PDF 页数。
        pages: 逐页解析结果。
        outputs: 本次生成的 JSON、Markdown、TXT 等输出路径。
        warnings: 文档级解析警告。
    返回值：
        dataclass 实例，无额外返回值。
    """

    source_pdf_path: str
    pdf_sha256: str
    processed_at: str
    parser_engine: str
    document_metadata: dict[str, Any]
    page_count: int
    pages: list[ParsedPage] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class FinancialReportPdfProcessor:
    """
    财报 PDF 解析器。

    参数：
        workspace: 信息处理员工作区，所有解析结果、图片和处理清单都会落在这里。
        save_images: 是否导出被判定为有效信息的图片区域。
        export_table_csv: 是否额外导出表格 CSV；默认关闭，因为 JSON 和 Markdown 已经保存表格信息。
        min_image_area_ratio: 图片显示面积占整页面积的最低阈值，低于该值且没有有效周边文字时会被丢弃。

    返回值：
        初始化后的处理器实例。
    """

    def __init__(
        self,
        workspace: str | Path = DEFAULT_PROCESSOR_WORKSPACE,
        *,
        save_images: bool = True,
        export_table_csv: bool = False,
        min_image_area_ratio: float = 0.006,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.parsed_dir = self.workspace / "parsed_reports"
        self.manifest_dir = self.workspace / "manifests"
        self.save_images = save_images
        self.export_table_csv = export_table_csv
        self.min_image_area_ratio = min_image_area_ratio
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """
        初始化处理员所需目录。

        为什么在构造函数中统一创建目录：
        PDF 处理会同时产生正文、图片和总清单；如果目录分散创建，失败时很难判断是哪类产物没有落盘。

        参数：
            无。
        返回值：
            无。
        """
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

    def process_pdf(
        self,
        pdf_path: str | Path,
        *,
        source_record: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> ProcessedReport:
        """
        处理单份财报 PDF，并写出 JSON、Markdown、TXT 和图片文件；表格 CSV 默认不导出。

        参数：
            pdf_path: 待处理 PDF 路径。
            source_record: 信息收集员 manifest 中对应的记录；传入后会写入业务元数据并用于规范输出目录。
            overwrite: 如果已经存在解析结果，是否重新解析并覆盖核心输出文件。

        返回值：
            ProcessedReport，包含所有解析结果和输出路径。
        """
        resolved_pdf_path = Path(pdf_path).resolve()
        if not resolved_pdf_path.exists():
            raise FileNotFoundError(f"PDF 文件不存在: {resolved_pdf_path}")

        output_dir = self._build_output_dir(resolved_pdf_path, source_record)
        json_output_path = output_dir / "content.json"
        if json_output_path.exists() and not overwrite:
            existing_payload = json.loads(json_output_path.read_text(encoding="utf-8"))
            return self._processed_report_from_dict(existing_payload)

        output_dir.mkdir(parents=True, exist_ok=True)
        if self.export_table_csv:
            (output_dir / "tables").mkdir(exist_ok=True)
        (output_dir / "images").mkdir(exist_ok=True)

        pdf_sha256 = self._sha256_file(resolved_pdf_path)
        if fitz is not None:
            report = self._process_with_pymupdf(resolved_pdf_path, output_dir, pdf_sha256, source_record)
        elif PdfReader is not None:
            report = self._process_with_pypdf(resolved_pdf_path, output_dir, pdf_sha256, source_record)
        else:
            raise RuntimeError(
                "当前 Python 环境缺少 PDF 解析依赖。请安装 PyMuPDF，或至少安装 pypdf 后再运行。"
            )

        self._write_report_outputs(report, output_dir)
        self._update_processing_manifest(report)
        return report

    def _process_with_pymupdf(
        self,
        pdf_path: Path,
        output_dir: Path,
        pdf_sha256: str,
        source_record: dict[str, Any] | None,
    ) -> ProcessedReport:
        """
        使用 PyMuPDF 解析 PDF。

        为什么优先使用 PyMuPDF：
        它可以同时读取文本、表格候选和图片位置，适合第一阶段把复杂财报 PDF 转成统一结构化数据。

        参数：
            pdf_path: PDF 绝对路径。
            output_dir: 当前 PDF 的输出目录。
            pdf_sha256: PDF 文件哈希。
            source_record: 信息收集员记录。

        返回值：
            ProcessedReport。
        """
        pages: list[ParsedPage] = []
        document_warnings: list[str] = []
        image_digest_seen_count: dict[str, int] = {}

        with fitz.open(pdf_path) as document:  # type: ignore[union-attr]
            document_metadata = self._build_document_metadata(
                pdf_path=pdf_path,
                source_record=source_record,
                pdf_metadata=dict(document.metadata or {}),
            )
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                page_number = page_index + 1
                page_warnings: list[str] = []

                text = self._extract_page_text(page, page_warnings)
                text_blocks = self._extract_text_blocks(page, page_warnings)
                tables = self._extract_page_tables(page, page_number, output_dir, page_warnings)
                images = self._extract_page_images(
                    document=document,
                    page=page,
                    page_number=page_number,
                    output_dir=output_dir,
                    text_blocks=text_blocks,
                    digest_seen_count=image_digest_seen_count,
                    page_warnings=page_warnings,
                )

                pages.append(
                    ParsedPage(
                        page_number=page_number,
                        text=text,
                        tables=tables,
                        images=images,
                        warnings=page_warnings,
                    )
                )

            return ProcessedReport(
                source_pdf_path=str(pdf_path),
                pdf_sha256=pdf_sha256,
                processed_at=self._utc_now(),
                parser_engine="pymupdf",
                document_metadata=document_metadata,
                page_count=document.page_count,
                pages=pages,
                warnings=document_warnings,
            )

    def _process_with_pypdf(
        self,
        pdf_path: Path,
        output_dir: Path,
        pdf_sha256: str,
        source_record: dict[str, Any] | None,
    ) -> ProcessedReport:
        """
        使用 pypdf 作为文本解析降级方案。

        为什么需要降级方案：
        有些环境可能没有 PyMuPDF；此时仍然应该尽量提取正文文本，但必须明确标注表格和图片不可用。

        参数：
            pdf_path: PDF 绝对路径。
            output_dir: 当前 PDF 的输出目录，当前降级流程不直接使用但保留接口一致性。
            pdf_sha256: PDF 文件哈希。
            source_record: 信息收集员记录。

        返回值：
            ProcessedReport。
        """
        _ = output_dir
        reader = PdfReader(str(pdf_path))  # type: ignore[operator]
        pages: list[ParsedPage] = []
        for page_index, page in enumerate(reader.pages):
            text = self._normalize_text(page.extract_text() or "")
            pages.append(
                ParsedPage(
                    page_number=page_index + 1,
                    text=text,
                    warnings=["当前使用 pypdf 降级解析，仅提取正文文本，未抽取表格和图片。"],
                )
            )

        metadata = {}
        if getattr(reader, "metadata", None):
            metadata = {str(key): str(value) for key, value in dict(reader.metadata).items()}

        return ProcessedReport(
            source_pdf_path=str(pdf_path),
            pdf_sha256=pdf_sha256,
            processed_at=self._utc_now(),
            parser_engine="pypdf_text_only",
            document_metadata=self._build_document_metadata(
                pdf_path=pdf_path,
                source_record=source_record,
                pdf_metadata=metadata,
            ),
            page_count=len(reader.pages),
            pages=pages,
            warnings=["当前环境没有 PyMuPDF，表格和图片处理能力已降级。"],
        )

    def _extract_page_text(self, page: Any, warnings: list[str]) -> str:
        """
        提取单页正文文本。

        参数：
            page: PyMuPDF 页面对象。
            warnings: 当前页警告列表，发生非致命异常时会追加说明。

        返回值：
            清洗后的页面文本。
        """
        try:
            return self._normalize_text(page.get_text("text", sort=True) or "")
        except Exception as exc:
            warnings.append(f"正文提取失败: {exc.__class__.__name__}: {exc}")
            return ""

    def _extract_text_blocks(self, page: Any, warnings: list[str]) -> list[dict[str, Any]]:
        """
        提取文字块，用于给图片生成上下文摘要。

        参数：
            page: PyMuPDF 页面对象。
            warnings: 当前页警告列表。

        返回值：
            包含 bbox 与 text 的文字块列表。
        """
        try:
            page_dict = page.get_text("dict", sort=True)
        except TypeError:
            page_dict = page.get_text("dict")
        except Exception as exc:
            warnings.append(f"文字块提取失败: {exc.__class__.__name__}: {exc}")
            return []

        text_blocks: list[dict[str, Any]] = []
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            fragments: list[str] = []
            for line in block.get("lines", []):
                line_text = "".join(span.get("text", "") for span in line.get("spans", []))
                if line_text.strip():
                    fragments.append(line_text.strip())
            text = self._normalize_text("\n".join(fragments))
            if text:
                text_blocks.append({"bbox": list(block.get("bbox", [])), "text": text})
        return text_blocks

    def _extract_page_tables(
        self,
        page: Any,
        page_number: int,
        output_dir: Path,
        warnings: list[str],
    ) -> list[ParsedTable]:
        """
        提取单页表格，并在显式开启时写出 CSV。

        参数：
            page: PyMuPDF 页面对象。
            page_number: 当前页码。
            output_dir: 当前 PDF 的输出目录。
            warnings: 当前页警告列表。

        返回值：
            当前页识别出的表格列表。
        """
        if not hasattr(page, "find_tables"):
            warnings.append("当前 PyMuPDF 版本不支持 find_tables，未执行表格识别。")
            return []

        try:
            table_finder = page.find_tables()
        except Exception as exc:
            warnings.append(f"表格识别失败: {exc.__class__.__name__}: {exc}")
            return []

        parsed_tables: list[ParsedTable] = []
        for table_index, table in enumerate(getattr(table_finder, "tables", []), start=1):
            table_id = f"p{page_number:03d}_t{table_index:02d}"
            try:
                rows = self._normalize_table_rows(table.extract())
            except Exception as exc:
                warnings.append(f"表格 {table_id} 提取失败: {exc.__class__.__name__}: {exc}")
                continue

            if not rows:
                continue

            csv_relative_path = ""
            if self.export_table_csv:
                csv_path = output_dir / "tables" / f"{table_id}.csv"
                self._write_table_csv(rows, csv_path)
                csv_relative_path = self._relative_to_workspace(csv_path)
            parsed_tables.append(
                ParsedTable(
                    table_id=table_id,
                    page_number=page_number,
                    bbox=[float(value) for value in getattr(table, "bbox", [])],
                    rows=rows,
                    csv_relative_path=csv_relative_path,
                    markdown=self._table_to_markdown(rows),
                )
            )
        return parsed_tables

    def _extract_page_images(
        self,
        *,
        document: Any,
        page: Any,
        page_number: int,
        output_dir: Path,
        text_blocks: list[dict[str, Any]],
        digest_seen_count: dict[str, int],
        page_warnings: list[str],
    ) -> list[ParsedImage]:
        """
        提取单页图片，并判断保留或丢弃。

        参数：
            document: PyMuPDF 文档对象。
            page: PyMuPDF 页面对象。
            page_number: 当前页码。
            output_dir: 当前 PDF 的输出目录。
            text_blocks: 当前页文字块，用于生成图片上下文摘要。
            digest_seen_count: 全文档图片摘要计数，用于识别重复装饰图片。
            page_warnings: 当前页警告列表。

        返回值：
            当前页图片解析结果列表。
        """
        try:
            image_infos = page.get_image_info(hashes=True, xrefs=True)
        except Exception as exc:
            page_warnings.append(f"图片信息读取失败: {exc.__class__.__name__}: {exc}")
            return self._extract_page_images_without_bbox(document, page, page_number, output_dir, page_warnings)

        parsed_images: list[ParsedImage] = []
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        for image_index, image_info in enumerate(image_infos, start=1):
            image_id = f"p{page_number:03d}_img{image_index:02d}"
            bbox = [float(value) for value in image_info.get("bbox", [])]
            width = int(image_info.get("width") or 0)
            height = int(image_info.get("height") or 0)
            xref = int(image_info.get("xref") or 0)
            digest = self._normalize_digest(image_info.get("digest") or f"{page_number}-{image_index}-{xref}")
            digest_seen_count[digest] = digest_seen_count.get(digest, 0) + 1

            area_ratio = self._calculate_area_ratio(bbox, page_area)
            nearby_text = self._find_nearby_text(bbox, text_blocks)
            decision, category, summary, reason = self._classify_image(
                page_number=page_number,
                width=width,
                height=height,
                area_ratio=area_ratio,
                nearby_text=nearby_text,
                repeat_count=digest_seen_count[digest],
            )

            image_relative_path = ""
            if decision == "keep" and self.save_images:
                image_path = self._save_image_crop(
                    document=document,
                    page=page,
                    xref=xref,
                    bbox=bbox,
                    image_id=image_id,
                    output_dir=output_dir,
                    warnings=page_warnings,
                )
                if image_path:
                    image_relative_path = self._relative_to_workspace(image_path)

            parsed_images.append(
                ParsedImage(
                    image_id=image_id,
                    page_number=page_number,
                    bbox=bbox,
                    width=width,
                    height=height,
                    area_ratio=round(area_ratio, 6),
                    digest=digest,
                    decision=decision,
                    category=category,
                    summary=summary,
                    reason=reason,
                    image_relative_path=image_relative_path,
                    nearby_text=nearby_text,
                )
            )
        return parsed_images

    def _extract_page_images_without_bbox(
        self,
        document: Any,
        page: Any,
        page_number: int,
        output_dir: Path,
        warnings: list[str],
    ) -> list[ParsedImage]:
        """
        在无法获取图片坐标时降级提取图片清单。

        参数：
            document: PyMuPDF 文档对象。
            page: PyMuPDF 页面对象。
            page_number: 当前页码。
            output_dir: 当前 PDF 的输出目录。
            warnings: 当前页警告列表。

        返回值：
            仅包含尺寸和导出路径的图片列表。
        """
        parsed_images: list[ParsedImage] = []
        try:
            raw_images = page.get_images(full=True)
        except Exception as exc:
            warnings.append(f"图片降级读取失败: {exc.__class__.__name__}: {exc}")
            return []

        for image_index, raw_image in enumerate(raw_images, start=1):
            image_id = f"p{page_number:03d}_img{image_index:02d}"
            xref = int(raw_image[0])
            width = int(raw_image[2] or 0)
            height = int(raw_image[3] or 0)
            decision = "keep" if width >= 300 and height >= 120 else "discard"
            reason = "无法获取页面坐标，但图片像素较大，先保留待后续视觉复核。" if decision == "keep" else "无法获取页面坐标且图片像素较小，按装饰图丢弃。"
            image_relative_path = ""
            if decision == "keep" and self.save_images:
                image_path = self._save_image_crop(
                    document=document,
                    page=page,
                    xref=xref,
                    bbox=[],
                    image_id=image_id,
                    output_dir=output_dir,
                    warnings=warnings,
                )
                if image_path:
                    image_relative_path = self._relative_to_workspace(image_path)
            parsed_images.append(
                ParsedImage(
                    image_id=image_id,
                    page_number=page_number,
                    width=width,
                    height=height,
                    decision=decision,
                    category="unknown_no_bbox",
                    summary=f"第 {page_number} 页图片，底层解析库未返回页面坐标，需要后续视觉模型复核。",
                    reason=reason,
                    image_relative_path=image_relative_path,
                )
            )
        return parsed_images

    def _classify_image(
        self,
        *,
        page_number: int,
        width: int,
        height: int,
        area_ratio: float,
        nearby_text: str,
        repeat_count: int,
    ) -> tuple[str, str, str, str]:
        """
        判断图片是否需要保留，并生成中文摘要。

        为什么先用启发式规则而不是直接依赖视觉模型：
        第一阶段要保证离线、可复现、低依赖；启发式规则可以先过滤 logo、页眉页脚、签章等噪音，
        后续再把保留下来的图片交给多模态 LLM 做更细摘要。

        参数：
            page_number: 当前页码。
            width: 图片像素宽度。
            height: 图片像素高度。
            area_ratio: 图片显示面积占整页面积比例。
            nearby_text: 图片周边文字。
            repeat_count: 同一图片摘要在文档中出现的次数。

        返回值：
            四元组：decision、category、summary、reason。
        """
        compact_nearby_text = self._single_line(nearby_text)
        has_informative_context = any(keyword in compact_nearby_text for keyword in INFORMATIVE_IMAGE_KEYWORDS)
        has_decorative_context = any(keyword in compact_nearby_text for keyword in DECORATIVE_IMAGE_KEYWORDS)
        pixel_area = width * height

        if has_decorative_context and area_ratio < 0.08:
            return (
                "discard",
                "signature_or_decorative",
                f"第 {page_number} 页图片疑似签章、二维码、页眉页脚或其他装饰性元素。",
                "周边文字命中签章/声明/二维码等弱信息关键词，且图片面积不大。",
            )

        if repeat_count >= 3 and area_ratio < 0.03:
            return (
                "discard",
                "repeated_decoration",
                f"第 {page_number} 页图片疑似跨页重复装饰元素。",
                "同一图片摘要在文档中多次出现，且显示面积较小。",
            )

        if area_ratio < self.min_image_area_ratio and pixel_area < 50_000 and not has_informative_context:
            return (
                "discard",
                "tiny_decoration",
                f"第 {page_number} 页图片尺寸较小，未发现有效图表上下文。",
                "图片面积低于阈值，且周边文字没有财务指标、图表或业务说明。",
            )

        if has_informative_context:
            category = "chart_or_business_diagram"
            summary = f"第 {page_number} 页图片疑似财务图表、业务结构图或流程图；周边文字：{compact_nearby_text[:240]}"
            reason = "周边文字包含财务指标、图表、业务流程或结构类关键词。"
            return "keep", category, summary, reason

        if area_ratio >= 0.04 or pixel_area >= 180_000:
            return (
                "keep",
                "large_visual_element",
                f"第 {page_number} 页存在较大图片，当前未能从周边文字确定内容，需要后续视觉模型复核。",
                "图片面积较大，直接丢弃可能遗漏图表、产品照片或业务示意图。",
            )

        return (
            "discard",
            "low_information_image",
            f"第 {page_number} 页图片暂判为低信息量图片。",
            "图片面积不大，周边没有明确财报分析相关文字。",
        )

    def _save_image_crop(
        self,
        *,
        document: Any,
        page: Any,
        xref: int,
        bbox: list[float],
        image_id: str,
        output_dir: Path,
        warnings: list[str],
    ) -> Path | None:
        """
        保存有效图片或图片所在页面区域。

        参数：
            document: PyMuPDF 文档对象。
            page: PyMuPDF 页面对象。
            xref: 图片对象编号。
            bbox: 图片页面坐标。
            image_id: 图片编号。
            output_dir: 当前 PDF 的输出目录。
            warnings: 当前页警告列表。

        返回值：
            成功保存时返回图片路径，失败时返回 None。
        """
        image_dir = output_dir / "images"
        if bbox and len(bbox) == 4:
            try:
                clip = fitz.Rect(bbox)  # type: ignore[union-attr]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)  # type: ignore[union-attr]
                image_path = image_dir / f"{image_id}.png"
                pixmap.save(str(image_path))
                return image_path
            except Exception as exc:
                warnings.append(f"图片 {image_id} 按页面区域导出失败，尝试按 xref 导出: {exc.__class__.__name__}: {exc}")

        if xref <= 0:
            warnings.append(f"图片 {image_id} 缺少有效 xref，无法导出原始图片。")
            return None

        try:
            extracted = document.extract_image(xref)
            image_bytes = extracted.get("image", b"")
            image_ext = extracted.get("ext", "png") or "png"
            image_path = image_dir / f"{image_id}.{image_ext}"
            image_path.write_bytes(image_bytes)
            return image_path
        except Exception as exc:
            warnings.append(f"图片 {image_id} 按 xref 导出失败: {exc.__class__.__name__}: {exc}")
            return None

    def _write_report_outputs(self, report: ProcessedReport, output_dir: Path) -> None:
        """
        写出单份财报的 JSON、Markdown 和纯文本结果。

        参数：
            report: 已完成解析的财报结果。
            output_dir: 当前 PDF 的输出目录。

        返回值：
            无。
        """
        json_path = output_dir / "content.json"
        markdown_path = output_dir / "content.md"
        text_path = output_dir / "content.txt"

        report.outputs = {
            "json": self._relative_to_workspace(json_path),
            "markdown": self._relative_to_workspace(markdown_path),
            "text": self._relative_to_workspace(text_path),
        }

        json_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(self._render_markdown(report), encoding="utf-8")
        text_path.write_text(self._render_plain_text(report), encoding="utf-8")

    def _render_markdown(self, report: ProcessedReport) -> str:
        """
        将结构化结果渲染为适合 LLM 阅读的 Markdown。

        参数：
            report: 财报处理结果。

        返回值：
            Markdown 字符串。
        """
        metadata = report.document_metadata
        title = metadata.get("title") or metadata.get("pdf_stem") or "财报解析结果"
        lines: list[str] = [f"# {title}", ""]
        lines.extend(
            [
                "## 文档元数据",
                "",
                f"- 原始 PDF: {report.source_pdf_path}",
                f"- PDF SHA256: {report.pdf_sha256}",
                f"- 解析时间: {report.processed_at}",
                f"- 解析引擎: {report.parser_engine}",
                f"- 页数: {report.page_count}",
            ]
        )
        for key in ["stock_code", "company_name", "report_type", "report_type_label", "report_year", "report_variant", "announcement_id", "published_at", "source_pdf_url"]:
            value = metadata.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        if report.warnings:
            lines.extend(["", "## 文档级警告", ""])
            lines.extend(f"- {warning}" for warning in report.warnings)

        for page in report.pages:
            lines.extend(["", f"## 第 {page.page_number} 页", ""])
            if page.warnings:
                lines.extend(["### 解析警告", ""])
                lines.extend(f"- {warning}" for warning in page.warnings)
                lines.append("")
            lines.extend(["### 正文", ""])
            lines.append(page.text if page.text else "（本页未提取到可读正文。）")

            if page.tables:
                lines.extend(["", "### 表格", ""])
                for table in page.tables:
                    lines.extend([f"#### {table.table_id}", ""])
                    lines.append(f"- CSV: {table.csv_relative_path}")
                    lines.append("")
                    lines.append(table.markdown or "（该表格无法渲染为 Markdown。）")
                    lines.append("")

            kept_images = [image for image in page.images if image.decision == "keep"]
            discarded_images = [image for image in page.images if image.decision == "discard"]
            if kept_images or discarded_images:
                lines.extend(["", "### 图片处理", ""])
                for image in kept_images:
                    lines.append(f"- 保留 {image.image_id}: {image.summary}")
                    lines.append(f"  - 类别: {image.category}")
                    lines.append(f"  - 原因: {image.reason}")
                    if image.image_relative_path:
                        lines.append(f"  - 图片路径: {image.image_relative_path}")
                for image in discarded_images:
                    lines.append(f"- 丢弃 {image.image_id}: {image.reason}")
        lines.append("")
        return "\n".join(lines)

    def _render_plain_text(self, report: ProcessedReport) -> str:
        """
        将结构化结果渲染为纯文本。

        参数：
            report: 财报处理结果。

        返回值：
            纯文本字符串。
        """
        sections: list[str] = []
        for page in report.pages:
            sections.append(f"===== 第 {page.page_number} 页 =====")
            sections.append(page.text if page.text else "（本页未提取到可读正文。）")
            for table in page.tables:
                sections.append(f"\n[表格 {table.table_id}]")
                sections.append(table.markdown)
            for image in page.images:
                if image.decision == "keep":
                    sections.append(f"\n[图片 {image.image_id}] {image.summary}")
        return "\n\n".join(sections).strip() + "\n"

    def _update_processing_manifest(self, report: ProcessedReport) -> None:
        """
        更新信息处理员总清单。

        参数：
            report: 单份财报处理结果。

        返回值：
            无。
        """
        json_manifest_path = self.manifest_dir / PROCESSING_JSON_MANIFEST_NAME
        csv_manifest_path = self.manifest_dir / PROCESSING_CSV_MANIFEST_NAME
        rows: list[dict[str, Any]] = []
        if json_manifest_path.exists():
            rows = json.loads(json_manifest_path.read_text(encoding="utf-8"))

        metadata = report.document_metadata
        manifest_row = {
            "pdf_sha256": report.pdf_sha256,
            "source_pdf_path": report.source_pdf_path,
            "processed_at": report.processed_at,
            "parser_engine": report.parser_engine,
            "page_count": report.page_count,
            "table_count": sum(len(page.tables) for page in report.pages),
            "kept_image_count": sum(1 for page in report.pages for image in page.images if image.decision == "keep"),
            "discarded_image_count": sum(1 for page in report.pages for image in page.images if image.decision == "discard"),
            "warning_count": len(report.warnings) + sum(len(page.warnings) for page in report.pages),
            "output_json": report.outputs.get("json", ""),
            "output_markdown": report.outputs.get("markdown", ""),
            "output_text": report.outputs.get("text", ""),
            "stock_code": metadata.get("stock_code", ""),
            "company_name": metadata.get("company_name", ""),
            "report_type": metadata.get("report_type", ""),
            "report_year": metadata.get("report_year", ""),
            "report_variant": metadata.get("report_variant", ""),
            "announcement_id": metadata.get("announcement_id", ""),
        }

        rows_by_hash = {str(row.get("pdf_sha256", "")): row for row in rows}
        rows_by_hash[report.pdf_sha256] = manifest_row
        merged_rows = sorted(rows_by_hash.values(), key=lambda row: (str(row.get("stock_code", "")), str(row.get("report_year", "")), str(row.get("source_pdf_path", ""))))

        json_manifest_path.write_text(json.dumps(merged_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        with csv_manifest_path.open("w", encoding="utf-8-sig", newline="") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=list(manifest_row.keys()))
            writer.writeheader()
            writer.writerows(merged_rows)

    def _build_document_metadata(
        self,
        *,
        pdf_path: Path,
        source_record: dict[str, Any] | None,
        pdf_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """
        合并 PDF 自带元数据与信息收集员业务元数据。

        参数：
            pdf_path: PDF 路径。
            source_record: 信息收集员 manifest 记录。
            pdf_metadata: PDF 文件内部元数据。

        返回值：
            合并后的元数据字典。
        """
        metadata: dict[str, Any] = {
            "pdf_file_name": pdf_path.name,
            "pdf_stem": pdf_path.stem,
            "pdf_size_bytes": pdf_path.stat().st_size,
            "pdf_metadata": pdf_metadata,
        }
        if source_record:
            metadata.update({key: value for key, value in source_record.items() if value is not None})
        if not metadata.get("title"):
            metadata["title"] = pdf_path.stem
        return metadata

    def _build_output_dir(self, pdf_path: Path, source_record: dict[str, Any] | None) -> Path:
        """
        为单份 PDF 构建稳定输出目录。

        参数：
            pdf_path: PDF 路径。
            source_record: 信息收集员记录。

        返回值：
            输出目录路径。
        """
        if source_record:
            report_type = self._safe_path_part(str(source_record.get("report_type") or "unknown_report_type"))
            report_year = self._safe_path_part(str(source_record.get("report_year") or "unknown_year"))
            stock_code = self._safe_path_part(str(source_record.get("stock_code") or "unknown_stock"))
            stem = self._safe_path_part(pdf_path.stem)
            return self.parsed_dir / report_type / report_year / stock_code / stem

        stem = self._safe_path_part(pdf_path.stem)
        sha_prefix = self._sha256_file(pdf_path)[:12]
        return self.parsed_dir / "direct" / f"{stem}-{sha_prefix}"

    def _normalize_table_rows(self, rows: list[list[Any]]) -> list[list[str]]:
        """
        清洗表格二维数组。

        参数：
            rows: 底层解析库返回的原始表格行。

        返回值：
            清洗后的二维字符串数组。
        """
        cleaned_rows: list[list[str]] = []
        for row in rows or []:
            cleaned_row = [self._clean_cell(cell) for cell in row]
            if any(cell for cell in cleaned_row):
                cleaned_rows.append(cleaned_row)
        return cleaned_rows

    def _write_table_csv(self, rows: list[list[str]], csv_path: Path) -> None:
        """
        写出单个表格 CSV。

        参数：
            rows: 表格二维数组。
            csv_path: CSV 输出路径。

        返回值：
            无。
        """
        with csv_path.open("w", encoding="utf-8-sig", newline="") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerows(rows)

    def _table_to_markdown(self, rows: list[list[str]]) -> str:
        """
        将表格二维数组转换为 Markdown 表格。

        参数：
            rows: 表格二维数组。

        返回值：
            Markdown 表格字符串。
        """
        if not rows:
            return ""
        max_columns = max(len(row) for row in rows)
        padded_rows = [row + [""] * (max_columns - len(row)) for row in rows]
        header = [self._escape_markdown_cell(cell) for cell in padded_rows[0]]
        separator = ["---"] * max_columns
        body = [[self._escape_markdown_cell(cell) for cell in row] for row in padded_rows[1:]]
        markdown_rows = ["| " + " | ".join(header) + " |", "| " + " | ".join(separator) + " |"]
        markdown_rows.extend("| " + " | ".join(row) + " |" for row in body)
        return "\n".join(markdown_rows)

    def _find_nearby_text(self, bbox: list[float], text_blocks: list[dict[str, Any]]) -> str:
        """
        查找图片周边文字。

        参数：
            bbox: 图片坐标。
            text_blocks: 页面文字块列表。

        返回值：
            按距离排序后的周边文字摘要。
        """
        if len(bbox) != 4:
            return ""
        image_center_y = (bbox[1] + bbox[3]) / 2
        image_top = bbox[1]
        image_bottom = bbox[3]
        candidates: list[tuple[float, str]] = []
        for block in text_blocks:
            block_bbox = block.get("bbox", [])
            block_text = self._single_line(str(block.get("text", "")))
            if len(block_bbox) != 4 or not block_text:
                continue
            block_center_y = (float(block_bbox[1]) + float(block_bbox[3])) / 2
            vertical_gap = min(abs(float(block_bbox[3]) - image_top), abs(float(block_bbox[1]) - image_bottom), abs(block_center_y - image_center_y))
            if vertical_gap <= 160:
                candidates.append((vertical_gap, block_text))
        candidates.sort(key=lambda item: item[0])
        merged_text = "；".join(text for _, text in candidates[:6])
        return merged_text[:600]

    def _calculate_area_ratio(self, bbox: list[float], page_area: float) -> float:
        """
        计算图片显示面积占整页面积比例。

        参数：
            bbox: 图片坐标。
            page_area: 页面面积。

        返回值：
            面积比例。
        """
        if len(bbox) != 4:
            return 0.0
        width = max(bbox[2] - bbox[0], 0.0)
        height = max(bbox[3] - bbox[1], 0.0)
        return (width * height) / page_area

    def _processed_report_from_dict(self, payload: dict[str, Any]) -> ProcessedReport:
        """
        从已有 JSON 解析结果恢复 ProcessedReport。

        参数：
            payload: content.json 中的字典。

        返回值：
            ProcessedReport 实例。
        """
        pages: list[ParsedPage] = []
        for page_payload in payload.get("pages", []):
            tables = [ParsedTable(**table_payload) for table_payload in page_payload.get("tables", [])]
            images = [ParsedImage(**image_payload) for image_payload in page_payload.get("images", [])]
            pages.append(
                ParsedPage(
                    page_number=page_payload.get("page_number", 0),
                    text=page_payload.get("text", ""),
                    tables=tables,
                    images=images,
                    warnings=page_payload.get("warnings", []),
                )
            )
        return ProcessedReport(
            source_pdf_path=payload.get("source_pdf_path", ""),
            pdf_sha256=payload.get("pdf_sha256", ""),
            processed_at=payload.get("processed_at", ""),
            parser_engine=payload.get("parser_engine", ""),
            document_metadata=payload.get("document_metadata", {}),
            page_count=payload.get("page_count", len(pages)),
            pages=pages,
            outputs=payload.get("outputs", {}),
            warnings=payload.get("warnings", []),
        )

    def _sha256_file(self, file_path: Path) -> str:
        """
        计算文件 SHA256。

        参数：
            file_path: 文件路径。

        返回值：
            十六进制 SHA256 字符串。
        """
        digest = hashlib.sha256()
        with file_path.open("rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _normalize_text(self, text: str) -> str:
        """
        对 PDF 文本做轻量清洗。

        参数：
            text: 原始文本。

        返回值：
            清洗后的文本。
        """
        normalized_lines: list[str] = []
        for line in text.replace("\r", "\n").split("\n"):
            cleaned_line = WHITESPACE_PATTERN.sub(" ", line).strip()
            normalized_lines.append(cleaned_line)
        while normalized_lines and not normalized_lines[0]:
            normalized_lines.pop(0)
        while normalized_lines and not normalized_lines[-1]:
            normalized_lines.pop()
        return "\n".join(normalized_lines)

    def _clean_cell(self, cell: Any) -> str:
        """
        清洗单个表格单元格。

        参数：
            cell: 原始单元格值。

        返回值：
            清洗后的字符串。
        """
        if cell is None:
            return ""
        return self._normalize_text(str(cell)).replace("\n", " ").strip()

    def _escape_markdown_cell(self, cell: str) -> str:
        """
        转义 Markdown 表格单元格。

        参数：
            cell: 单元格字符串。

        返回值：
            可安全放入 Markdown 表格的字符串。
        """
        return cell.replace("|", "\\|").replace("\n", "<br>")

    def _safe_path_part(self, value: str) -> str:
        """
        清洗路径片段。

        参数：
            value: 原始路径片段。

        返回值：
            可安全用于 Windows 文件名的字符串。
        """
        cleaned = SAFE_FILENAME_PATTERN.sub("_", value).strip().strip(".")
        return cleaned or "unknown"

    def _relative_to_workspace(self, path: Path) -> str:
        """
        将绝对路径转换为相对处理工作区路径。

        参数：
            path: 目标路径。

        返回值：
            相对路径字符串；如果路径不在工作区内，则返回绝对路径。
        """
        try:
            return str(path.resolve().relative_to(self.workspace)).replace("\\", "/")
        except ValueError:
            return str(path.resolve()).replace("\\", "/")

    def _normalize_digest(self, digest: Any) -> str:
        """
        统一图片摘要格式。

        参数：
            digest: 底层库返回的 bytes、字符串或其他对象。

        返回值：
            字符串摘要。
        """
        if isinstance(digest, bytes):
            return digest.hex()
        return str(digest)

    def _single_line(self, text: str) -> str:
        """
        将多行文本压缩成单行摘要。

        参数：
            text: 原始文本。

        返回值：
            单行文本。
        """
        return WHITESPACE_PATTERN.sub(" ", " ".join(text.split())).strip()

    def _utc_now(self) -> str:
        """
        获取当前 UTC 时间。

        参数：
            无。

        返回值：
            ISO 8601 时间字符串。
        """
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
