"""Financial markets watcher: index/FX/commodity levels plus market headlines.

Both halves are keyless. Quotes come from Yahoo Finance's chart endpoint, one
observation per symbol per market timestamp - so the feed keeps a history and
the panel can show the latest. Headlines come from the finance feeds in
MARKETS_FEED_URLS.

Observation ``data.kind`` is "quote" or "headline", which is how the panel
splits them.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from autonomous.config import Settings
from autonomous.errors import describe
from autonomous.watchers.base import Observation, Watcher
from autonomous.watchers.feeds import fetch_feed

QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

SPARKLINE_POINTS = 24

log = logging.getLogger(__name__)


def _closes(result: dict) -> list[float]:
    """Daily closing prices, gaps (holidays, halts) dropped."""
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    return [c for c in (quote.get("close") or []) if c is not None]


def _clean_name(value: str) -> str:
    """Yahoo pads some names ("EURO STOXX 50      I"); collapse the whitespace."""
    return " ".join(value.split())


def _format_change(price: float | None, previous: float | None) -> tuple[float | None, str]:
    if price is None or not previous:
        return None, "n/a"
    # Rounded at source: this value is displayed, and substituted into rule
    # goals, where full float precision is noise.
    change = round((price - previous) / previous * 100, 2)
    return change, f"{change:+.2f}%"


class MarketsWatcher(Watcher):
    name = "markets"

    def __init__(self, settings: Settings, headlines_per_feed: int = 12) -> None:
        self.settings = settings
        self.headlines_per_feed = headlines_per_feed

    @property
    def interval_seconds(self) -> int:
        return self.settings.markets_poll_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.settings.markets_symbols or self.settings.markets_feed_urls)

    async def poll(self) -> list[Observation]:
        headers = {"User-Agent": self.settings.user_agent}
        async with httpx.AsyncClient(
            timeout=self.settings.http_timeout_seconds, follow_redirects=True, headers=headers
        ) as client:
            quotes, headlines = await asyncio.gather(self._quotes(client), self._headlines(client))
        return quotes + headlines

    # --- quotes ------------------------------------------------------------

    async def _quotes(self, client: httpx.AsyncClient) -> list[Observation]:
        symbols = self.settings.markets_symbols
        if not symbols:
            return []
        results = await asyncio.gather(
            *(self._one_quote(client, symbol) for symbol in symbols),
            return_exceptions=True,
        )
        observations: list[Observation] = []
        for symbol, result in zip(symbols, results, strict=True):
            if isinstance(result, BaseException):
                # One unavailable symbol must not lose the rest of the poll.
                log.warning("quote for %s unavailable: %s", symbol, describe(result))
            elif result is not None:
                observations.append(result)
        return observations

    async def _one_quote(self, client: httpx.AsyncClient, symbol: str) -> Observation | None:
        # A month of daily closes gives the panel a real sparkline, not a stub.
        response = await client.get(
            QUOTE_URL.format(symbol=symbol), params={"interval": "1d", "range": "1mo"}
        )
        response.raise_for_status()
        results = (response.json().get("chart") or {}).get("result") or []
        if not results:
            return None
        meta = results[0].get("meta") or {}
        closes = _closes(results[0])

        price = meta.get("regularMarketPrice")
        # NOT meta.chartPreviousClose: that is the close before the *requested
        # range* starts, so with range=1mo it would make the delta a monthly
        # change. The prior session's close is the second-to-last daily close.
        previous = closes[-2] if len(closes) >= 2 else meta.get("previousClose")
        name = _clean_name(meta.get("shortName") or meta.get("longName") or symbol)
        market_time = meta.get("regularMarketTime")
        change, change_label = _format_change(price, previous)

        currency = meta.get("currency") or ""
        return Observation(
            key=f"quote:{symbol}:{market_time}",
            title=" ".join(f"{name}: {price} {currency} ({change_label})".split()),
            data={
                "kind": "quote",
                "symbol": symbol,
                "name": name,
                "price": price,
                "previous_close": previous,
                "change_percent": change,
                "currency": currency or None,
                "market_time": market_time,
                "sparkline": closes[-SPARKLINE_POINTS:],
                "exchange": meta.get("fullExchangeName"),
            },
        )

    # --- headlines ---------------------------------------------------------

    async def _headlines(self, client: httpx.AsyncClient) -> list[Observation]:
        observations: list[Observation] = []
        for url in self.settings.markets_feed_urls:
            entries, error = await fetch_feed(client, url, self.headlines_per_feed)
            if error:
                log.warning("markets feed %s unavailable: %s", url, error)
                continue
            observations.extend(
                Observation(
                    key=entry.key,
                    title=entry.title,
                    body=entry.summary,
                    url=entry.link,
                    data={
                        "kind": "headline",
                        "source": entry.feed_title,
                        "published": entry.published,
                    },
                )
                for entry in entries
            )
        return observations
