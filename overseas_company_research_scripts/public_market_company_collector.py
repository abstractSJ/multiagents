"""海外上市公司公开源资料采集器。

本模块提供一条独立于 A 股 CNINFO 链路的 SEC 公开源采集能力，
用于把海外公司 ticker 解析为 CIK，并获取 filings、XBRL companyfacts
和可选的 SEC primary filing document。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{accession_no_dash}/{primary_document}"
PUBLIC_FREE_SEC = "public_free_sec"


@dataclass
class SourceRecord:
    """描述一次公开源获取结果。

    Args:
        source_id: 稳定来源编号。
        source_type: 来源类型，例如 sec_companyfacts。
        url: 公开来源 URL。
        retrieved_at: 获取或复用时间。
        local_path: 相对工作区的本地路径。
        license_or_access: 来源访问属性。
        status: ok、existing 或 failed。
        error_message: 失败时的错误信息。
    """

    source_id: str
    source_type: str
    url: str
    retrieved_at: str
    local_path: str
    license_or_access: str = PUBLIC_FREE_SEC
    status: str = "ok"
    error_message: str = ""


@dataclass
class FilingRecord:
    """描述一条 SEC filing 记录及其本地下载状态。

    Args:
        ticker: 股票代码。
        cik: 十位补零 CIK。
        company_name: 公司名称。
        form: 申报表类型，例如 10-K、10-Q、8-K。
        filing_date: filing 提交日期。
        report_date: 报告期日期。
        accession_number: SEC accession number。
        primary_document: primary document 文件名。
        primary_doc_description: primary document 描述。
        filing_url: primary document 公共 URL。
        local_path: 下载后的相对路径。
        download_status: downloaded、existing、not_requested、failed 或 no_primary_document。
        error_message: 下载失败原因。
    """

    ticker: str
    cik: str
    company_name: str
    form: str
    filing_date: str
    report_date: str
    accession_number: str
    primary_document: str
    primary_doc_description: str
    filing_url: str
    local_path: str
    download_status: str = "not_requested"
    error_message: str = ""


class PublicMarketCompanyCollector:
    """SEC 公开源采集器。

    Args:
        workspace: 海外公司研究工作区根目录。
        user_agent: SEC 请求要求的 User-Agent。
        sleep_seconds: SEC 请求之间的间隔秒数。
        timeout: 单次 HTTP 请求超时时间。
    """

    def __init__(self, workspace: str | Path, user_agent: str, sleep_seconds: float = 0.25, timeout: int = 60) -> None:
        self.workspace = Path(workspace)
        self.user_agent = user_agent.strip()
        self.sleep_seconds = sleep_seconds
        self.timeout = timeout
        self.source_records: list[SourceRecord] = []
        self.warnings: list[str] = []
        if not self.user_agent:
            raise ValueError("SEC 请求必须提供 User-Agent，可通过 --user-agent 或 SEC_USER_AGENT 指定。")

    def collect(
        self,
        ticker: str,
        company_name: str | None,
        forms: list[str],
        max_filings_per_form: int,
        download_filings: bool,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """执行 SEC-first 公开源采集。

        Args:
            ticker: 股票代码，例如 MU。
            company_name: 用户指定公司名；为空时使用 SEC ticker mapping 的名称。
            forms: 需要保留的 SEC form 类型。
            max_filings_per_form: 每类 form 最多保留条数。
            download_filings: 是否下载 primary filing document。
            overwrite: 本地已有文件时是否覆盖。

        Returns:
            采集摘要，包含目标公司、清单路径、审计路径和 filing 数量。
        """

        normalized_ticker = ticker.upper().strip()
        target_dir = self.workspace / normalized_ticker
        (target_dir / "raw" / "sec").mkdir(parents=True, exist_ok=True)
        (target_dir / "filings").mkdir(parents=True, exist_ok=True)
        self.source_records = []
        self.warnings = []

        ticker_mapping = self._fetch_json(
            url=SEC_TICKER_URL,
            target_dir=target_dir,
            local_relative_path=Path("raw/sec/company_tickers.json"),
            source_id="sec_company_tickers",
            source_type="sec_ticker_mapping",
            overwrite=overwrite,
        )
        ticker_item = self._resolve_ticker(ticker_mapping, normalized_ticker)
        cik = f"{int(ticker_item['cik_str']):010d}"
        sec_company_name = str(ticker_item.get("title") or company_name or normalized_ticker)
        final_company_name = company_name or sec_company_name

        submissions_url = SEC_SUBMISSIONS_URL.format(cik=cik)
        submissions = self._fetch_json(
            url=submissions_url,
            target_dir=target_dir,
            local_relative_path=Path(f"raw/sec/submissions_CIK{cik}.json"),
            source_id=f"sec_submissions_CIK{cik}",
            source_type="sec_submissions",
            overwrite=overwrite,
        )

        companyfacts_url = SEC_COMPANYFACTS_URL.format(cik=cik)
        companyfacts = self._fetch_json(
            url=companyfacts_url,
            target_dir=target_dir,
            local_relative_path=Path(f"raw/sec/companyfacts_CIK{cik}.json"),
            source_id=f"sec_companyfacts_CIK{cik}",
            source_type="sec_companyfacts",
            overwrite=overwrite,
        )

        filing_records = self._build_filing_records(
            submissions=submissions,
            ticker=normalized_ticker,
            cik=cik,
            company_name=final_company_name,
            forms=forms,
            max_filings_per_form=max_filings_per_form,
        )
        if download_filings:
            self._download_filings(target_dir, filing_records, overwrite=overwrite)

        source_manifest_path = target_dir / "source_manifest.json"
        filing_manifest_path = target_dir / "filing_manifest.json"
        audit_path = target_dir / "collection_audit.json"
        self._write_json(source_manifest_path, [asdict(record) for record in self.source_records])
        self._write_json(filing_manifest_path, [asdict(record) for record in filing_records])
        audit = self._build_audit(
            ticker=normalized_ticker,
            cik=cik,
            company_name=final_company_name,
            filing_records=filing_records,
            download_filings=download_filings,
        )
        self._write_json(audit_path, audit)

        return {
            "ticker": normalized_ticker,
            "company_name": final_company_name,
            "sec_company_name": sec_company_name,
            "cik": cik,
            "companyfacts_entity_name": companyfacts.get("entityName"),
            "target_dir": str(target_dir.resolve()),
            "source_manifest_path": str(source_manifest_path.resolve()),
            "filing_manifest_path": str(filing_manifest_path.resolve()),
            "collection_audit_path": str(audit_path.resolve()),
            "filing_count": len(filing_records),
            "downloaded_filing_count": sum(1 for record in filing_records if record.download_status in {"downloaded", "existing"}),
            "warnings": self.warnings,
        }

    def _fetch_json(
        self,
        url: str,
        target_dir: Path,
        local_relative_path: Path,
        source_id: str,
        source_type: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        """获取 JSON 来源并落盘。

        Args:
            url: 公开源 URL。
            target_dir: 目标公司工作区。
            local_relative_path: 相对目标公司工作区的路径。
            source_id: 来源编号。
            source_type: 来源类型。
            overwrite: 是否覆盖本地已有文件。

        Returns:
            解析后的 JSON 对象。
        """

        output_path = target_dir / local_relative_path
        if output_path.exists() and not overwrite:
            self.source_records.append(
                SourceRecord(
                    source_id=source_id,
                    source_type=source_type,
                    url=url,
                    retrieved_at=self._now_iso(),
                    local_path=local_relative_path.as_posix(),
                    status="existing",
                )
            )
            return json.loads(output_path.read_text(encoding="utf-8"))

        try:
            raw = self._http_get(url)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(raw)
            self.source_records.append(
                SourceRecord(
                    source_id=source_id,
                    source_type=source_type,
                    url=url,
                    retrieved_at=self._now_iso(),
                    local_path=local_relative_path.as_posix(),
                    status="ok",
                )
            )
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self.source_records.append(
                SourceRecord(
                    source_id=source_id,
                    source_type=source_type,
                    url=url,
                    retrieved_at=self._now_iso(),
                    local_path=local_relative_path.as_posix(),
                    status="failed",
                    error_message=str(exc),
                )
            )
            raise

    def _http_get(self, url: str) -> bytes:
        """执行带 SEC User-Agent 的 HTTP GET。

        Args:
            url: 请求地址。

        Returns:
            响应字节内容。
        """

        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "identity",
                "Accept": "application/json,text/html,application/xhtml+xml,text/plain,*/*",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} 获取失败：{url}；响应片段：{body}") from exc
        except URLError as exc:
            raise RuntimeError(f"网络请求失败：{url}；原因：{exc.reason}") from exc

        # SEC 对自动访问有频率约束；即使是免费公开接口，也要主动降速，避免被视为高频抓取。
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return payload

    def _resolve_ticker(self, ticker_mapping: dict[str, Any], ticker: str) -> dict[str, Any]:
        """从 SEC ticker mapping 中解析 ticker。

        Args:
            ticker_mapping: SEC ticker mapping JSON。
            ticker: 目标 ticker。

        Returns:
            匹配到的 SEC 映射记录。
        """

        for item in ticker_mapping.values():
            if str(item.get("ticker", "")).upper() == ticker:
                return item
        raise ValueError(f"SEC company_tickers.json 中未找到 ticker：{ticker}")

    def _build_filing_records(
        self,
        submissions: dict[str, Any],
        ticker: str,
        cik: str,
        company_name: str,
        forms: list[str],
        max_filings_per_form: int,
    ) -> list[FilingRecord]:
        """从 submissions JSON 中构建 filing 清单。

        Args:
            submissions: SEC submissions JSON。
            ticker: 股票代码。
            cik: 十位 CIK。
            company_name: 公司名称。
            forms: 需要筛选的 form 类型。
            max_filings_per_form: 每类 form 保留数量。

        Returns:
            filing 清单。
        """

        recent = submissions.get("filings", {}).get("recent", {})
        accession_numbers = recent.get("accessionNumber") or []
        wanted_forms = {form.upper() for form in forms}
        form_counts = {form: 0 for form in wanted_forms}
        records: list[FilingRecord] = []

        for index, accession_number in enumerate(accession_numbers):
            form = str(self._recent_value(recent, "form", index) or "").upper()
            if form not in wanted_forms:
                continue
            if form_counts.get(form, 0) >= max_filings_per_form:
                continue
            form_counts[form] = form_counts.get(form, 0) + 1
            primary_document = str(self._recent_value(recent, "primaryDocument", index) or "")
            accession_no_dash = accession_number.replace("-", "")
            filing_url = self._build_filing_url(cik, accession_number, primary_document) if primary_document else ""
            local_path = (
                Path("filings") / self._safe_path_segment(form) / accession_no_dash / self._safe_path_segment(primary_document)
                if primary_document
                else Path("")
            )
            records.append(
                FilingRecord(
                    ticker=ticker,
                    cik=cik,
                    company_name=company_name,
                    form=form,
                    filing_date=str(self._recent_value(recent, "filingDate", index) or ""),
                    report_date=str(self._recent_value(recent, "reportDate", index) or ""),
                    accession_number=accession_number,
                    primary_document=primary_document,
                    primary_doc_description=str(self._recent_value(recent, "primaryDocDescription", index) or ""),
                    filing_url=filing_url,
                    local_path=local_path.as_posix(),
                    download_status="not_requested" if primary_document else "no_primary_document",
                )
            )

        missing_forms = [form for form, count in sorted(form_counts.items()) if count == 0]
        if missing_forms:
            self.warnings.append(f"submissions 未找到这些 form 的 recent 记录：{', '.join(missing_forms)}")
        return records

    def _download_filings(self, target_dir: Path, filing_records: list[FilingRecord], overwrite: bool) -> None:
        """下载 filing primary document。

        Args:
            target_dir: 目标公司工作区。
            filing_records: filing 清单。
            overwrite: 本地已有文件时是否覆盖。
        """

        for record in filing_records:
            if not record.filing_url or not record.local_path:
                record.download_status = "no_primary_document"
                continue
            output_path = target_dir / record.local_path
            source_id = f"sec_filing_{record.accession_number.replace('-', '')}_{self._safe_path_segment(record.primary_document)}"
            if output_path.exists() and not overwrite:
                record.download_status = "existing"
                self.source_records.append(
                    SourceRecord(
                        source_id=source_id,
                        source_type="sec_filing_primary_document",
                        url=record.filing_url,
                        retrieved_at=self._now_iso(),
                        local_path=record.local_path,
                        status="existing",
                    )
                )
                continue
            try:
                raw = self._http_get(record.filing_url)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(raw)
                record.download_status = "downloaded"
                self.source_records.append(
                    SourceRecord(
                        source_id=source_id,
                        source_type="sec_filing_primary_document",
                        url=record.filing_url,
                        retrieved_at=self._now_iso(),
                        local_path=record.local_path,
                        status="ok",
                    )
                )
            except Exception as exc:
                record.download_status = "failed"
                record.error_message = str(exc)
                self.source_records.append(
                    SourceRecord(
                        source_id=source_id,
                        source_type="sec_filing_primary_document",
                        url=record.filing_url,
                        retrieved_at=self._now_iso(),
                        local_path=record.local_path,
                        status="failed",
                        error_message=str(exc),
                    )
                )
                self.warnings.append(f"下载 filing 失败：{record.accession_number} {record.primary_document}；{exc}")

    def _build_filing_url(self, cik: str, accession_number: str, primary_document: str) -> str:
        """构造 SEC archive primary document URL。

        Args:
            cik: 十位 CIK。
            accession_number: accession number。
            primary_document: primary document 文件名。

        Returns:
            SEC archive URL。
        """

        return SEC_ARCHIVE_URL.format(
            cik_no_zero=str(int(cik)),
            accession_no_dash=accession_number.replace("-", ""),
            primary_document=quote(primary_document),
        )

    def _build_audit(
        self,
        ticker: str,
        cik: str,
        company_name: str,
        filing_records: list[FilingRecord],
        download_filings: bool,
    ) -> dict[str, Any]:
        """构建采集审计信息。

        Args:
            ticker: 股票代码。
            cik: 十位 CIK。
            company_name: 公司名称。
            filing_records: filing 清单。
            download_filings: 是否请求下载 filing。

        Returns:
            审计字典。
        """

        failed_sources = [record for record in self.source_records if record.status == "failed"]
        succeeded_sources = [record for record in self.source_records if record.status in {"ok", "existing"}]
        if failed_sources and succeeded_sources:
            status = "partial_success"
        elif failed_sources:
            status = "failed"
        elif filing_records:
            status = "success"
        else:
            status = "partial_success"
            self.warnings.append("SEC submissions 可访问，但筛选后的 filing 清单为空。")

        return {
            "schema_version": "1.0",
            "generated_at": self._now_iso(),
            "target": {"ticker": ticker, "cik": cik, "company_name": company_name},
            "status": status,
            "paid_terminals_used": False,
            "download_filings_requested": download_filings,
            "sources_attempted": [record.source_id for record in self.source_records],
            "sources_succeeded": [record.source_id for record in succeeded_sources],
            "sources_failed": [asdict(record) for record in failed_sources],
            "filing_count": len(filing_records),
            "downloaded_filing_count": sum(1 for record in filing_records if record.download_status in {"downloaded", "existing"}),
            "warnings": self.warnings,
            "next_steps": [
                "如需经营变量和前瞻指引，继续读取 10-K/10-Q MD&A、earnings release 和 IR presentation。",
                "如需估值结论，补充免费或自有行情、同业市值倍数和预测假设；本链路不使用付费终端。",
            ],
        }

    def _recent_value(self, recent: dict[str, list[Any]], key: str, index: int) -> Any:
        """安全读取 SEC recent filings 数组字段。"""

        values = recent.get(key) or []
        return values[index] if index < len(values) else None

    def _safe_path_segment(self, value: str) -> str:
        """把外部文件名或 form 转为安全路径片段。"""

        safe = value.replace("/", "_").replace("\\", "_").strip()
        return safe or "unknown"

    def _write_json(self, path: Path, payload: Any) -> None:
        """写入 UTF-8 JSON 文件。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _now_iso(self) -> str:
        """返回 UTC ISO 时间。"""

        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
