"""海外上市公司公开源研究输入包命令行入口。

该脚本串联 SEC 公开源采集器和输入包构建器，生成独立于 A 股 CNINFO 链路的
海外公司研究资料包。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from public_market_company_collector import PublicMarketCompanyCollector
from public_market_package_builder import PublicMarketPackageBuilder
from public_market_quote_scraper import DEFAULT_SOURCES as DEFAULT_QUOTE_SOURCES
from public_market_quote_scraper import PublicMarketQuoteScraper


DEFAULT_WORKSPACE = Path(__file__).resolve().parent / "research_workspace"


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns:
        配置完成的 ArgumentParser。
    """

    parser = argparse.ArgumentParser(description="从 SEC 免费公开源生成海外上市公司研究输入包")
    parser.add_argument("--ticker", required=True, help="海外股票 ticker，例如 MU")
    parser.add_argument("--company-name", help="公司名称；不传时使用 SEC ticker mapping 的名称")
    parser.add_argument("--market", default="NASDAQ", help="上市市场，默认 NASDAQ")
    parser.add_argument("--forms", nargs="+", default=["10-K", "10-Q", "8-K"], help="需要纳入 filing manifest 的 SEC forms")
    parser.add_argument("--max-filings-per-form", type=int, default=3, help="每类 form 最多保留多少条 recent filing")
    parser.add_argument("--download-filings", action="store_true", help="是否下载 SEC primary filing document HTML")
    parser.add_argument("--overwrite", action="store_true", help="本地已有原始文件时是否覆盖重新抓取")
    parser.add_argument("--as-of-date", required=True, help="信息包日期，格式 YYYY-MM-DD")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="海外公司公开源研究工作区")
    parser.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"), help="SEC 请求 User-Agent；SEC 建议包含真实联系邮箱，也可用 SEC_USER_AGENT 环境变量提供")
    parser.add_argument("--sleep-seconds", type=float, default=0.25, help="SEC 请求间隔秒数")
    parser.add_argument("--skip-market-snapshot", action="store_true", help="跳过公开网页日级行情交叉抓取；默认会生成 market_snapshot")
    parser.add_argument("--quote-sources", nargs="+", default=DEFAULT_QUOTE_SOURCES, help="行情网页来源，默认 stockanalysis stooq cnbc google")
    parser.add_argument("--quote-user-agent", default="Mozilla/5.0 multiagents-public-web-quote-research", help="行情网页抓取 User-Agent")
    parser.add_argument("--max-price-diff-pct", type=float, default=1.0, help="多源行情最新价相对中位数的最大允许偏差百分比")
    return parser


def main() -> None:
    """执行采集、构建输入包并打印 JSON 摘要。"""

    parser = build_parser()
    args = parser.parse_args()
    if not args.user_agent:
        parser.error("请通过 --user-agent 或 SEC_USER_AGENT 提供 SEC User-Agent，建议包含真实联系邮箱。")

    collector = PublicMarketCompanyCollector(
        workspace=args.workspace,
        user_agent=args.user_agent,
        sleep_seconds=args.sleep_seconds,
    )
    collection_result = collector.collect(
        ticker=args.ticker,
        company_name=args.company_name,
        forms=args.forms,
        max_filings_per_form=args.max_filings_per_form,
        download_filings=args.download_filings,
        overwrite=args.overwrite,
    )

    market_snapshot_result = None
    if not args.skip_market_snapshot:
        quote_scraper = PublicMarketQuoteScraper(
            ticker=collection_result["ticker"],
            workspace=args.workspace,
            user_agent=args.quote_user_agent,
            max_price_diff_pct=args.max_price_diff_pct,
        )
        market_snapshot_result = {
            key: str(path)
            for key, path in quote_scraper.scrape(args.quote_sources).items()
        }

    builder = PublicMarketPackageBuilder(target_dir=Path(collection_result["target_dir"]))
    package_result = builder.build(
        ticker=collection_result["ticker"],
        company_name=collection_result["company_name"],
        cik=collection_result["cik"],
        as_of_date=args.as_of_date,
        market=args.market,
    )

    print(
        json.dumps(
            {
                "status": "completed",
                "paid_terminals_used": False,
                "collection": collection_result,
                "market_snapshot": market_snapshot_result,
                "package": package_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
