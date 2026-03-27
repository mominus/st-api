"""
Web search fallback service for Claude Code style search prompts.

When Claude Code sends:
    "Perform a web search for the query: <query>"
this service executes a lightweight web search and returns condensed text
results so tool_result payloads contain actionable links instead of placeholders.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)


_LEGACY_WEB_SEARCH_PREFIX_RE = re.compile(
    r"^\s*(?:[-*]\s*)?perform a web search for the query:\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def extract_legacy_web_search_query(text: str) -> Optional[str]:
    """Extract query from legacy Claude Code search prompt text."""
    if not text:
        return None

    match = _LEGACY_WEB_SEARCH_PREFIX_RE.match(text.strip())
    if not match:
        return None

    query = (match.group(1) or "").strip()
    if not query:
        return None

    if len(query) >= 2 and query[0] == query[-1] and query[0] in {"'", '"'}:
        query = query[1:-1].strip()

    return query or None


@dataclass
class SearchResult:
    title: str
    url: str
    source: str = ""


class WebSearchFallbackService:
    """Execute lightweight public-web search without external API keys."""

    async def search(self, query: str, max_results: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            return "No query provided."

        results = await self._search_google_news_rss(query=query, max_results=max_results)
        if not results:
            results = await self._search_duckduckgo_instant(query=query, max_results=max_results)

        if not results:
            return "No relevant web results were found for this query."

        lines: List[str] = []
        for idx, item in enumerate(results[:max_results], start=1):
            title = item.title.strip() or "Untitled"
            url = item.url.strip()
            if not url:
                continue
            if item.source:
                lines.append(f"{idx}. {title} ({item.source}) - {url}")
            else:
                lines.append(f"{idx}. {title} - {url}")

        if not lines:
            return "No relevant web results were found for this query."
        return "\n".join(lines)

    async def _search_google_news_rss(self, query: str, max_results: int) -> List[SearchResult]:
        is_cjk = sum(1 for ch in query if "\u4e00" <= ch <= "\u9fff") >= 2
        if is_cjk:
            hl = "zh-CN"
            gl = "CN"
            ceid = "CN:zh-Hans"
        else:
            hl = "en-US"
            gl = "US"
            ceid = "US:en"

        url = (
            "https://news.google.com/rss/search"
            f"?q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"
        )

        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except Exception as exc:
            logger.debug("Google News RSS search failed: %s", exc)
            return []

        try:
            root = ET.fromstring(resp.text)
        except Exception as exc:
            logger.debug("Google News RSS parse failed: %s", exc)
            return []

        results: List[SearchResult] = []
        for item in root.findall(".//item"):
            title = html.unescape((item.findtext("title") or "").strip())
            link = (item.findtext("link") or "").strip()
            source = html.unescape((item.findtext("source") or "").strip())
            if title and link:
                results.append(SearchResult(title=title, url=link, source=source))
            if len(results) >= max_results:
                break
        return results

    async def _search_duckduckgo_instant(self, query: str, max_results: int) -> List[SearchResult]:
        url = "https://api.duckduckgo.com/"
        params = {
            "q": query,
            "format": "json",
            "no_redirect": "1",
            "no_html": "1",
        }

        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.debug("DuckDuckGo instant search failed: %s", exc)
            return []

        results: List[SearchResult] = []

        abstract = str(data.get("AbstractText") or "").strip()
        abstract_url = str(data.get("AbstractURL") or "").strip()
        heading = str(data.get("Heading") or "").strip() or "DuckDuckGo Summary"
        if abstract and abstract_url:
            results.append(SearchResult(title=f"{heading}: {abstract}", url=abstract_url, source="DuckDuckGo"))

        def _consume_topics(topics):
            for item in topics or []:
                if len(results) >= max_results:
                    return
                if not isinstance(item, dict):
                    continue
                if "Topics" in item and isinstance(item.get("Topics"), list):
                    _consume_topics(item.get("Topics"))
                    continue
                text = str(item.get("Text") or "").strip()
                first_url = str(item.get("FirstURL") or "").strip()
                if text and first_url:
                    results.append(SearchResult(title=text, url=first_url, source="DuckDuckGo"))

        _consume_topics(data.get("RelatedTopics"))
        return results[:max_results]


_web_search_fallback_service: Optional[WebSearchFallbackService] = None


def get_web_search_fallback_service() -> WebSearchFallbackService:
    global _web_search_fallback_service
    if _web_search_fallback_service is None:
        _web_search_fallback_service = WebSearchFallbackService()
    return _web_search_fallback_service

