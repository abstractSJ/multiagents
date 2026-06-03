"""公开网页日级行情交叉抓取脚本。

本脚本用于研究链路中的低成本行情补充：从多个公开网页抓取同一 ticker 的
日级行情字段，并用多源价格偏差校验结果是否适合写入 market_snapshot 产物。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_WORKSPACE = Path(__file__).resolve().parent / "research_workspace"
DEFAULT_SOURCES = ["stockanalysis", "stooq", "cnbc", "google"]


@dataclass
class QuoteSourceResult:
    """单个公开网页行情抓取结果。

    Args:
        source: 来源名称。
        url: 抓取 URL。
        retrieved_at: UTC 抓取时间。
        status: ok 或 failed。
        last_price: 最新价或页面显示价。
        change: 日内涨跌额。
        change_percent: 日内涨跌幅，单位为百分比。
        previous_close: 前收盘价。
        open_price: 开盘价。
        high: 日内最高价。
        low: 日内最低价。
        volume: 成交量。
        market_time: 页面显示的行情时间。
        error_message: 失败原因。
    """

    source: str
    url: str
    retrieved_at: str
    status: str
    last_price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    previous_close: float | None = None
    open_price: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    market_time: str = ""
    error_message: str = ""


class PublicMarketQuoteScraper:
    """多源公开网页行情抓取器。

    Args:
        ticker: 股票代码，例如 MU。
        workspace: 研究工作区根目录。
        user_agent: HTTP 请求 User-Agent。
        timeout: 单个网页请求超时时间。
        max_price_diff_pct: 多源最新价相对中位数的最大容忍偏差。
    """

    def __init__(
        self,
        ticker: str,
        workspace: str | Path,
        user_agent: str,
        timeout: int = 30,
        max_price_diff_pct: float = 1.0,
    ) -> None:
        self.ticker = ticker.upper().strip()
        self.workspace = Path(workspace)
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_price_diff_pct = max_price_diff_pct
        self.parsers: dict[str, Callable[[], QuoteSourceResult]] = {
            "stockanalysis": self._scrape_stockanalysis,
            "stooq": self._scrape_stooq,
            "cnbc": self._scrape_cnbc,
            "google": self._scrape_google,
        }

    def scrape(self, sources: list[str]) -> dict[str, Path]:
        """执行多源行情抓取并写入 market_snapshot 产物。

        Args:
            sources: 需要抓取的来源列表。

        Returns:
            关键产物路径。
        """

        results: list[QuoteSourceResult] = []
        for source in sources:
            parser = self.parsers.get(source)
            if not parser:
                results.append(
                    QuoteSourceResult(
                        source=source,
                        url="",
                        retrieved_at=self._now_iso(),
                        status="failed",
                        error_message=f"未知来源：{source}",
                    )
                )
                continue
            try:
                results.append(parser())
            except Exception as exc:
                results.append(
                    QuoteSourceResult(
                        source=source,
                        url=self._source_url(source),
                        retrieved_at=self._now_iso(),
                        status="failed",
                        error_message=str(exc),
                    )
                )

        snapshot = self._build_snapshot(results)
        target_dir = self.workspace / self.ticker
        target_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = target_dir / "market_snapshot.json"
        audit_path = target_dir / "market_snapshot_audit.json"
        markdown_path = target_dir / "market_snapshot.md"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        audit_path.write_text(json.dumps(snapshot["audit"], ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(self._render_markdown(snapshot), encoding="utf-8")
        return {
            "market_snapshot_path": snapshot_path.resolve(),
            "market_snapshot_audit_path": audit_path.resolve(),
            "market_snapshot_md_path": markdown_path.resolve(),
        }

    def _scrape_stockanalysis(self) -> QuoteSourceResult:
        """抓取 StockAnalysis 行情页。"""

        url = f"https://stockanalysis.com/stocks/{self.ticker.lower()}/"
        raw_html = self._fetch(url)
        text = self._html_to_text(raw_html)
        last_price = self._first_float(r'<div class="[^"]*text-4xl[^"]*"[^>]*>([0-9,.]+)</div>', raw_html)
        day_range = self._label_value_html(raw_html, "Day's Range")
        low, high = self._parse_range(day_range)
        change_match = re.search(r'<div class="[^"]*text-4xl[^"]*"[^>]*>[0-9,.]+</div>\s*<div[^>]*>([+-][0-9,.]+)\s*\(([+-]?[0-9,.]+)%\)</div>', raw_html)
        market_time = self._match_text(r'([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4},\s+[^\n]+?Market\s+(?:open|closed))', text)
        return QuoteSourceResult(
            source="stockanalysis",
            url=url,
            retrieved_at=self._now_iso(),
            status="ok",
            last_price=last_price,
            change=self._to_float(change_match.group(1)) if change_match else None,
            change_percent=self._to_float(change_match.group(2)) if change_match else None,
            previous_close=self._to_float(self._label_value_html(raw_html, "Previous Close")),
            open_price=self._to_float(self._label_value_html(raw_html, "Open")),
            high=high,
            low=low,
            volume=self._to_number_with_suffix(self._label_value_html(raw_html, "Volume")),
            market_time=market_time,
        )

    def _scrape_stooq(self) -> QuoteSourceResult:
        """抓取 Stooq 行情页。"""

        stooq_symbol = f"{self.ticker.lower()}.us"
        url = f"https://stooq.com/q/?s={stooq_symbol}"
        raw_html = self._fetch(url)
        prefix = re.escape(f"aq_{stooq_symbol}_")
        return QuoteSourceResult(
            source="stooq",
            url=url,
            retrieved_at=self._now_iso(),
            status="ok",
            last_price=self._first_float(rf'id={prefix}c4>([0-9,.]+)</span>', raw_html),
            change=self._first_float(rf'id={prefix}m2>([+-]?[0-9,.]+)</span>', raw_html),
            change_percent=self._first_float(rf'id={prefix}m3>\(([+-]?[0-9,.]+)%\)</span>', raw_html),
            previous_close=self._first_float(rf'id={prefix}p>([0-9,.]+)</span>', raw_html),
            open_price=self._first_float(rf'id={prefix}o>([0-9,.]+)</span>', raw_html),
            high=self._first_float(rf'id={prefix}h>([0-9,.]+)</span>', raw_html),
            low=self._first_float(rf'id={prefix}l>([0-9,.]+)</span>', raw_html),
            volume=self._to_number_with_suffix(self._match_text(rf'id={prefix}v2>([^<]+)</span>', raw_html)),
            market_time=" ".join(
                item
                for item in [
                    self._match_text(rf'id={prefix}d2>([^<]+)</span>', raw_html),
                    self._match_text(rf'id={prefix}t1>([^<]+)</span>', raw_html),
                ]
                if item
            ),
        )

    def _scrape_cnbc(self) -> QuoteSourceResult:
        """抓取 CNBC 行情页。"""

        url = f"https://www.cnbc.com/quotes/{self.ticker}"
        raw_html = self._fetch(url)
        return QuoteSourceResult(
            source="cnbc",
            url=url,
            retrieved_at=self._now_iso(),
            status="ok",
            last_price=self._first_float(r'"price":"([0-9,.]+)"', raw_html),
            change=self._first_float(r'"priceChange":"([+-]?[0-9,.]+)"', raw_html),
            change_percent=self._first_float(r'"priceChangePercent":"([+-]?[0-9,.]+)"', raw_html),
            previous_close=self._to_float(self._split_stat(raw_html, "Prev Close")),
            open_price=self._to_float(self._split_stat(raw_html, "Open")),
            high=self._to_float(self._split_stat(raw_html, "Day High")),
            low=self._to_float(self._split_stat(raw_html, "Day Low")),
            volume=self._to_number_with_suffix(self._match_text(r'<div class="QuoteStrip-volume">([^<]+)</div>', raw_html)),
            market_time=self._match_text(r'<span class="QuoteStrip-lastTradeTime">([^<]+)</span>', raw_html),
        )

    def _scrape_google(self) -> QuoteSourceResult:
        """抓取 Google Finance 行情页。

        Google Finance 的静态 HTML 结构会随地区和语言变化；这里仅把它作为价格交叉验证来源，
        不把 OHLC 字段作为硬依赖。
        """

        url = f"https://www.google.com/finance/quote/{self.ticker}:NASDAQ?hl=en"
        raw_html = self._fetch(url, accept_language="en-US,en;q=0.9")
        price = self._first_float(r'\$\s*([0-9]{2,4}\.[0-9]{2})', raw_html)
        return QuoteSourceResult(
            source="google",
            url=url,
            retrieved_at=self._now_iso(),
            status="ok",
            last_price=price,
        )

    def _build_snapshot(self, results: list[QuoteSourceResult]) -> dict[str, object]:
        """构建多源交叉校验快照。"""

        ok_results = [result for result in results if result.status == "ok" and result.last_price is not None]
        prices = [float(result.last_price) for result in ok_results]
        median_price = statistics.median(prices) if prices else None
        deviations = []
        for result in ok_results:
            deviation = abs(float(result.last_price) - median_price) / median_price * 100 if median_price else None
            deviations.append({"source": result.source, "last_price": result.last_price, "deviation_pct": deviation})

        max_deviation_pct = max((item["deviation_pct"] for item in deviations if item["deviation_pct"] is not None), default=None)
        consensus_passed = len(ok_results) >= 2 and max_deviation_pct is not None and max_deviation_pct <= self.max_price_diff_pct
        primary = self._choose_primary(ok_results)
        return {
            "schema_version": "1.0",
            "generated_at": self._now_iso(),
            "ticker": self.ticker,
            "market_data_scope": "public_webpage_cross_check_daily_quote",
            "paid_terminals_used": False,
            "primary_snapshot": asdict(primary) if primary else None,
            "sources": [asdict(result) for result in results],
            "cross_check": {
                "successful_price_source_count": len(ok_results),
                "median_last_price": median_price,
                "max_deviation_pct": max_deviation_pct,
                "max_allowed_deviation_pct": self.max_price_diff_pct,
                "consensus_passed": consensus_passed,
                "deviations": deviations,
            },
            "audit": {
                "status": "pass" if consensus_passed else "needs_review",
                "source_count": len(results),
                "failed_sources": [asdict(result) for result in results if result.status != "ok"],
                "limitations": [
                    "公开网页结构可能变化，抓取失败不等于行情源不存在。",
                    "网页行情可能为实时、近实时或延迟行情；必须保留来源和抓取时间，不能混同为交易所授权实时行情。",
                    "盘中抓取时不同网页刷新时间不同，多源价格允许存在小幅偏差。",
                ],
            },
        }

    def _choose_primary(self, ok_results: list[QuoteSourceResult]) -> QuoteSourceResult | None:
        """选择字段最完整的主快照。"""

        if not ok_results:
            return None
        # Stooq 和 CNBC 的 OHLC 字段更完整，优先选择字段多且可被其他源价格验证的结果。
        return max(
            ok_results,
            key=lambda result: sum(
                value is not None
                for value in [
                    result.last_price,
                    result.previous_close,
                    result.open_price,
                    result.high,
                    result.low,
                    result.volume,
                ]
            ),
        )

    def _fetch(self, url: str, accept_language: str = "en-US,en;q=0.9") -> str:
        """获取网页 HTML。"""

        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
                "Accept-Language": accept_language,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} 获取失败：{url}") from exc
        except URLError as exc:
            raise RuntimeError(f"网络请求失败：{url}；原因：{exc.reason}") from exc

    def _source_url(self, source: str) -> str:
        """返回来源 URL。"""

        return {
            "stockanalysis": f"https://stockanalysis.com/stocks/{self.ticker.lower()}/",
            "stooq": f"https://stooq.com/q/?s={self.ticker.lower()}.us",
            "cnbc": f"https://www.cnbc.com/quotes/{self.ticker}",
            "google": f"https://www.google.com/finance/quote/{self.ticker}:NASDAQ?hl=en",
        }.get(source, "")

    def _label_value_html(self, raw_html: str, label: str) -> str:
        """从 StockAnalysis 表格 HTML 中读取 label 对应值。"""

        pattern = rf'>{re.escape(label)}</td><td[^>]*>(.*?)</td>'
        return self._clean_html(self._match_text(pattern, raw_html))

    def _split_stat(self, raw_html: str, label: str) -> str:
        """从 CNBC SplitStats 结构中读取 label 对应值。"""

        pattern = rf'<span class="SplitStats-name">{re.escape(label)}</span><span class="SplitStats-price">([^<]+)</span>'
        return self._clean_html(self._match_text(pattern, raw_html))

    def _parse_range(self, value: str) -> tuple[float | None, float | None]:
        """解析 low - high 区间。"""

        if not value or "-" not in value:
            return None, None
        left, right = value.split("-", 1)
        return self._to_float(left), self._to_float(right)

    def _match_text(self, pattern: str, text: str) -> str:
        """返回正则第一组文本。"""

        match = re.search(pattern, text, re.S)
        return html.unescape(match.group(1)).strip() if match else ""

    def _first_float(self, pattern: str, text: str) -> float | None:
        """返回正则第一组浮点数。"""

        return self._to_float(self._match_text(pattern, text))

    def _to_float(self, value: object) -> float | None:
        """把金额/百分比文本转为浮点数。"""

        if value in {None, ""}:
            return None
        cleaned = str(value).replace("$", "").replace(",", "").replace("%", "").replace("+", "").strip()
        if not cleaned or cleaned in {"-", "N/A"}:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _to_number_with_suffix(self, value: object) -> float | None:
        """解析带 K/M/B/T/m/g 后缀的数字。"""

        if value in {None, ""}:
            return None
        text = str(value).replace(",", "").replace("$", "").strip()
        match = re.match(r'([+-]?[0-9.]+)\s*([KkMmBbTtGg]?)', text)
        if not match:
            return self._to_float(text)
        number = float(match.group(1))
        suffix = match.group(2).lower()
        multiplier = {
            "k": 1_000,
            "m": 1_000_000,
            "b": 1_000_000_000,
            "g": 1_000_000_000,
            "t": 1_000_000_000_000,
        }.get(suffix, 1)
        return number * multiplier

    def _clean_html(self, value: str) -> str:
        """清理 HTML 片段。"""

        without_tags = re.sub(r'<[^>]+>', '', value)
        return html.unescape(without_tags).strip()

    def _html_to_text(self, raw_html: str) -> str:
        """把 HTML 粗略转成可检索文本。"""

        text = re.sub(r'<script[\s\S]*?</script>', ' ', raw_html, flags=re.I)
        text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.I)
        text = re.sub(r'<[^>]+>', '\n', text)
        text = html.unescape(text)
        return re.sub(r'\n+', '\n', text)

    def _now_iso(self) -> str:
        """返回 UTC ISO 时间。"""

        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _render_markdown(self, snapshot: dict[str, object]) -> str:
        """渲染行情快照 Markdown。"""

        cross_check = snapshot.get("cross_check", {})
        primary = snapshot.get("primary_snapshot") or {}
        lines = [
            f"# {self.ticker} 公开网页行情交叉快照",
            "",
            f"- 生成时间：{snapshot.get('generated_at')}",
            f"- 是否使用付费终端：{snapshot.get('paid_terminals_used')}",
            f"- 价格中位数：{cross_check.get('median_last_price')}",
            f"- 最大价格偏差：{cross_check.get('max_deviation_pct')}",
            f"- 交叉校验通过：{cross_check.get('consensus_passed')}",
            "",
            "## 主快照",
            "",
            f"- 来源：{primary.get('source')}",
            f"- 最新价：{primary.get('last_price')}",
            f"- 前收：{primary.get('previous_close')}",
            f"- 开盘：{primary.get('open_price')}",
            f"- 最高 / 最低：{primary.get('high')} / {primary.get('low')}",
            f"- 成交量：{primary.get('volume')}",
            f"- 页面行情时间：{primary.get('market_time')}",
            "",
            "## 来源明细",
            "",
            "| 来源 | 状态 | 最新价 | 涨跌额 | 涨跌幅% | 开盘 | 最高 | 最低 | 前收 | 成交量 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for result in snapshot.get("sources", []):
            lines.append(
                "| {source} | {status} | {last_price} | {change} | {change_percent} | {open_price} | {high} | {low} | {previous_close} | {volume} |".format(
                    **result
                )
            )
        lines.extend(["", "## 限制", ""])
        for limitation in snapshot.get("audit", {}).get("limitations", []):
            lines.append(f"- {limitation}")
        lines.append("")
        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="从多个公开网页交叉抓取海外股票日级行情")
    parser.add_argument("--ticker", required=True, help="股票代码，例如 MU")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="研究工作区")
    parser.add_argument("--sources", nargs="+", default=DEFAULT_SOURCES, help="网页来源：stockanalysis stooq cnbc google")
    parser.add_argument("--user-agent", default="Mozilla/5.0 multiagents-public-web-quote-research", help="网页请求 User-Agent")
    parser.add_argument("--timeout", type=int, default=30, help="单个网页超时时间")
    parser.add_argument("--max-price-diff-pct", type=float, default=1.0, help="多源价格相对中位数的最大允许偏差百分比")
    return parser


def main() -> None:
    """运行多源网页行情抓取并打印产物路径。"""

    args = build_parser().parse_args()
    scraper = PublicMarketQuoteScraper(
        ticker=args.ticker,
        workspace=args.workspace,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_price_diff_pct=args.max_price_diff_pct,
    )
    paths = scraper.scrape(args.sources)
    print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
