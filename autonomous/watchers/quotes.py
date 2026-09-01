"""Market quotes from Yahoo Finance's keyless chart endpoint.

Shared by any branch that lists symbols, not just the markets one.
"""

from __future__ import annotations

import httpx

from autonomous.watchers.base import Observation

QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SPARKLINE_POINTS = 24


def clean_name(value: str) -> str:
    """Yahoo pads some names ("EURO STOXX 50      I"); collapse the whitespace."""
    return " ".join(value.split())


def format_change(price: float | None, previous: float | None) -> tuple[float | None, str]:
    if price is None or not previous:
        return None, "n/a"
    # Rounded at source: this value is displayed and substituted into prompts,
    # where full float precision is noise.
    change = round((price - previous) / previous * 100, 2)
    return change, f"{change:+.2f}%"


def closes(result: dict) -> list[float]:
    """Daily closing prices, gaps (holidays, halts) dropped."""
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    return [c for c in (quote.get("close") or []) if c is not None]


async def fetch_quote(client: httpx.AsyncClient, symbol: str) -> Observation | None:
    # A month of daily closes gives the panel a real sparkline, not a stub.
    response = await client.get(
        QUOTE_URL.format(symbol=symbol), params={"interval": "1d", "range": "1mo"}
    )
    response.raise_for_status()
    results = (response.json().get("chart") or {}).get("result") or []
    if not results:
        return None

    meta = results[0].get("meta") or {}
    series = closes(results[0])

    price = meta.get("regularMarketPrice")
    # NOT meta.chartPreviousClose: that is the close before the *requested range*
    # starts, so with range=1mo it would make the delta a monthly change. The
    # prior session's close is the second-to-last daily close.
    previous = series[-2] if len(series) >= 2 else meta.get("previousClose")
    name = clean_name(meta.get("shortName") or meta.get("longName") or symbol)
    market_time = meta.get("regularMarketTime")
    change, change_label = format_change(price, previous)
    currency = meta.get("currency") or ""

    return Observation(
        # The key embeds the market timestamp, so a new price is a new
        # observation rather than a de-duplicated no-op.
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
            "sparkline": series[-SPARKLINE_POINTS:],
            "exchange": meta.get("fullExchangeName"),
        },
    )
