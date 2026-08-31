"""Headless-browser tools, for sites that need JavaScript or have no usable API.

Two levels, deliberately separated:

* ``browser_read`` renders a page and returns its text. Read-only.
* ``browser_interact`` clicks, types and submits. This acts on real sites under
  your identity, so it stays off until ``BROWSER_ACTIONS_ENABLED=true``.

Requires the optional extra: ``pip install -e ".[browser]" && playwright install chromium``.
"""

from __future__ import annotations

import json
from typing import Any

from autonomous.config import Settings
from autonomous.tools.base import Tool
from autonomous.tools.web import html_to_text

_INSTALL_HINT = (
    'Playwright is not installed. Run: pip install -e ".[browser]" && playwright install chromium'
)


async def _render(url: str, actions: list[dict[str, Any]] | None, settings: Settings) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return f"Error: {_INSTALL_HINT}"

    timeout_ms = int(settings.http_timeout_seconds * 1000)
    log: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(user_agent=settings.user_agent)
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            for step in actions or []:
                action = str(step.get("action", "")).lower()
                selector = step.get("selector", "")
                value = step.get("value", "")
                if action == "click":
                    await page.click(selector, timeout=timeout_ms)
                elif action == "fill":
                    await page.fill(selector, value, timeout=timeout_ms)
                elif action == "press":
                    await page.press(selector or "body", value or "Enter", timeout=timeout_ms)
                elif action == "wait_for":
                    await page.wait_for_selector(selector, timeout=timeout_ms)
                else:
                    log.append(f"skipped unknown action {action!r}")
                    continue
                log.append(f"{action} {selector} {value}".strip())
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            html = await page.content()
            final_url = page.url
        finally:
            await browser.close()

    prefix = f"URL: {final_url}\n"
    if log:
        prefix += "Actions performed:\n" + "\n".join(f"- {line}" for line in log) + "\n"
    return prefix + "\n" + html_to_text(html)


def build_browser_tools(settings: Settings) -> list[Tool]:
    if not settings.browser_enabled:
        return []

    tools = [
        Tool(
            name="browser_read",
            description=(
                "Open a URL in a headless browser, run its JavaScript, and return the "
                "rendered page text. Use when fetch_url returns an empty or app-shell page."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            fn=lambda url: _render(url, None, settings),
        )
    ]

    if settings.browser_actions_enabled:
        tools.append(
            Tool(
                name="browser_interact",
                description=(
                    "Open a URL and perform a short sequence of browser actions "
                    "(click, fill, press, wait_for), then return the resulting page text. "
                    "Only for sites with no usable API."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "description": "Ordered steps to perform after the page loads.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["click", "fill", "press", "wait_for"],
                                    },
                                    "selector": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                                "required": ["action"],
                            },
                        },
                    },
                    "required": ["url", "actions"],
                },
                fn=lambda url, actions=None: _render(
                    url,
                    actions if isinstance(actions, list) else json.loads(actions or "[]"),
                    settings,
                ),
                mutating=True,
            )
        )
    return tools
