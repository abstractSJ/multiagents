"""
巨潮资讯 A 股财报采集核心模块。

该模块负责以下几类核心能力：
1. 按时间范围与财报类型，从巨潮资讯公开公告接口检索财报元数据；
2. 将所有采集结果合并维护为 2 份总清单：1 份 JSON、1 份 CSV；
3. 将财报 PDF 下载到统一工作区，并使用规范化文件名持久化保存；
4. 把旧批次 manifest、旧命名 PDF、旧 replay 工作区中的文件迁移到当前统一结构；
5. 保持对历史 manifest 的兼容重放能力，保证已经采集过的数据不会失效。
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib import parse, request

# 巨潮资讯公告查询接口与静态 PDF 下载域名。
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_HOST = "https://static.cninfo.com.cn/"

# 总清单固定文件名。
# 之所以固定成 2 个总文件，是因为用户已经明确要求停止继续生成按批次命名的小清单。
TOTAL_JSON_MANIFEST_NAME = "cninfo_all_reports.json"
TOTAL_CSV_MANIFEST_NAME = "cninfo_all_reports.csv"

# 这里统一使用浏览器形态的请求头，是因为巨潮资讯对非浏览器请求较为敏感。
# 这样做的目的不是规避限制，而是尽量让公开接口请求稳定、可复现。
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# 财报类型与巨潮资讯 category 编码的映射关系。
# 这里同时保存中文标签，是为了后续生成“股票代码-股票名-财报年度年报”这一类规范文件名时直接复用。
REPORT_TYPE_CONFIG = {
    "annual": {
        "label": "年报",
        "category": "category_ndbg_szsh",
    },
    "semiannual": {
        "label": "半年报",
        "category": "category_bndbg_szsh",
    },
    "q1": {
        "label": "一季报",
        "category": "category_yjdbg_szsh",
    },
    "q3": {
        "label": "三季报",
        "category": "category_sjdbg_szsh",
    },
}

# Windows 文件名不能包含以下字符；同时也去掉连续空白，避免路径过长和落盘失败。
INVALID_FILENAME_CHARS = re.compile(r"[\\/:*?\"<>|]+")
MULTI_SPACE_PATTERN = re.compile(r"\s+")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
REPORT_YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)(?:\s*年(?:度)?|[\s_\-]*年度)?")
ANNUAL_TITLE_YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)(?:\s*年(?:度)?\s*|[\s_\-]*)?(?:年度报告|年报)")
DELAY_NOTICE_KEYWORDS = ["延期披露", "推迟披露", "无法按期披露", "不能按期披露"]


@dataclass
class ReportRecord:
    """
    财报记录对象。

    参数说明：
        stock_code: 上市公司证券代码。
        company_name: 上市公司名称。
        report_type: 内部统一财报类型编码，例如 annual、semiannual。
        report_type_label: 面向业务使用的中文财报类型名称，例如 年报、半年报。
        announcement_id: 巨潮资讯公告唯一标识，可作为稳定主键使用。
        title: 公告标题原文，可能含高亮 HTML 标签。
        published_at: 公告发布时间，格式为 YYYY-MM-DD。
        source_adjunct_url: 巨潮资讯返回的相对 PDF 路径。
        source_pdf_url: 拼接后的完整 PDF 下载地址。
        local_relative_path: 财报在本地工作区内的相对路径。
        query_keyword: 本次查询时使用的关键字，可为空。
        query_category: 本次查询使用的巨潮资讯分类编码。
        page_num: 该记录来自查询结果的第几页。
        page_size: 单页请求大小，用于复现实验条件。
        download_status: 下载状态，当前允许出现 pending、downloaded、existing、failed:* 等值。
        downloaded_at: 实际下载完成时间，未下载时为空字符串。
        file_size_kb: 源站返回的 PDF 体积，单位 KB。
        report_year: 财报所属年度，例如 2025 年年报中的 2025。
        report_variant: 财报版本后缀，例如 摘要、英文版、修订版；正式版为空字符串。
        record_kind: 公告记录性质，例如 report、delay_notice、non_target。
        title_classification: 基于标题识别出的细分类型，例如 annual_full、annual_summary。
        security_category: 证券类别粗分类，例如 a_share、b_share、beijing_exchange。
        collection_warning: 采集或解析阶段发现的可审计风险标记。
    """

    stock_code: str = ""
    company_name: str = ""
    report_type: str = ""
    report_type_label: str = ""
    announcement_id: str = ""
    title: str = ""
    published_at: str = ""
    source_adjunct_url: str = ""
    source_pdf_url: str = ""
    local_relative_path: str = ""
    query_keyword: str = ""
    query_category: str = ""
    page_num: int = 0
    page_size: int = 0
    download_status: str = "pending"
    downloaded_at: str = ""
    file_size_kb: int = 0
    report_year: str = ""
    report_variant: str = ""
    record_kind: str = "report"
    title_classification: str = ""
    security_category: str = "unknown"
    collection_warning: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> "ReportRecord":
        """
        从任意 manifest 字典安全构造 ReportRecord。

        为什么需要这层兼容构造：
        因为历史 manifest 不包含 report_year、report_variant 等新字段，
        如果直接做 ReportRecord(**payload)，随着结构演化会很容易因缺字段或多字段失败。

        参数：
            payload: manifest 中的一条原始记录字典。

        返回值：
            兼容填充后的 ReportRecord 实例。
        """
        allowed_field_names = {field.name for field in fields(cls)}
        filtered_payload = {
            key: value for key, value in payload.items() if key in allowed_field_names
        }
        return cls(**filtered_payload)


class CninfoFinancialReportCollector:
    """
    巨潮资讯 A 股财报采集器。

    参数：
        workspace: 工作区目录。总清单文件与下载后的 PDF 都会落在该目录中。
        timeout: 单次网络请求超时时间，单位秒。
        retries: 网络请求失败时的最大重试次数。

    返回值：
        无显式返回值，初始化时会自动准备目录结构。
    """

    def __init__(self, workspace: str | Path, timeout: int = 30, retries: int = 3) -> None:
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout
        self.retries = max(1, retries)
        self.manifest_dir = self.workspace / "manifests"
        self.report_dir = self.workspace / "reports"
        self.total_json_manifest_path = self.manifest_dir / TOTAL_JSON_MANIFEST_NAME
        self.total_csv_manifest_path = self.manifest_dir / TOTAL_CSV_MANIFEST_NAME
        self.legacy_replay_workspace = self.workspace.parent / "replay_workspace"
        self.last_collection_audits: list[dict] = []
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """
        准备工作目录。

        为什么这里在初始化时就创建目录：
        因为后续既要写总清单又要写 PDF，还要做历史文件迁移；
        如果目录创建分散在多个函数里，排障时会很难定位到底是哪一步缺目录。
        """
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def query_page(
        self,
        *,
        report_type: str,
        start_date: str,
        end_date: str,
        page_num: int,
        page_size: int,
        keyword: str = "",
    ) -> dict:
        """
        查询单页财报公告。

        参数：
            report_type: 财报类型编码。
            start_date: 查询开始日期，格式 YYYY-MM-DD。
            end_date: 查询结束日期，格式 YYYY-MM-DD。
            page_num: 页码，从 1 开始。
            page_size: 单页条数。
            keyword: 搜索关键字，可传证券代码或公司名称。

        返回值：
            巨潮资讯返回的 JSON 字典。
        """
        if report_type not in REPORT_TYPE_CONFIG:
            raise ValueError(f"不支持的财报类型: {report_type}")

        payload = {
            "pageNum": page_num,
            "pageSize": page_size,
            # 当前验证结果表明，这个接口使用 szse 也可以返回全市场结果。
            # 因此这里固定成一个稳定值，避免把“上交所/深交所拆分抓取”误当成必要步骤。
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": keyword,
            "secid": "",
            "category": REPORT_TYPE_CONFIG[report_type]["category"],
            "trade": "",
            "seDate": f"{start_date}~{end_date}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        return self._post_json(payload)

    def iter_reports(
        self,
        *,
        report_types: Sequence[str],
        start_date: str,
        end_date: str,
        keyword: str = "",
        page_size: int = 30,
        max_pages: int | None = None,
        sleep_seconds: float = 0.2,
        split_windows: bool = False,
    ) -> Iterator[ReportRecord]:
        """
        按给定条件遍历财报记录。

        参数：
            report_types: 要抓取的财报类型列表。
            start_date: 查询开始日期。
            end_date: 查询结束日期。
            keyword: 搜索关键字。
            page_size: 单页条数。
            max_pages: 最多抓取多少页；为 None 时会抓完整个结果集。
            sleep_seconds: 页与页之间的等待秒数。
            split_windows: 是否按更小披露时间窗口采集全市场结果。

        返回值：
            逐条产出 ReportRecord。
        """
        normalized_types = self._normalize_report_types(report_types)
        self.last_collection_audits = []

        for report_type in normalized_types:
            if split_windows and not keyword:
                yield from self._iter_reports_by_windows(
                    report_type=report_type,
                    start_date=start_date,
                    end_date=end_date,
                    page_size=page_size,
                    max_pages=max_pages,
                    sleep_seconds=sleep_seconds,
                )
                continue

            yield from self._iter_reports_single_window(
                report_type=report_type,
                start_date=start_date,
                end_date=end_date,
                keyword=keyword,
                page_size=page_size,
                max_pages=max_pages,
                sleep_seconds=sleep_seconds,
                window_label="direct",
            )

    def _iter_reports_by_windows(
        self,
        *,
        report_type: str,
        start_date: str,
        end_date: str,
        page_size: int,
        max_pages: int | None,
        sleep_seconds: float,
    ) -> Iterator[ReportRecord]:
        """
        按月拆分全市场披露窗口，必要时自动下钻到按日采集。

        参数：
            report_type: 财报类型编码。
            start_date: 查询开始日期。
            end_date: 查询结束日期。
            page_size: 单页条数。
            max_pages: 单窗口最多抓取页数。
            sleep_seconds: 页间等待秒数。

        返回值：
            逐条产出 ReportRecord。
        """
        for window_start, window_end in self._iter_month_windows(start_date, end_date):
            first_payload = self.query_page(
                report_type=report_type,
                start_date=window_start,
                end_date=window_end,
                page_num=1,
                page_size=page_size,
                keyword="",
            )
            total_announcement = int(first_payload.get("totalAnnouncement") or 0)
            if total_announcement > page_size * 100:
                self.last_collection_audits.append(
                    {
                        "report_type": report_type,
                        "window_start": window_start,
                        "window_end": window_end,
                        "granularity": "month",
                        "total_announcement": total_announcement,
                        "warning": "large_window_split_to_daily",
                    }
                )
                for day_start, day_end in self._iter_day_windows(window_start, window_end):
                    yield from self._iter_reports_single_window(
                        report_type=report_type,
                        start_date=day_start,
                        end_date=day_end,
                        keyword="",
                        page_size=page_size,
                        max_pages=max_pages,
                        sleep_seconds=sleep_seconds,
                        window_label="daily",
                    )
                continue

            yield from self._iter_reports_single_window(
                report_type=report_type,
                start_date=window_start,
                end_date=window_end,
                keyword="",
                page_size=page_size,
                max_pages=max_pages,
                sleep_seconds=sleep_seconds,
                window_label="monthly",
                first_payload=first_payload,
            )

    def _iter_reports_single_window(
        self,
        *,
        report_type: str,
        start_date: str,
        end_date: str,
        keyword: str,
        page_size: int,
        max_pages: int | None,
        sleep_seconds: float,
        window_label: str,
        first_payload: dict | None = None,
    ) -> Iterator[ReportRecord]:
        """
        抓取单个披露时间窗口，并记录分页重复风险。

        参数：
            report_type: 财报类型编码。
            start_date: 查询开始日期。
            end_date: 查询结束日期。
            keyword: 搜索关键字。
            page_size: 单页条数。
            max_pages: 单窗口最多抓取页数。
            sleep_seconds: 页间等待秒数。
            window_label: 审计用窗口标签。
            first_payload: 可复用的第一页响应。

        返回值：
            逐条产出 ReportRecord。
        """
        page_num = 1
        seen_ids: set[str] = set()
        unique_count = 0
        repeated_pages = 0
        total_announcement = 0
        paging_warning = ""

        while True:
            if max_pages is not None and page_num > max_pages:
                break

            if page_num == 1 and first_payload is not None:
                payload = first_payload
            else:
                payload = self.query_page(
                    report_type=report_type,
                    start_date=start_date,
                    end_date=end_date,
                    page_num=page_num,
                    page_size=page_size,
                    keyword=keyword,
                )

            total_announcement = int(payload.get("totalAnnouncement") or 0)
            announcements = payload.get("announcements") or []
            if not announcements:
                break

            page_ids = [str(item.get("announcementId") or "").strip() for item in announcements]
            valid_page_ids = [announcement_id for announcement_id in page_ids if announcement_id]
            repeated_id_count = sum(1 for announcement_id in valid_page_ids if announcement_id in seen_ids)
            if valid_page_ids and repeated_id_count == len(valid_page_ids):
                repeated_pages += 1
                paging_warning = "paging_repeat_suspected"
                break

            for item in announcements:
                record = self._build_record(
                    raw_item=item,
                    report_type=report_type,
                    keyword=keyword,
                    page_num=page_num,
                    page_size=page_size,
                )
                if paging_warning:
                    record.collection_warning = self._merge_warning_tokens(
                        record.collection_warning,
                        paging_warning,
                    )
                if record.announcement_id not in seen_ids:
                    unique_count += 1
                seen_ids.add(record.announcement_id)
                yield record

            if page_num * page_size >= total_announcement:
                break

            page_num += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        self.last_collection_audits.append(
            {
                "report_type": report_type,
                "window_start": start_date,
                "window_end": end_date,
                "granularity": window_label,
                "total_announcement": total_announcement,
                "unique_announcements": unique_count,
                "requested_pages": page_num,
                "repeated_pages": repeated_pages,
                "warning": paging_warning,
            }
        )

    def _iter_month_windows(self, start_date: str, end_date: str) -> Iterator[tuple[str, str]]:
        """
        将日期区间拆成自然月窗口。

        参数：
            start_date: 查询开始日期。
            end_date: 查询结束日期。

        返回值：
            逐个产出月度窗口起止日期。
        """
        current_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        final_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        while current_date <= final_date:
            if current_date.month == 12:
                next_month = date(current_date.year + 1, 1, 1)
            else:
                next_month = date(current_date.year, current_date.month + 1, 1)
            window_end = min(next_month - timedelta(days=1), final_date)
            yield current_date.isoformat(), window_end.isoformat()
            current_date = window_end + timedelta(days=1)

    def _iter_day_windows(self, start_date: str, end_date: str) -> Iterator[tuple[str, str]]:
        """
        将日期区间拆成按日窗口。

        参数：
            start_date: 查询开始日期。
            end_date: 查询结束日期。

        返回值：
            逐个产出日度窗口起止日期。
        """
        current_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        final_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        while current_date <= final_date:
            day = current_date.isoformat()
            yield day, day
            current_date += timedelta(days=1)

    def collect(
        self,
        *,
        report_types: Sequence[str],
        start_date: str,
        end_date: str,
        keyword: str = "",
        page_size: int = 30,
        max_pages: int | None = None,
        sleep_seconds: float = 0.2,
        download: bool = False,
        overwrite: bool = False,
        split_windows: bool = False,
    ) -> tuple[list[ReportRecord], Path, Path]:
        """
        执行一次完整采集流程，并将结果并入 2 份总清单。

        参数：
            report_types: 财报类型列表。
            start_date: 查询开始日期。
            end_date: 查询结束日期。
            keyword: 搜索关键字。
            page_size: 单页条数。
            max_pages: 最多抓取页数。
            sleep_seconds: 翻页等待秒数。
            download: 是否同步下载 PDF。
            overwrite: 如果本地已存在 PDF，是否覆盖。
            split_windows: 是否按月/日拆分披露窗口以降低分页失真。

        返回值：
            records: 本次采集得到的记录列表。
            json_manifest_path: 总 JSON 清单路径。
            csv_manifest_path: 总 CSV 清单路径。
        """
        existing_records = self._load_existing_records_from_workspace()
        current_records = self._deduplicate_records(
            self.iter_reports(
                report_types=report_types,
                start_date=start_date,
                end_date=end_date,
                keyword=keyword,
                page_size=page_size,
                max_pages=max_pages,
                sleep_seconds=sleep_seconds,
                split_windows=split_windows,
            )
        )

        record_index = self._build_record_index(existing_records)
        for current_record in current_records:
            self._merge_record_into_index(record_index, current_record)

        all_records = list(record_index.values())
        self._normalize_records(all_records, migrate_files=True)

        # 这里重新从合并后的总索引中取回“本次记录”，是为了保证：
        # 1) 当前运行拿到的是总清单中的最终对象；
        # 2) 路径规范化、状态合并后的结果会同步反映到本次返回值中。
        current_record_ids = [record.announcement_id for record in current_records]
        current_records = [
            record_index[announcement_id]
            for announcement_id in current_record_ids
            if announcement_id in record_index
        ]

        self.write_manifests(
            all_records,
            self.total_json_manifest_path,
            self.total_csv_manifest_path,
        )

        if download:
            for record in current_records:
                self.download_report(record, overwrite=overwrite)
            self.write_manifests(
                all_records,
                self.total_json_manifest_path,
                self.total_csv_manifest_path,
            )

        self._cleanup_legacy_manifest_files()
        self._cleanup_empty_directories()
        return current_records, self.total_json_manifest_path, self.total_csv_manifest_path

    def write_manifests(
        self,
        records: Sequence[ReportRecord],
        json_manifest_path: str | Path,
        csv_manifest_path: str | Path,
    ) -> None:
        """
        将财报记录分别写入 JSON 与 CSV 清单。

        参数：
            records: 财报记录列表。
            json_manifest_path: JSON 清单落盘路径。
            csv_manifest_path: CSV 清单落盘路径。

        返回值：
            无。
        """
        json_path = Path(json_manifest_path)
        csv_path = Path(csv_manifest_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        # 这里统一按披露日期倒序排序，是因为总清单更像长期维护的资料主表，
        # 人工检查时通常更关心最近新增的记录，而不是历史最早的记录。
        sorted_records = sorted(
            records,
            key=lambda record: (
                record.published_at or "",
                record.stock_code or "",
                record.announcement_id or "",
            ),
            reverse=True,
        )

        json_payload = [asdict(record) for record in sorted_records]
        json_path.write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        fieldnames = list(ReportRecord.__dataclass_fields__.keys())
        with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for record in sorted_records:
                writer.writerow(asdict(record))

    def download_report(self, record: ReportRecord, overwrite: bool = False) -> Path:
        """
        下载单份财报 PDF。

        参数：
            record: 待下载的财报记录。
            overwrite: 本地已存在文件时是否覆盖。

        返回值：
            下载后的本地绝对路径。
        """
        self._normalize_records([record], migrate_files=True)
        local_path = self.workspace / record.local_relative_path
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if local_path.exists() and not overwrite:
            record.download_status = "existing"
            record.downloaded_at = self._now_string()
            return local_path

        request_object = request.Request(
            record.source_pdf_url,
            headers={
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
                "Referer": "https://www.cninfo.com.cn/",
            },
        )

        last_error: Exception | None = None
        for attempt_index in range(1, self.retries + 1):
            try:
                with request.urlopen(request_object, timeout=self.timeout) as response:
                    file_bytes = response.read()
                local_path.write_bytes(file_bytes)
                record.download_status = "downloaded"
                record.downloaded_at = self._now_string()
                return local_path
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                record.download_status = f"failed:{type(exc).__name__}"
                if attempt_index < self.retries:
                    time.sleep(1)

        if last_error is not None:
            raise last_error
        raise RuntimeError("下载失败，但未捕获到明确异常。")

    def _post_json(self, payload: dict) -> dict:
        """
        发起 POST 请求并解析 JSON。

        为什么单独封装这一层：
        采集链路最脆弱的部分就是网络请求，重试、编码、请求头等都应该集中收口，避免每个调用点各自拼装。
        """
        body = parse.urlencode(payload).encode("utf-8")
        last_error: Exception | None = None

        for attempt_index in range(1, self.retries + 1):
            try:
                request_object = request.Request(
                    CNINFO_QUERY_URL,
                    data=body,
                    headers=DEFAULT_HEADERS,
                    method="POST",
                )
                with request.urlopen(request_object, timeout=self.timeout) as response:
                    response_text = response.read().decode("utf-8")
                return json.loads(response_text)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt_index < self.retries:
                    time.sleep(1)

        if last_error is not None:
            raise last_error
        raise RuntimeError("请求失败，但未捕获到明确异常。")

    def _build_record(
        self,
        *,
        raw_item: dict,
        report_type: str,
        keyword: str,
        page_num: int,
        page_size: int,
    ) -> ReportRecord:
        """
        将巨潮资讯原始响应项转换为统一记录对象。

        参数：
            raw_item: 巨潮资讯返回的单条公告字典。
            report_type: 当前财报类型编码。
            keyword: 查询关键字。
            page_num: 来源页码。
            page_size: 来源页大小。

        返回值：
            标准化后的 ReportRecord。
        """
        stock_code = str(raw_item.get("secCode") or "").strip()
        company_name = self._clean_text(raw_item.get("secName") or "")
        title = self._clean_text(raw_item.get("announcementTitle") or "")
        announcement_id = str(raw_item.get("announcementId") or "").strip()
        source_adjunct_url = str(raw_item.get("adjunctUrl") or "").strip()
        source_pdf_url = parse.urljoin(CNINFO_STATIC_HOST, source_adjunct_url)
        published_at = self._timestamp_to_date(raw_item.get("announcementTime"))
        file_size_kb = int(raw_item.get("adjunctSize") or 0)
        report_year, collection_warning = self._extract_report_year_with_warning(
            title=title,
            published_at=published_at,
        )
        title_classification = self._classify_title(title=title, report_type=report_type)
        record_kind = self._detect_record_kind(title_classification)
        report_variant = self._detect_report_variant(title)
        security_category = self._detect_security_category(stock_code)
        local_relative_path = self._build_relative_path(
            stock_code=stock_code,
            company_name=company_name,
            report_type=report_type,
            report_year=report_year,
            report_variant=report_variant,
            announcement_id=announcement_id,
            include_announcement_suffix=False,
        )

        return ReportRecord(
            stock_code=stock_code,
            company_name=company_name,
            report_type=report_type,
            report_type_label=REPORT_TYPE_CONFIG[report_type]["label"],
            announcement_id=announcement_id,
            title=title,
            published_at=published_at,
            source_adjunct_url=source_adjunct_url,
            source_pdf_url=source_pdf_url,
            local_relative_path=local_relative_path,
            query_keyword=keyword,
            query_category=REPORT_TYPE_CONFIG[report_type]["category"],
            page_num=page_num,
            page_size=page_size,
            file_size_kb=file_size_kb,
            report_year=report_year,
            report_variant=report_variant,
            record_kind=record_kind,
            title_classification=title_classification,
            security_category=security_category,
            collection_warning=collection_warning,
        )

    def _build_relative_path(
        self,
        *,
        stock_code: str,
        company_name: str,
        report_type: str,
        report_year: str,
        report_variant: str,
        announcement_id: str,
        include_announcement_suffix: bool,
    ) -> str:
        """
        生成财报在工作区内的规范相对路径。

        为什么这里改成“股票代码-股票名-财报年度-版本后缀”的格式：
        因为用户希望本地看到文件名时，不需要再去读 manifest 才知道这份 PDF 是谁的、属于哪一财年、是不是摘要版。
        同时，只有在同名冲突时才追加公告编号，既保证可读性，也避免像“同一财年两份摘要”这种场景互相覆盖。

        参数：
            stock_code: 股票代码。
            company_name: 股票名称。
            report_type: 财报类型编码。
            report_year: 财报所属年度。
            report_variant: 版本后缀，例如 摘要、英文版、修订版。
            announcement_id: 公告编号。
            include_announcement_suffix: 是否在文件名末尾追加公告编号后缀。

        返回值：
            规范化后的相对路径。
        """
        safe_stock_code = self._safe_path_part(stock_code, fallback="unknown_code")
        safe_company_name = self._safe_path_part(company_name, fallback="unknown_company")
        safe_report_year = self._safe_path_part(report_year, fallback="unknown_year")
        safe_announcement_id = self._safe_path_part(announcement_id, fallback="unknown_announcement")
        report_type_label = REPORT_TYPE_CONFIG.get(report_type, {}).get("label", report_type or "财报")
        safe_report_type_label = self._safe_path_part(report_type_label, fallback="财报")

        filename_stem = f"{safe_stock_code}-{safe_company_name}-{safe_report_year}年{safe_report_type_label}"
        if report_variant:
            safe_report_variant = self._safe_path_part(report_variant, fallback="版本")
            filename_stem = f"{filename_stem}-{safe_report_variant}"
        if include_announcement_suffix:
            filename_stem = f"{filename_stem}-公告{safe_announcement_id}"

        filename = f"{filename_stem}.pdf"
        relative_path = (
            Path("reports")
            / report_type
            / safe_report_year
            / safe_stock_code
            / filename
        )
        return relative_path.as_posix()

    def _deduplicate_records(self, records: Iterable[ReportRecord]) -> list[ReportRecord]:
        """
        按公告编号去重，并保留信息更完整、状态更好的版本。

        为什么这里不能再像最初版本那样“见到重复就跳过”：
        因为总清单模式下，同一公告会在不同运行中被再次命中，
        后一次运行可能带来更完整的字段值、更新后的下载状态或新的规范路径。

        参数：
            records: 任意来源的财报记录序列。

        返回值：
            合并去重后的记录列表。
        """
        record_index = self._build_record_index([])
        for record in records:
            self._merge_record_into_index(record_index, record)
        return list(record_index.values())

    def _build_record_index(self, records: Iterable[ReportRecord]) -> dict[str, ReportRecord]:
        """
        以公告编号构建记录索引。

        参数：
            records: 财报记录序列。

        返回值：
            以 announcement_id 为键的记录字典。
        """
        record_index: dict[str, ReportRecord] = {}
        for record in records:
            if not record.announcement_id:
                continue
            self._merge_record_into_index(record_index, record)
        return record_index

    def _merge_record_into_index(
        self,
        record_index: dict[str, ReportRecord],
        incoming_record: ReportRecord,
    ) -> ReportRecord:
        """
        将一条记录合并进总索引，并保留信息更完整的版本。

        参数：
            record_index: 以 announcement_id 为键的总索引。
            incoming_record: 待合并记录。

        返回值：
            合并后的最终记录对象。
        """
        if not incoming_record.announcement_id:
            return incoming_record

        existing_record = record_index.get(incoming_record.announcement_id)
        if existing_record is None:
            record_index[incoming_record.announcement_id] = incoming_record
            return incoming_record

        self._merge_record(existing_record, incoming_record)
        return existing_record

    def _merge_record(self, target_record: ReportRecord, source_record: ReportRecord) -> None:
        """
        将 source_record 的有效信息合并到 target_record。

        为什么这里采用“按字段选择更优值”的合并策略：
        因为总清单是长期主表，同一条公告可能先以 pending 状态进入，
        后续又以 downloaded 状态再次出现；如果只保留第一次记录，就会丢失最新状态。

        参数：
            target_record: 被更新的目标记录。
            source_record: 提供新信息的来源记录。

        返回值：
            无，直接原地修改 target_record。
        """
        preferred_text_fields = [
            "stock_code",
            "company_name",
            "report_type",
            "report_type_label",
            "title",
            "published_at",
            "source_adjunct_url",
            "source_pdf_url",
            "local_relative_path",
            "query_keyword",
            "query_category",
            "report_year",
            "report_variant",
            "record_kind",
            "title_classification",
            "security_category",
            "collection_warning",
        ]
        for field_name in preferred_text_fields:
            incoming_value = getattr(source_record, field_name)
            if incoming_value not in ("", None):
                setattr(target_record, field_name, incoming_value)

        preferred_numeric_fields = ["page_num", "page_size", "file_size_kb"]
        for field_name in preferred_numeric_fields:
            incoming_value = getattr(source_record, field_name)
            if isinstance(incoming_value, int) and incoming_value > 0:
                setattr(target_record, field_name, incoming_value)

        target_status_priority = self._status_priority(target_record.download_status)
        source_status_priority = self._status_priority(source_record.download_status)
        if source_status_priority >= target_status_priority:
            target_record.download_status = source_record.download_status
            if source_record.downloaded_at:
                target_record.downloaded_at = source_record.downloaded_at
        elif not target_record.downloaded_at and source_record.downloaded_at:
            target_record.downloaded_at = source_record.downloaded_at

    def _status_priority(self, status: str) -> int:
        """
        计算下载状态优先级。

        参数：
            status: 当前下载状态字符串。

        返回值：
            状态优先级，数值越大表示状态越“完整”。
        """
        if status in {"downloaded", "existing"}:
            return 3
        if isinstance(status, str) and status.startswith("failed:"):
            return 2
        if status == "pending":
            return 1
        return 0

    def _load_existing_records_from_workspace(self) -> list[ReportRecord]:
        """
        从工作区加载现有总清单与历史批次清单，并完成路径规范化迁移。

        为什么要把“历史加载 + 迁移”放在同一个入口：
        因为用户已经要求只保留 2 个总清单文件，
        所以每次正式采集前，都应先把历史碎片 manifest 收拢成统一主表，再继续增量更新。

        返回值：
            迁移、合并、规范化后的总记录列表。
        """
        total_records = self._load_manifest_records(self.total_json_manifest_path)
        legacy_records: list[ReportRecord] = []
        for legacy_manifest_path in self._list_legacy_json_manifest_paths():
            legacy_records.extend(self._load_manifest_records(legacy_manifest_path))

        merged_records = self._deduplicate_records([*total_records, *legacy_records])
        self._normalize_records(merged_records, migrate_files=True)
        return merged_records

    def _load_manifest_records(self, manifest_path: str | Path) -> list[ReportRecord]:
        """
        从指定 JSON manifest 中加载记录。

        参数：
            manifest_path: manifest 文件路径。

        返回值：
            manifest 中的记录列表；文件不存在时返回空列表。
        """
        manifest = Path(manifest_path)
        if not manifest.exists():
            return []

        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"manifest 内容不是列表结构: {manifest}")
        return [ReportRecord.from_dict(item) for item in payload]

    def _list_legacy_json_manifest_paths(self) -> list[Path]:
        """
        列出需要并入总清单的历史批次 JSON manifest。

        返回值：
            历史 JSON manifest 路径列表。
        """
        legacy_manifest_paths: list[Path] = []
        for manifest_path in sorted(self.manifest_dir.glob("cninfo_*.json")):
            if manifest_path.name == TOTAL_JSON_MANIFEST_NAME:
                continue
            legacy_manifest_paths.append(manifest_path)
        return legacy_manifest_paths

    def _cleanup_legacy_manifest_files(self) -> None:
        """
        删除已并入总清单的历史批次 manifest 文件。

        为什么这里在总清单写回之后再删旧文件：
        因为用户明确要求最终只保留 2 个总文件，但在总清单尚未落盘成功之前就删旧文件，
        会增加数据回退难度；先写总清单，再清旧文件更安全。
        """
        for manifest_path in sorted(self.manifest_dir.glob("cninfo_*.json")):
            if manifest_path.name == TOTAL_JSON_MANIFEST_NAME:
                continue
            manifest_path.unlink(missing_ok=True)
        for manifest_path in sorted(self.manifest_dir.glob("cninfo_*.csv")):
            if manifest_path.name == TOTAL_CSV_MANIFEST_NAME:
                continue
            manifest_path.unlink(missing_ok=True)

    def _normalize_records(self, records: Sequence[ReportRecord], migrate_files: bool) -> None:
        """
        统一补齐记录派生字段、规范路径，并在需要时迁移旧文件。

        参数：
            records: 待规范化的记录列表。
            migrate_files: 是否执行真实文件迁移。

        返回值：
            无，直接原地修改 records 中的对象。
        """
        if not records:
            return

        original_relative_paths: dict[str, str] = {}
        path_buckets: dict[str, list[ReportRecord]] = {}

        for record in records:
            original_relative_paths[record.announcement_id] = record.local_relative_path
            self._enrich_record_metadata(record)
            candidate_relative_path = self._build_relative_path(
                stock_code=record.stock_code,
                company_name=record.company_name,
                report_type=record.report_type,
                report_year=record.report_year,
                report_variant=record.report_variant,
                announcement_id=record.announcement_id,
                include_announcement_suffix=False,
            )
            path_buckets.setdefault(candidate_relative_path, []).append(record)

        for candidate_relative_path, bucket_records in path_buckets.items():
            if len(bucket_records) == 1:
                bucket_records[0].local_relative_path = candidate_relative_path
                continue

            # 同一公司、同一财年、同一版本后缀下如果出现多份文件，
            # 例如“同一财年两份摘要”，就必须追加公告编号，否则会发生文件覆盖。
            for record in sorted(bucket_records, key=lambda item: item.announcement_id):
                record.local_relative_path = self._build_relative_path(
                    stock_code=record.stock_code,
                    company_name=record.company_name,
                    report_type=record.report_type,
                    report_year=record.report_year,
                    report_variant=record.report_variant,
                    announcement_id=record.announcement_id,
                    include_announcement_suffix=True,
                )

        if not migrate_files:
            return

        for record in records:
            old_relative_path = original_relative_paths.get(record.announcement_id, "")
            self._migrate_record_file_to_canonical_path(
                record=record,
                old_relative_path=old_relative_path,
            )

    def _enrich_record_metadata(self, record: ReportRecord) -> None:
        """
        根据现有字段补齐记录的派生信息。

        参数：
            record: 待补齐的记录对象。

        返回值：
            无，直接原地修改 record。
        """
        record.stock_code = str(record.stock_code or "").strip()
        record.company_name = self._clean_text(record.company_name or "")
        record.title = self._clean_text(record.title or "")

        if not record.report_type_label:
            record.report_type_label = REPORT_TYPE_CONFIG.get(record.report_type, {}).get(
                "label",
                record.report_type,
            )
        if record.source_adjunct_url and not record.source_pdf_url:
            record.source_pdf_url = parse.urljoin(CNINFO_STATIC_HOST, record.source_adjunct_url)

        report_year, collection_warning = self._extract_report_year_with_warning(
            title=record.title,
            published_at=record.published_at,
            fallback_year=record.report_year,
        )
        record.report_year = report_year
        record.collection_warning = self._merge_warning_tokens(
            record.collection_warning,
            collection_warning,
        )
        record.title_classification = self._classify_title(
            title=record.title,
            report_type=record.report_type,
            fallback_classification=record.title_classification,
        )
        record.record_kind = self._detect_record_kind(record.title_classification)
        record.report_variant = self._detect_report_variant(record.title, fallback_variant=record.report_variant)
        record.security_category = self._detect_security_category(record.stock_code)

        if not record.download_status:
            record.download_status = "pending"

    def _extract_report_year(
        self,
        *,
        title: str,
        published_at: str,
        fallback_year: str = "",
    ) -> str:
        """
        从标题中提取财报所属年度。

        参数：
            title: 公告标题。
            published_at: 披露日期。
            fallback_year: 当标题中无法提取年度时的备用年度。

        返回值：
            财报所属年度字符串，例如 2025。
        """
        report_year, _ = self._extract_report_year_with_warning(
            title=title,
            published_at=published_at,
            fallback_year=fallback_year,
        )
        return report_year

    def _extract_report_year_with_warning(
        self,
        *,
        title: str,
        published_at: str,
        fallback_year: str = "",
    ) -> tuple[str, str]:
        """
        提取财报所属年度，并返回是否使用了低置信兜底。

        参数：
            title: 公告标题。
            published_at: 披露日期。
            fallback_year: 历史清单中已有的年度。

        返回值：
            二元组，第一项是财报年度，第二项是解析告警标记。
        """
        normalized_title = title or ""
        annual_matched = ANNUAL_TITLE_YEAR_PATTERN.search(normalized_title)
        if annual_matched:
            return annual_matched.group(1), ""

        matched = REPORT_YEAR_PATTERN.search(normalized_title)
        if matched:
            return matched.group(1), ""
        if fallback_year:
            return fallback_year, "year_parse_used_existing_value"
        if published_at:
            return published_at[:4], "year_parse_fallback_used"
        return "unknown_year", "year_parse_failed"

    def _classify_title(
        self,
        *,
        title: str,
        report_type: str,
        fallback_classification: str = "",
    ) -> str:
        """
        根据公告标题识别其对缺失核验有用的业务类型。

        参数：
            title: 公告标题。
            report_type: 当前查询使用的财报类型。
            fallback_classification: 历史清单中的已有分类。

        返回值：
            标准化后的标题分类字符串。
        """
        normalized_title = title or ""
        if any(keyword in normalized_title for keyword in DELAY_NOTICE_KEYWORDS):
            if "年度报告" in normalized_title or "年报" in normalized_title:
                return "annual_delay_notice"
            return f"{report_type}_delay_notice" if report_type else "delay_notice"

        is_english = "英文" in normalized_title
        is_summary = "摘要" in normalized_title
        is_revision = any(keyword in normalized_title for keyword in ["修订", "更正", "更新后", "更正版"])
        is_annual_title = "年度报告" in normalized_title or "年报" in normalized_title

        if report_type == "annual" and is_annual_title:
            if is_english and is_summary:
                return "annual_english_summary"
            if is_english:
                return "annual_english_full"
            if is_summary:
                return "annual_summary"
            if is_revision:
                return "annual_revision"
            return "annual_full"

        if fallback_classification:
            return fallback_classification
        if is_revision:
            return f"{report_type}_revision" if report_type else "revision"
        if is_summary:
            return f"{report_type}_summary" if report_type else "summary"
        return f"{report_type}_full" if report_type else "unknown"

    def _detect_record_kind(self, title_classification: str) -> str:
        """
        将标题分类收敛为公告记录性质。

        参数：
            title_classification: `_classify_title` 输出的细分类型。

        返回值：
            report、delay_notice 或 non_target。
        """
        if title_classification.endswith("_delay_notice"):
            return "delay_notice"
        if title_classification in {"unknown", "non_target"}:
            return "non_target"
        return "report"

    def _detect_security_category(self, stock_code: str) -> str:
        """
        按证券代码做粗粒度证券类别识别。

        参数：
            stock_code: 证券代码。

        返回值：
            a_share、b_share、beijing_exchange 或 unknown。
        """
        code = str(stock_code or "").strip()
        if code.startswith(("200", "201", "900")):
            return "b_share"
        if code.startswith(("83", "87", "88", "92")):
            return "beijing_exchange"
        if len(code) == 6 and code[:1] in {"0", "3", "6"}:
            return "a_share"
        return "unknown"

    def _merge_warning_tokens(self, *warnings: str) -> str:
        """
        合并多个采集告警标记，并保持稳定去重。

        参数：
            warnings: 分号分隔或单个告警字符串。

        返回值：
            分号分隔的去重告警字符串。
        """
        tokens: list[str] = []
        seen_tokens: set[str] = set()
        for warning in warnings:
            for token in str(warning or "").split(";"):
                cleaned_token = token.strip()
                if not cleaned_token or cleaned_token in seen_tokens:
                    continue
                seen_tokens.add(cleaned_token)
                tokens.append(cleaned_token)
        return ";".join(tokens)

    def _detect_report_variant(self, title: str, fallback_variant: str = "") -> str:
        """
        根据标题识别财报版本后缀。

        参数：
            title: 公告标题。
            fallback_variant: 当标题无法识别时使用的备用版本值。

        返回值：
            版本后缀；正式版返回空字符串。
        """
        normalized_title = title or ""
        if any(keyword in normalized_title for keyword in ["修订", "更正", "更新后", "更正版"]):
            return "修订版"
        if "英文" in normalized_title:
            return "英文版"
        if "摘要" in normalized_title:
            return "摘要"
        return fallback_variant

    def _migrate_record_file_to_canonical_path(
        self,
        *,
        record: ReportRecord,
        old_relative_path: str,
    ) -> None:
        """
        把旧路径下的 PDF 迁移到当前规范路径。

        参数：
            record: 已经拥有规范 local_relative_path 的记录。
            old_relative_path: 迁移前记录中的旧相对路径。

        返回值：
            无。
        """
        if not record.local_relative_path:
            return

        canonical_absolute_path = self.workspace / record.local_relative_path
        canonical_absolute_path.parent.mkdir(parents=True, exist_ok=True)

        candidate_paths = self._build_candidate_file_paths(old_relative_path)
        existing_candidate_paths = [path for path in candidate_paths if path.exists()]

        # 如果规范路径本身已经存在，就把其它旧路径下的副本删除掉，避免同一份文件在多个目录重复存在。
        if canonical_absolute_path.exists():
            for candidate_path in existing_candidate_paths:
                if candidate_path.resolve() == canonical_absolute_path.resolve():
                    continue
                candidate_path.unlink(missing_ok=True)
            return

        for candidate_path in existing_candidate_paths:
            if candidate_path.resolve() == canonical_absolute_path.resolve():
                return
            shutil.move(str(candidate_path), str(canonical_absolute_path))
            # 迁移成功后，其它同源副本就不再需要保留了。
            for remaining_candidate_path in existing_candidate_paths:
                if remaining_candidate_path == candidate_path:
                    continue
                remaining_candidate_path.unlink(missing_ok=True)
            return

    def _build_candidate_file_paths(self, old_relative_path: str) -> list[Path]:
        """
        为同一条记录构建所有可能存在历史文件的候选绝对路径。

        为什么这里同时检查统一工作区与旧 replay 工作区：
        因为过去的重放实验曾经把同一份文件下载到 `replay_workspace`，
        如果不把它也纳入候选路径，统一工作区后这些文件就会继续散落在外。

        参数：
            old_relative_path: 旧 manifest 中记录的相对路径。

        返回值：
            可能存在该文件的候选绝对路径列表。
        """
        candidate_paths: list[Path] = []
        normalized_old_relative_path = old_relative_path or ""
        if normalized_old_relative_path:
            candidate_paths.append(self.workspace / normalized_old_relative_path)
            candidate_paths.append(self.legacy_replay_workspace / normalized_old_relative_path)

        unique_candidate_paths: list[Path] = []
        seen_paths: set[str] = set()
        for candidate_path in candidate_paths:
            normalized_candidate_path = str(candidate_path.resolve(strict=False))
            if normalized_candidate_path in seen_paths:
                continue
            seen_paths.add(normalized_candidate_path)
            unique_candidate_paths.append(candidate_path)
        return unique_candidate_paths

    def _cleanup_empty_directories(self) -> None:
        """
        清理迁移后遗留的空目录。

        为什么要做这一步：
        因为旧路径迁移完成后，原来按披露年份命名的目录、旧 replay 工作区中的目录可能已经空了，
        如果不清理，用户仍然会看到大量失效目录，体验上仍然像是“有两套工作区、两套命名体系”。
        """
        self._remove_empty_directories_under(self.report_dir)
        if self.legacy_replay_workspace.exists():
            self._remove_empty_directories_under(self.legacy_replay_workspace)
            try:
                if self.legacy_replay_workspace.exists() and not any(self.legacy_replay_workspace.iterdir()):
                    self.legacy_replay_workspace.rmdir()
            except OSError:
                return

    def _remove_empty_directories_under(self, root_directory: Path) -> None:
        """
        递归删除指定根目录下的空目录。

        参数：
            root_directory: 要清理的根目录。

        返回值：
            无。
        """
        if not root_directory.exists():
            return
        for directory_path in sorted(
            [path for path in root_directory.rglob("*") if path.is_dir()],
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                if any(directory_path.iterdir()):
                    continue
                directory_path.rmdir()
            except OSError:
                continue

    def _normalize_report_types(self, report_types: Sequence[str]) -> list[str]:
        """
        规范化财报类型参数。

        参数：
            report_types: 用户输入的财报类型列表。

        返回值：
            规范化后的类型列表。
        """
        if not report_types:
            return list(REPORT_TYPE_CONFIG.keys())

        if len(report_types) == 1 and report_types[0] == "all":
            return list(REPORT_TYPE_CONFIG.keys())

        normalized_types: list[str] = []
        for report_type in report_types:
            if report_type not in REPORT_TYPE_CONFIG:
                raise ValueError(f"不支持的财报类型: {report_type}")
            normalized_types.append(report_type)
        return normalized_types

    @staticmethod
    def _clean_text(raw_text: str) -> str:
        """
        清理源站返回的文本字段。

        为什么要先去 HTML 再做空白压缩：
        因为源站会在命中关键字时插入 <em> 标签，如果直接拿原文落盘，后续 agent 做匹配时容易产生噪音。
        """
        no_html_text = HTML_TAG_PATTERN.sub("", raw_text)
        unescaped_text = unescape(no_html_text)
        compact_text = MULTI_SPACE_PATTERN.sub(" ", unescaped_text)
        return compact_text.strip()

    @staticmethod
    def _safe_path_part(raw_text: str, fallback: str) -> str:
        """
        将任意文本转换为适合文件路径的安全片段。

        参数：
            raw_text: 原始文本。
            fallback: 当原始文本清洗后为空时使用的兜底值。

        返回值：
            可安全落盘的短路径片段。
        """
        cleaned_text = INVALID_FILENAME_CHARS.sub("_", raw_text)
        cleaned_text = MULTI_SPACE_PATTERN.sub("_", cleaned_text)
        cleaned_text = cleaned_text.strip(" ._")
        if not cleaned_text:
            cleaned_text = fallback
        # 限制片段长度是为了控制 Windows 长路径风险，尤其是在标题异常冗长时。
        return cleaned_text[:80]

    @staticmethod
    def _timestamp_to_date(timestamp_ms: int | str | None) -> str:
        """
        将毫秒级时间戳转换为 YYYY-MM-DD。

        参数：
            timestamp_ms: 巨潮资讯返回的毫秒时间戳。

        返回值：
            格式化日期字符串；无法解析时返回空字符串。
        """
        if timestamp_ms in (None, ""):
            return ""
        try:
            timestamp_int = int(timestamp_ms)
        except (TypeError, ValueError):
            return ""
        return datetime.fromtimestamp(timestamp_int / 1000).strftime("%Y-%m-%d")

    @staticmethod
    def _now_string() -> str:
        """
        获取当前时间字符串，统一用于清单记录。

        返回值：
            当前时间，格式为 YYYY-MM-DD HH:MM:SS。
        """
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
