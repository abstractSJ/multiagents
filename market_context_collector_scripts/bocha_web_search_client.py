"""Bocha Web Search 客户端。

本模块只负责把 Bocha Web Search 的 HTTP 响应转换成项目内部统一的搜索结果格式。
它不做投资判断，也不把 API Key 写入任何产物或日志；调用方可以通过环境变量
`BOCHA_WEB_SEARCH_API_KEY`，或本地忽略配置 `collector_workspace/local_config.json` 提供密钥。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_BOCHA_WEB_SEARCH_URL = "https://api.bocha.cn/v1/web-search"
LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / "collector_workspace" / "local_config.json"


@dataclass(frozen=True)
class WebSearchResult:
    """统一后的网页搜索结果。

    参数：
        title: 搜索结果标题。
        url: 搜索结果链接。
        snippet: 搜索结果摘要或片段。
        published_at: 页面发布时间；搜索引擎未返回时为空。
        site_name: 站点名称；搜索引擎未返回时为空。
        raw: 原始结果对象，便于审计和后续兼容不同响应格式。
    返回值：
        dataclass 实例，无额外返回值。
    """

    title: str
    url: str
    snippet: str
    published_at: str
    site_name: str
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。

        参数：
            无。
        返回值：
            搜索结果字典。
        """
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "published_at": self.published_at,
            "site_name": self.site_name,
            "raw": self.raw,
        }


class BochaWebSearchClient:
    """Bocha Web Search HTTP 客户端。

    参数：
        api_key: Bocha API Key；为空时从 `BOCHA_WEB_SEARCH_API_KEY` 读取。
        url: Bocha Web Search URL；为空时从 `BOCHA_WEB_SEARCH_URL` 或默认地址读取。
        timeout: 单次请求超时时间，单位秒。
    返回值：
        客户端实例。
    """

    def __init__(self, api_key: str | None = None, url: str | None = None, timeout: int = 20) -> None:
        local_config = load_local_config()
        self.api_key = api_key or os.environ.get("BOCHA_WEB_SEARCH_API_KEY", "") or get_config_text(
            local_config, ["BOCHA_WEB_SEARCH_API_KEY", "api_key"]
        )
        self.url = (
            url
            or os.environ.get("BOCHA_WEB_SEARCH_URL", "")
            or get_config_text(local_config, ["BOCHA_WEB_SEARCH_URL", "url"])
            or DEFAULT_BOCHA_WEB_SEARCH_URL
        )
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError("缺少 BOCHA_WEB_SEARCH_API_KEY 或本地 local_config.json，无法调用 Bocha Web Search。")

    def search(self, query: str, *, count: int = 10, freshness: str | None = "oneMonth") -> list[dict[str, Any]]:
        """执行一次网页搜索并返回统一结果。

        参数：
            query: 搜索关键词。
            count: 期望返回条数；具体上限由 Bocha 服务端决定。
            freshness: 时效范围，例如 oneDay、oneWeek、oneMonth、oneYear；为 None 时不向服务端发送该字段。
        返回值：
            搜索结果字典列表。
        """
        payload = build_search_payload(query, count=count, freshness=freshness)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            message = _safe_error_body(error)
            raise RuntimeError(f"Bocha Web Search HTTP {error.code}: {message}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Bocha Web Search 网络错误: {error.reason}") from error

        try:
            payload_obj = json.loads(response_body)
        except json.JSONDecodeError as error:
            raise RuntimeError("Bocha Web Search 返回了非 JSON 响应。") from error
        return [result.to_dict() for result in normalize_bocha_response(payload_obj)]


def build_search_payload(query: str, *, count: int, freshness: str | None) -> dict[str, Any]:
    """构造 Bocha Web Search 请求体。

    参数：
        query: 搜索关键词。
        count: 期望返回条数。
        freshness: 时效范围；为 None 时完全省略 freshness 字段。
    返回值：
        可直接 JSON 序列化的请求字典。

    为什么这样做：
        历史 strict-cutoff 查询需要检索较早资料。如果发送默认 oneMonth，服务端会在来源进入
        本地截止日过滤前先丢掉历史结果，因此 None 必须表示“不发送”，而不是发送 JSON null。
    """
    payload: dict[str, Any] = {
        "query": query,
        "count": count,
        # 开启摘要有助于在不抓取正文的 v1 版本中提取市场叙事，但摘要仍只作为弱证据。
        "summary": True,
    }
    if freshness is not None:
        payload["freshness"] = freshness
    return payload


def load_local_config() -> dict[str, Any]:
    """读取本地忽略配置文件。

    参数：
        无。
    返回值：
        配置字典；文件不存在、格式错误或读取失败时返回空字典。
    """
    config_path = Path(os.environ.get("BOCHA_WEB_SEARCH_CONFIG", "") or LOCAL_CONFIG_PATH)
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def get_config_text(config: dict[str, Any], keys: list[str]) -> str:
    """从配置中按候选键读取非空字符串。

    参数：
        config: 配置字典。
        keys: 候选键名。
    返回值：
        第一个非空字符串；没有则返回空字符串。
    """
    for key in keys:
        value = config.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_bocha_response(payload: Any) -> list[WebSearchResult]:
    """归一化 Bocha 或类搜索引擎响应。

    参数：
        payload: HTTP 响应解析后的 JSON 对象。
    返回值：
        统一后的 `WebSearchResult` 列表。
    """
    items = _extract_result_items(payload)
    normalized: list[WebSearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result = _normalize_one_item(item)
        # 没有 URL 的结果无法追溯来源，因此不进入证据表。
        if result.url:
            normalized.append(result)
    return normalized


def _extract_result_items(payload: Any) -> list[Any]:
    """从不同可能的响应结构中提取结果列表。

    参数：
        payload: HTTP 响应 JSON。
    返回值：
        搜索结果原始对象列表。
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    # Bocha 常见结构是 data.webPages.value；这里同时兼容 results/items/value 等变体，
    # 这样后续服务端字段微调时不必立刻改上层采集逻辑。
    candidates: list[Any] = [
        payload.get("results"),
        payload.get("items"),
        payload.get("value"),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("results"), data.get("items"), data.get("value")])
        web_pages = data.get("webPages") or data.get("webpages") or data.get("web_pages")
        if isinstance(web_pages, dict):
            candidates.extend([web_pages.get("value"), web_pages.get("results"), web_pages.get("items")])
    web_pages = payload.get("webPages") or payload.get("webpages") or payload.get("web_pages")
    if isinstance(web_pages, dict):
        candidates.extend([web_pages.get("value"), web_pages.get("results"), web_pages.get("items")])

    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def _normalize_one_item(item: dict[str, Any]) -> WebSearchResult:
    """归一化单条搜索结果。

    参数：
        item: 单条搜索结果原始对象。
    返回值：
        统一后的搜索结果。
    """
    title = _first_text(item, ["title", "name", "displayName"])
    url = _first_text(item, ["url", "link", "displayUrl", "webUrl"])
    snippet = _first_text(item, ["snippet", "summary", "description", "content", "text"])
    published_at = _first_text(item, ["published_at", "publishedAt", "datePublished", "date", "displayDate"])
    site_name = _first_text(item, ["site_name", "siteName", "source", "provider", "host"])
    provider = item.get("provider")
    if not site_name and isinstance(provider, list) and provider:
        first_provider = provider[0]
        if isinstance(first_provider, dict):
            site_name = _first_text(first_provider, ["name", "siteName"])
        else:
            site_name = str(first_provider)
    return WebSearchResult(
        title=title,
        url=url,
        snippet=snippet,
        published_at=published_at,
        site_name=site_name,
        raw=item,
    )


def _first_text(item: dict[str, Any], keys: list[str]) -> str:
    """按候选字段顺序读取第一个非空字符串。

    参数：
        item: 原始对象。
        keys: 候选字段名。
    返回值：
        字符串值；没有可用字段时返回空字符串。
    """
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _safe_error_body(error: urllib.error.HTTPError) -> str:
    """读取 HTTP 错误响应体，避免泄露请求头中的密钥。

    参数：
        error: HTTPError 对象。
    返回值：
        截断后的错误文本。
    """
    try:
        body = error.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    # 错误内容可能很长，截断可以避免污染日志和审计产物。
    return body[:500]
