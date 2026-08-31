from __future__ import annotations

import pytest

from autonomous.watchers.base import Observation, Watcher
from autonomous.watchers.scheduler import Scheduler, build_scheduler


class FakeWatcher(Watcher):
    name = "fake"

    def __init__(self, batches):
        self._batches = list(batches)
        self.polls = 0

    @property
    def interval_seconds(self) -> int:
        return 60

    async def poll(self):
        self.polls += 1
        item = self._batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_poll_once_stores_and_deduplicates(settings, db):
    observation = Observation(key="a", title="First", url="https://x")
    watcher = FakeWatcher([[observation], [observation]])
    scheduler = Scheduler(settings=settings, db=db, watchers=[watcher])

    assert await scheduler.poll_once(watcher) == 1
    assert await scheduler.poll_once(watcher) == 0

    status = scheduler.status["fake"]
    assert status.new_observations == 1
    assert status.total_polls == 2
    assert status.last_error is None
    assert db.list_observations(source="fake")[0]["title"] == "First"


async def test_failure_is_recorded_and_reraised(settings, db):
    watcher = FakeWatcher([RuntimeError("imap down")])
    scheduler = Scheduler(settings=settings, db=db, watchers=[watcher])

    with pytest.raises(RuntimeError):
        await scheduler.poll_once(watcher)

    status = scheduler.status["fake"]
    assert "imap down" in status.last_error
    assert status.consecutive_failures == 1


def test_email_and_feeds_are_disabled_without_configuration(settings, db):
    scheduler = build_scheduler(settings, db)
    assert {w.name for w in scheduler.watchers} == {"email", "markets", "feeds"}
    disabled = {w.name for w in scheduler.watchers if not w.enabled}
    assert disabled == {"email", "feeds"}


def test_markets_is_on_by_default_and_off_when_emptied(settings, db):
    """Markets is the one watcher that works with no configuration at all."""
    markets = next(w for w in build_scheduler(settings, db).watchers if w.name == "markets")
    assert markets.enabled

    settings.markets_symbols = []
    settings.markets_feed_urls = []
    markets = next(w for w in build_scheduler(settings, db).watchers if w.name == "markets")
    assert not markets.enabled


async def test_markets_reports_quotes_and_headlines(settings, db, monkeypatch):
    import httpx

    from autonomous.watchers.markets import MarketsWatcher

    settings.markets_symbols = ["^GSPC"]
    settings.markets_feed_urls = ["https://example.invalid/markets.rss"]

    quote_payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": "^GSPC",
                        "shortName": "S&P 500",
                        "regularMarketPrice": 5100.0,
                        "currency": "USD",
                        "regularMarketTime": 1700000000,
                    },
                    "indicators": {"quote": [{"close": [4900.0, 5000.0, 5100.0]}]},
                }
            ]
        }
    }

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json=quote_payload, request=httpx.Request("GET", url))

    async def fake_feed(client, url, limit=15):
        from autonomous.watchers.feeds import FeedEntry

        return [
            FeedEntry(
                feed_title="WSJ Markets",
                title="Stocks rally",
                link="https://example.invalid/1",
                summary="A summary",
                published="Mon, 01 Jan 2026 09:00:00 GMT",
            )
        ], None

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr("autonomous.watchers.markets.fetch_feed", fake_feed)

    observations = await MarketsWatcher(settings).poll()
    quotes = [o for o in observations if o.data["kind"] == "quote"]
    headlines = [o for o in observations if o.data["kind"] == "headline"]

    assert len(quotes) == 1 and len(headlines) == 1
    assert quotes[0].data["change_percent"] == pytest.approx(2.0)
    assert "+2.00%" in quotes[0].title
    assert headlines[0].title == "Stocks rally"


async def test_one_bad_symbol_does_not_lose_the_others(settings, db, monkeypatch):
    import httpx

    from autonomous.watchers.markets import MarketsWatcher

    settings.markets_symbols = ["GOOD", "BAD"]
    settings.markets_feed_urls = []

    async def fake_get(self, url, **kwargs):
        if "BAD" in url:
            raise httpx.ConnectTimeout("", request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "GOOD",
                                "regularMarketPrice": 1.0,
                                "chartPreviousClose": 1.0,
                            }
                        }
                    ]
                }
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    observations = await MarketsWatcher(settings).poll()
    assert [o.data["symbol"] for o in observations] == ["GOOD"]


async def test_quote_key_changes_with_market_time(settings, monkeypatch):
    """A new price must not be swallowed by de-duplication."""
    import httpx

    from autonomous.watchers.markets import MarketsWatcher

    settings.markets_symbols = ["X"]
    settings.markets_feed_urls = []
    market_time = 111

    async def fake_get(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "X",
                                "regularMarketPrice": 2.0,
                                "regularMarketTime": market_time,
                            },
                            "indicators": {"quote": [{"close": [1.0, 2.0]}]},
                        }
                    ]
                }
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    watcher = MarketsWatcher(settings)

    first = (await watcher.poll())[0]
    market_time = 222
    second = (await watcher.poll())[0]

    assert first.key != second.key


async def test_quote_titles_are_tidy(settings, monkeypatch):
    """Yahoo pads some names and omits currency; neither should reach the panel."""
    import httpx

    from autonomous.watchers.markets import MarketsWatcher

    settings.markets_symbols = ["^STOXX50E"]
    settings.markets_feed_urls = []

    async def fake_get(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "^STOXX50E",
                                "shortName": "EURO STOXX 50                 I",
                                "regularMarketPrice": 6465.1,
                            },
                            "indicators": {"quote": [{"close": [6400.0, 6455.4, 6465.1]}]},
                        }
                    ]
                }
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    observation = (await MarketsWatcher(settings).poll())[0]

    assert observation.data["name"] == "EURO STOXX 50 I"
    assert "None" not in observation.title
    assert observation.title == "EURO STOXX 50 I: 6465.1 (+0.15%)"


async def test_change_is_against_the_prior_session_not_the_range_start(settings, monkeypatch):
    """meta.chartPreviousClose is relative to the requested range, so a 1-month
    range would turn the daily delta into a monthly one."""
    import httpx

    from autonomous.watchers.markets import MarketsWatcher

    settings.markets_symbols = ["^GSPC"]
    settings.markets_feed_urls = []

    async def fake_get(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "^GSPC",
                                "shortName": "S&P 500",
                                "regularMarketPrice": 101.0,
                                "chartPreviousClose": 50.0,  # a month ago
                            },
                            "indicators": {"quote": [{"close": [50.0, 90.0, 100.0, 101.0]}]},
                        }
                    ]
                }
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    quote = (await MarketsWatcher(settings).poll())[0]

    # 101 vs the prior close of 100 is +1%, not +102% against the range start.
    assert quote.data["previous_close"] == 100.0
    assert quote.data["change_percent"] == pytest.approx(1.0)
    assert quote.data["sparkline"] == [50.0, 90.0, 100.0, 101.0]


async def test_change_percent_is_rounded_for_display_and_templating(settings, monkeypatch):
    import httpx

    from autonomous.watchers.markets import MarketsWatcher

    settings.markets_symbols = ["^GSPC"]
    settings.markets_feed_urls = []

    async def fake_get(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {"symbol": "^GSPC", "regularMarketPrice": 7680.79},
                            "indicators": {"quote": [{"close": [7711.76, 7680.79]}]},
                        }
                    ]
                }
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    quote = (await MarketsWatcher(settings).poll())[0]

    # Not -0.4015914209756259.
    assert quote.data["change_percent"] == -0.4
