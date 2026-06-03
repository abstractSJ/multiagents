"""
A 股年报覆盖核验脚本。

该脚本负责把“manifest 里没看到年报”拆解为可审计状态，避免把采集缺口直接误判为公司未披露。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cninfo_financial_report_collector import CninfoFinancialReportCollector, ReportRecord

DEFAULT_WORKSPACE = Path(__file__).resolve().parent / "collector_workspace"
DEFAULT_MANIFEST = DEFAULT_WORKSPACE / "manifests" / "cninfo_all_reports.json"
DEFAULT_AUDIT_DIR = DEFAULT_WORKSPACE / "audits"
DEFAULT_REFERENCE_DIR = DEFAULT_WORKSPACE / "reference"
DEFAULT_UNIVERSE = DEFAULT_REFERENCE_DIR / "stock_universe.csv"
ANNUAL_TITLE_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)(?:\s*年(?:度)?\s*|[\s_\-]*)?(?:年度报告|年报)")


@dataclass
class StockUniverseItem:
    """
    股票 universe 中的一家公司。

    参数：
        stock_code: 证券代码。
        company_name: 公司简称。
        exchange: 交易所粗分类。
        security_category: 证券类别。
        listing_status: 上市状态。
        list_date: 上市日期。
        delist_date: 退市日期。
        expected_annual_report_required: 是否纳入目标财年年报应披露核验。
    """

    stock_code: str = ""
    company_name: str = ""
    exchange: str = "unknown"
    security_category: str = "unknown"
    listing_status: str = "listed"
    list_date: str = ""
    delist_date: str = ""
    expected_annual_report_required: str = "unknown"


@dataclass
class AnnualCoverageAuditRow:
    """
    公司级年报覆盖核验结果。

    参数：
        stock_code: 证券代码。
        company_name: 公司简称。
        security_category: 证券类别。
        exchange: 交易所粗分类。
        target_fiscal_year: 目标财年。
        expected_to_disclose_by_deadline: 是否预期应在截止日前披露。
        full_report_found: 是否找到正式年报。
        summary_only_found: 是否只找到摘要。
        english_only_found: 是否只找到英文版。
        delay_notice_found: 是否找到延期披露公告。
        parse_error_suspected: 是否存在标题与字段冲突。
        cninfo_query_status: 巨潮当前清单层面的状态。
        authority_check_status: 权威渠道复核状态。
        final_status: 最终审计状态。
        evidence_announcement_ids: 证据公告编号。
        evidence_source_pdf_urls: 证据 PDF 链接。
        notes: 审计备注。
    """

    stock_code: str
    company_name: str
    security_category: str
    exchange: str
    target_fiscal_year: str
    expected_to_disclose_by_deadline: str
    full_report_found: bool
    summary_only_found: bool
    english_only_found: bool
    delay_notice_found: bool
    parse_error_suspected: bool
    cninfo_query_status: str
    authority_check_status: str
    final_status: str
    evidence_announcement_ids: str
    evidence_source_pdf_urls: str
    notes: str = ""
    evidence_titles: list[str] = field(default_factory=list)


def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    返回值：
        配置完成的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(description="审计 A 股目标财年年报覆盖情况。")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="总 JSON 清单路径。")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE), help="股票 universe CSV 路径。")
    parser.add_argument("--fiscal-year", required=True, help="目标财年，例如 2025。")
    parser.add_argument("--deadline", required=True, help="披露截止日，例如 2026-04-30。")
    parser.add_argument("--output-json", default="", help="审计 JSON 输出路径。")
    parser.add_argument("--output-csv", default="", help="审计 CSV 输出路径。")
    parser.add_argument(
        "--build-universe-from-manifest",
        action="store_true",
        help="当 universe 不存在时，从现有 manifest 派生初版 universe。",
    )
    return parser


def load_manifest(manifest_path: str | Path) -> list[ReportRecord]:
    """
    加载并规范化总清单。

    参数：
        manifest_path: 总清单路径。

    返回值：
        兼容旧字段后的 ReportRecord 列表。
    """
    path = Path(manifest_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    collector = CninfoFinancialReportCollector(workspace=DEFAULT_WORKSPACE)
    records = [ReportRecord.from_dict(item) for item in payload]
    collector._normalize_records(records, migrate_files=False)
    return records


def infer_exchange(stock_code: str) -> str:
    """
    根据代码前缀推断交易所。

    参数：
        stock_code: 证券代码。

    返回值：
        sse、szse、bse 或 unknown。
    """
    code = str(stock_code or "").strip()
    if code.startswith(("6", "900")):
        return "sse"
    if code.startswith(("0", "2", "3")):
        return "szse"
    if code.startswith(("83", "87", "88", "92")):
        return "bse"
    return "unknown"


def build_universe_from_manifest(records: list[ReportRecord], universe_path: str | Path) -> list[StockUniverseItem]:
    """
    从当前 manifest 派生初版 universe。

    参数：
        records: 总清单记录。
        universe_path: universe 输出路径。

    返回值：
        universe 条目列表。
    """
    latest_by_code: dict[str, StockUniverseItem] = {}
    for record in records:
        if not record.stock_code:
            continue
        existing = latest_by_code.get(record.stock_code)
        if existing and existing.company_name:
            continue
        latest_by_code[record.stock_code] = StockUniverseItem(
            stock_code=record.stock_code,
            company_name=record.company_name,
            exchange=infer_exchange(record.stock_code),
            security_category=record.security_category,
            listing_status="listed",
            expected_annual_report_required="unknown",
        )

    universe = sorted(latest_by_code.values(), key=lambda item: item.stock_code)
    path = Path(universe_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        fieldnames = list(StockUniverseItem.__dataclass_fields__.keys())
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for item in universe:
            writer.writerow(asdict(item))
    return universe


def load_universe(universe_path: str | Path) -> list[StockUniverseItem]:
    """
    加载股票 universe。

    参数：
        universe_path: universe CSV 路径。

    返回值：
        universe 条目列表。
    """
    path = Path(universe_path)
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        return [StockUniverseItem(**{key: row.get(key, "") for key in StockUniverseItem.__dataclass_fields__}) for row in reader]


def title_mentions_target_annual(title: str, fiscal_year: str) -> bool:
    """
    判断标题是否提及目标财年的年报。

    参数：
        title: 公告标题。
        fiscal_year: 目标财年。

    返回值：
        如果标题提及目标财年年报则为 True。
    """
    matched = ANNUAL_TITLE_PATTERN.search(title or "")
    return bool(matched and matched.group(1) == fiscal_year)


def is_target_annual_evidence(record: ReportRecord, fiscal_year: str) -> bool:
    """
    判断一条记录是否与目标财年年报核验相关。

    参数：
        record: 财报记录。
        fiscal_year: 目标财年。

    返回值：
        如果该记录可作为目标财年年报证据则为 True。
    """
    if record.report_type == "annual" and record.report_year == fiscal_year:
        return True
    if title_mentions_target_annual(record.title, fiscal_year):
        return True
    return record.title_classification == "annual_delay_notice" and fiscal_year in record.title


def classify_company(
    *,
    universe_item: StockUniverseItem,
    records: list[ReportRecord],
    fiscal_year: str,
) -> AnnualCoverageAuditRow:
    """
    对单家公司生成目标财年年报覆盖状态。

    参数：
        universe_item: universe 中的公司。
        records: 该公司的相关公告记录。
        fiscal_year: 目标财年。

    返回值：
        公司级审计结果。
    """
    evidence_records = [record for record in records if is_target_annual_evidence(record, fiscal_year)]
    full_records = [record for record in evidence_records if record.title_classification in {"annual_full", "annual_revision"} and record.record_kind == "report"]
    summary_records = [record for record in evidence_records if record.title_classification in {"annual_summary", "annual_english_summary"}]
    english_records = [record for record in evidence_records if record.title_classification in {"annual_english_full", "annual_english_summary"}]
    delay_records = [record for record in evidence_records if record.record_kind == "delay_notice"]
    parse_error_records = [
        record
        for record in records
        if title_mentions_target_annual(record.title, fiscal_year) and record.report_year != fiscal_year
    ]
    parse_error_records.extend(
        record
        for record in records
        if record.report_type == "annual"
        and record.report_year != fiscal_year
        and record.title_classification in {
            "annual_full",
            "annual_summary",
            "annual_english_full",
            "annual_english_summary",
            "annual_revision",
        }
        and "year_parse" in record.collection_warning
        and record not in parse_error_records
    )

    full_report_found = bool(full_records)
    summary_only_found = bool(summary_records and not full_records)
    english_only_found = bool(english_records and not full_records)
    delay_notice_found = bool(delay_records)
    parse_error_suspected = bool(parse_error_records)

    if full_report_found:
        final_status = "disclosed_full_report"
        cninfo_status = "found_full_report"
    elif delay_notice_found:
        final_status = "delay_notice_found"
        cninfo_status = "found_delay_notice"
    elif parse_error_suspected:
        final_status = "parse_error_suspected"
        cninfo_status = "found_title_but_field_conflict"
    elif english_only_found:
        final_status = "disclosed_english_only"
        cninfo_status = "found_english_only"
    elif summary_only_found:
        final_status = "disclosed_summary_only"
        cninfo_status = "found_summary_only"
    elif universe_item.security_category in {"b_share", "beijing_exchange"}:
        final_status = "special_security_category"
        cninfo_status = "not_found_on_cninfo_query"
    else:
        final_status = "not_found_on_cninfo_query"
        cninfo_status = "not_found_on_cninfo_query"

    evidence_pool = evidence_records or parse_error_records
    evidence_ids = ";".join(record.announcement_id for record in evidence_pool if record.announcement_id)
    evidence_urls = ";".join(record.source_pdf_url for record in evidence_pool if record.source_pdf_url)
    evidence_titles = [record.title for record in evidence_pool if record.title]

    notes = ""
    if final_status != "disclosed_full_report":
        notes = "该状态不是权威未披露结论，只表示当前 manifest 与规则初筛下需要复核。"

    return AnnualCoverageAuditRow(
        stock_code=universe_item.stock_code,
        company_name=universe_item.company_name,
        security_category=universe_item.security_category,
        exchange=universe_item.exchange,
        target_fiscal_year=fiscal_year,
        expected_to_disclose_by_deadline=universe_item.expected_annual_report_required,
        full_report_found=full_report_found,
        summary_only_found=summary_only_found,
        english_only_found=english_only_found,
        delay_notice_found=delay_notice_found,
        parse_error_suspected=parse_error_suspected,
        cninfo_query_status=cninfo_status,
        authority_check_status="not_checked",
        final_status=final_status,
        evidence_announcement_ids=evidence_ids,
        evidence_source_pdf_urls=evidence_urls,
        notes=notes,
        evidence_titles=evidence_titles,
    )


def write_audit_outputs(rows: list[AnnualCoverageAuditRow], output_json: str | Path, output_csv: str | Path) -> None:
    """
    写出 JSON 与 CSV 审计结果。

    参数：
        rows: 公司级审计结果。
        output_json: JSON 输出路径。
        output_csv: CSV 输出路径。

    返回值：
        无。
    """
    json_path = Path(output_json)
    csv_path = Path(output_csv)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    json_payload = [asdict(row) for row in rows]
    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_fieldnames = [field_name for field_name in AnnualCoverageAuditRow.__dataclass_fields__ if field_name != "evidence_titles"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_fieldnames)
        writer.writeheader()
        for row in rows:
            payload = asdict(row)
            payload.pop("evidence_titles", None)
            writer.writerow(payload)


def main() -> None:
    """
    命令行主入口。

    返回值：
        无。
    """
    args = build_parser().parse_args()
    records = load_manifest(args.manifest)
    universe_path = Path(args.universe)
    if not universe_path.exists():
        if not args.build_universe_from_manifest:
            raise FileNotFoundError(
                f"universe 不存在: {universe_path}。如需用现有 manifest 派生初版 universe，请加 --build-universe-from-manifest。"
            )
        universe = build_universe_from_manifest(records, universe_path)
    else:
        universe = load_universe(universe_path)

    records_by_code: dict[str, list[ReportRecord]] = defaultdict(list)
    for record in records:
        records_by_code[record.stock_code].append(record)

    rows = [
        classify_company(
            universe_item=item,
            records=records_by_code.get(item.stock_code, []),
            fiscal_year=args.fiscal_year,
        )
        for item in universe
    ]
    rows.sort(key=lambda row: (row.final_status, row.stock_code))

    output_json = args.output_json or DEFAULT_AUDIT_DIR / f"annual_{args.fiscal_year}_coverage_audit.json"
    output_csv = args.output_csv or DEFAULT_AUDIT_DIR / f"annual_{args.fiscal_year}_coverage_audit.csv"
    write_audit_outputs(rows, output_json, output_csv)

    status_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        status_counts[row.final_status] += 1

    print(f"核验完成，目标财年 {args.fiscal_year}，披露截止日 {args.deadline}。")
    print(f"universe 公司数: {len(universe)}")
    print(f"审计 JSON: {output_json}")
    print(f"审计 CSV: {output_csv}")
    print("状态统计：")
    for status, count in sorted(status_counts.items()):
        print(f"- {status}: {count}")
    print("说明：除 authority_confirmed_missing 外，其它异常状态均不能直接表述为确认未披露。")


if __name__ == "__main__":
    main()
