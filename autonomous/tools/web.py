"""Reading the live web: fetch a page as text, and read RSS/Atom feeds."""

from __future__ import annotations

import asyncio

import httpx

from autonomous.config import Settings
from autonomous.tools.base import Tool

_STRIP_TAGS = ("script", "style", "noscript", "template", "svg")


def html_to_text(html: str, limit: int = 20_000) -> str:
    """Reduce a page to readable text. Cheap, dependency-light, good enough to read."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [line for line in (ln.strip() for ln in text.splitlines()) if line]
    out = "\n".join(lines)
    return out[:limit] + ("\n...[truncated]" if len(out) > limit else "")


async def fetch_url(settings: Settings, url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return f"Error: only http(s) URLs are supported, got {url!r}"
    headers = {"User-Agent": settings.user_agent}
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, follow_redirects=True, headers=headers
    ) as client:
        response = await client.get(url)
    content_type = response.headers.get("content-type", "")
    body = response.content[: settings.http_max_bytes]
    header = f"HTTP {response.status_code} {response.url}\ncontent-type: {content_type}\n\n"
    if "html" in content_type:
        return header + html_to_text(body.decode(response.encoding or "utf-8", errors="replace"))
    return header + body.decode(response.encoding or "utf-8", errors="replace")[:20_000]


async def read_feed(settings: Settings, url: str, limit: int = 10) -> str:
    import feedparser

    raw = await fetch_url_raw(settings, url)
    # feedparser is synchronous and CPU-bound; keep it off the event loop.
    parsed = await asyncio.to_thread(feedparser.parse, raw)
    if not parsed.entries:
        return f"No entries found in feed {url}"
    lines = [f"Feed: {parsed.feed.get('title', url)}"]
    for entry in parsed.entries[:limit]:
        published = entry.get("published") or entry.get("updated") or ""
        lines.append(f"- {entry.get('title', '(untitled)')} [{published}] {entry.get('link', '')}")
    return "\n".join(lines)


async def fetch_url_raw(settings: Settings, url: str) -> bytes:
    headers = {"User-Agent": settings.user_agent}
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, follow_redirects=True, headers=headers
    ) as client:
        response = await client.get(url)
    return response.content[: settings.http_max_bytes]


def build_web_tools(settings: Settings) -> list[Tool]:
    return [
        Tool(
            name="fetch_url",
            description=(
                "Fetch an http(s) URL and return its readable text. Use this to read web "
                "pages, JSON APIs and documents."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "The absolute URL."}},
                "required": ["url"],
            },
            fn=lambda url: fetch_url(settings, url),
        ),
        Tool(
            name="read_feed",
            description="Read an RSS or Atom feed and list its most recent entries.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Feed URL."},
                    "limit": {"type": "integer", "description": "Max entries (default 10)."},
                },
                "required": ["url"],
            },
            fn=lambda url, limit=10: read_feed(settings, url, limit),
        ),
    ]
