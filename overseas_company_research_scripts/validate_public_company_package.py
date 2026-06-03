"""海外上市公司公开源输入包校验脚本。

该脚本对 company_input_package.json 做最低可用性校验，并输出 validation_report.json。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_TOP_LEVEL = [
    "schema_version",
    "generated_at",
    "target",
    "as_of_date",
    "source_policy",
    "filings",
    "financials",
    "market_data",
    "evidence_table",
    "known_gaps",
    "collection_audit_path",
]


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="校验海外上市公司公开源输入包")
    parser.add_argument("--package", required=True, help="company_input_package.json 路径")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    """读取 JSON 文件。

    Args:
        path: JSON 文件路径。

    Returns:
        解析后的 JSON 对象。
    """

    return json.loads(path.read_text(encoding="utf-8"))


def validate(package_path: Path) -> dict[str, Any]:
    """执行最低可用性校验。

    Args:
        package_path: company_input_package.json 路径。

    Returns:
        校验结果。
    """

    errors: list[str] = []
    warnings: list[str] = []
    package = load_json(package_path)
    package_dir = package_path.parent

    for key in REQUIRED_TOP_LEVEL:
        if key not in package:
            errors.append(f"缺少顶层字段：{key}")

    target = package.get("target", {})
    if not target.get("ticker"):
        errors.append("target.ticker 为空")
    if not target.get("cik"):
        errors.append("target.cik 为空")
    if not target.get("company_name"):
        errors.append("target.company_name 为空")

    source_policy = package.get("source_policy", {})
    if source_policy.get("paid_terminals_used") is not False:
        errors.append("source_policy.paid_terminals_used 必须为 false")
    if not source_policy.get("primary_sources"):
        errors.append("source_policy.primary_sources 为空")

    filings = package.get("filings", [])
    if not filings:
        errors.append("filings 为空，未形成 SEC filing manifest")
    else:
        missing_url_count = sum(1 for filing in filings if not filing.get("filing_url"))
        if missing_url_count:
            warnings.append(f"{missing_url_count} 条 filing 缺少 filing_url")
        downloaded_count = sum(1 for filing in filings if filing.get("download_status") in {"downloaded", "existing"})
        if downloaded_count == 0:
            warnings.append("filing manifest 已生成，但没有成功下载 primary document；仍可用 submissions/companyfacts 做基础研究。")

    financials = package.get("financials", {})
    metrics = financials.get("metrics", {})
    if not metrics:
        errors.append("financials.metrics 为空")
    available_metric_count = 0
    for metric_name, payload in metrics.items():
        if payload.get("status") == "available":
            available_metric_count += 1
        for fact_key in ["latest_annual", "latest_quarterly"]:
            fact = payload.get(fact_key)
            if not fact:
                continue
            for required_fact_key in ["value", "unit", "source_concept", "source_url", "form", "filed"]:
                if fact.get(required_fact_key) in {None, ""}:
                    errors.append(f"{metric_name}.{fact_key} 缺少字段：{required_fact_key}")
    if available_metric_count < 5:
        warnings.append("可用核心财务指标少于 5 个，当前只能作为低置信财务输入包。")

    market_data = package.get("market_data", {})
    market_snapshot_status = market_data.get("status")
    if market_snapshot_status == "available":
        if market_data.get("current_price") in {None, ""}:
            errors.append("market_data.status 为 available，但 current_price 为空")
        if market_data.get("consensus_passed") is not True:
            errors.append("market_data.status 为 available，但 consensus_passed 不是 true")
        snapshot_path = market_data.get("snapshot_path")
        if snapshot_path and not (package_dir / snapshot_path).exists():
            errors.append(f"market_data.snapshot_path 指向的文件不存在：{snapshot_path}")
    elif market_snapshot_status == "needs_review":
        warnings.append("公开网页行情快照已生成但需要人工复核；估值时不要直接使用单点价格。")
    else:
        warnings.append("未生成公开网页行情快照；公司研究链路缺少当前价格输入。")

    evidence_table = package.get("evidence_table", [])
    if not evidence_table:
        errors.append("evidence_table 为空")
    else:
        missing_source_refs = [item.get("ref_id") for item in evidence_table if not item.get("source_url") and item.get("evidence_type") != "sec_filing"]
        if missing_source_refs:
            warnings.append("部分证据缺少 source_url：" + ", ".join(str(item) for item in missing_source_refs[:10]))

    collection_audit_path = package.get("collection_audit_path")
    if collection_audit_path:
        resolved_audit_path = package_dir / collection_audit_path
        if not resolved_audit_path.exists():
            errors.append(f"collection_audit_path 指向的文件不存在：{collection_audit_path}")
    if not package.get("known_gaps"):
        warnings.append("known_gaps 为空；公开源链路应显式披露未接入付费终端、行情和共识预期等限制。")

    warnings.extend(
        [
            "v1 未校验公司 IR、电话会文本或 investor presentation；这些是经营变量和前瞻指引的后续补证来源。",
            "当前行情来自公开网页交叉抓取，可作为内部投研估值输入；不能等同于交易所授权实时行情或对外分发行情。",
        ]
    )
    validation_status = "fail" if errors else "pass_with_warnings" if warnings else "pass"
    result = {
        "schema_version": "1.0",
        "validated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "package_path": str(package_path.resolve()),
        "validation_status": validation_status,
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "ticker": target.get("ticker"),
            "cik": target.get("cik"),
            "company_name": target.get("company_name"),
            "filing_count": len(filings),
            "available_metric_count": available_metric_count,
            "evidence_item_count": len(evidence_table),
            "known_gap_count": len(package.get("known_gaps", [])),
            "market_snapshot_status": market_snapshot_status,
            "current_price": market_data.get("current_price"),
            "market_consensus_passed": market_data.get("consensus_passed"),
            "paid_terminals_used": source_policy.get("paid_terminals_used"),
        },
    }
    output_path = package_path.with_name("validation_report.json")
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    """运行校验并打印 JSON 结果。"""

    args = parse_args()
    result = validate(Path(args.package))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
