from __future__ import annotations

import httpx
import pytest

from autonomous.branches import Branch, load_branches
from autonomous.watchers.base import Observation, Watcher
from autonomous.watchers.branch import BranchWatcher
from autonomous.watchers.scheduler import Scheduler, build_scheduler


def branch_watcher(settings, *, symbols=None, feeds=None, slug="markets"):
    return BranchWatcher(
        Branch(slug=slug, name=slug.title(), symbols=list(symbols or []), feeds=list(feeds or [])),
        settings,
    )


def quote_response(url, *, symbol="^GSPC", price=5100.0, closes=None, **meta):
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "regularMarketPrice": price, **meta},
                    "indicators": {"quote": [{"close": closes if closes is not None else []}]},
                }
            ]
        }
    }
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


# --- scheduler ----------------------------------------------------------


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
    watcher = FakeWatcher([RuntimeError("feed down")])
    scheduler = Scheduler(settings=settings, db=db, watchers=[watcher])

    with pytest.raises(RuntimeError):
        await scheduler.poll_once(watcher)

    status = scheduler.status["fake"]
    assert "feed down" in status.last_error
    assert status.consecutive_failures == 1


# --- branches -----------------------------------------------------------


def test_a_watcher_is_built_for_every_branch(settings, db):
    scheduler = build_scheduler(settings, db)
    names = [w.name for w in scheduler.watchers]

    assert names[:3] == ["markets", "tech", "world"]
    assert "email" in names
    # Branches are configured out of the box; the inbox needs credentials.
    assert all(w.enabled for w in scheduler.watchers if w.name != "email")
    assert not next(w for w in scheduler.watchers if w.name == "email").enabled


def test_adding_a_branch_adds_a_watcher(settings, db, tmp_path):
    extra = Branch(slug="cycling", name="Cycling", feeds=["https://example.invalid/rss"])
    scheduler = build_scheduler(settings, db, None, [*load_branches(), extra])

    cycling = next(w for w in scheduler.watchers if w.name == "cycling")
    assert cycling.enabled


def test_a_branch_with_no_sources_is_disabled(settings):
    assert not branch_watcher(settings, slug="empty", symbols=[], feeds=[]).enabled


async def test_a_branch_reports_quotes_and_headlines(settings, monkeypatch):
    async def fake_get(self, url, **kwargs):
        return quote_response(url, shortName="S&P 500", closes=[4900.0, 5000.0, 5100.0])

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
    monkeypatch.setattr("autonomous.watchers.branch.fetch_feed", fake_feed)

    observations = await branch_watcher(
        settings, symbols=["^GSPC"], feeds=["https://example.invalid/f.rss"]
    ).poll()
    quotes = [o for o in observations if o.data["kind"] == "quote"]
    headlines = [o for o in observations if o.data["kind"] == "headline"]

    assert len(quotes) == 1 and len(headlines) == 1
    assert quotes[0].data["change_percent"] == pytest.approx(2.0)
    assert "+2.00%" in quotes[0].title
    assert headlines[0].title == "Stocks rally"
    # Every observation is tagged with the branch it came from.
    assert {o.data["branch"] for o in observations} == {"markets"}


async def test_one_bad_symbol_does_not_lose_the_others(settings, monkeypatch):
    async def fake_get(self, url, **kwargs):
        if "BAD" in url:
            raise httpx.ConnectTimeout("", request=httpx.Request("GET", url))
        return quote_response(url, symbol="GOOD", price=1.0, closes=[1.0, 1.0])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    observations = await branch_watcher(settings, symbols=["GOOD", "BAD"]).poll()

    assert [o.data["symbol"] for o in observations] == ["GOOD"]


async def test_quote_key_changes_with_market_time(settings, monkeypatch):
    """A new price must not be swallowed by de-duplication."""
    market_time = 111

    async def fake_get(self, url, **kwargs):
        return quote_response(
            url, symbol="X", price=2.0, closes=[1.0, 2.0], regularMarketTime=market_time
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    watcher = branch_watcher(settings, symbols=["X"])

    first = (await watcher.poll())[0]
    market_time = 222
    second = (await watcher.poll())[0]

    assert first.key != second.key


async def test_quote_titles_are_tidy(settings, monkeypatch):
    """Yahoo pads some names and omits currency; neither should reach the panel."""

    async def fake_get(self, url, **kwargs):
        return quote_response(
            url,
            symbol="^STOXX50E",
            price=6465.1,
            closes=[6400.0, 6455.4, 6465.1],
            shortName="EURO STOXX 50                 I",
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    observation = (await branch_watcher(settings, symbols=["^STOXX50E"]).poll())[0]

    assert observation.data["name"] == "EURO STOXX 50 I"
    assert "None" not in observation.title
    assert observation.title == "EURO STOXX 50 I: 6465.1 (+0.15%)"


async def test_change_is_against_the_prior_session_not_the_range_start(settings, monkeypatch):
    """meta.chartPreviousClose is relative to the requested range, so a 1-month
    range would turn the daily delta into a monthly one."""

    async def fake_get(self, url, **kwargs):
        return quote_response(
            url,
            price=101.0,
            closes=[50.0, 90.0, 100.0, 101.0],
            chartPreviousClose=50.0,  # a month ago
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    quote = (await branch_watcher(settings, symbols=["^GSPC"]).poll())[0]

    # 101 vs the prior close of 100 is +1%, not +102% against the range start.
    assert quote.data["previous_close"] == 100.0
    assert quote.data["change_percent"] == pytest.approx(1.0)
    assert quote.data["sparkline"] == [50.0, 90.0, 100.0, 101.0]


async def test_change_percent_is_rounded(settings, monkeypatch):
    async def fake_get(self, url, **kwargs):
        return quote_response(url, price=7680.79, closes=[7711.76, 7680.79])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    quote = (await branch_watcher(settings, symbols=["^GSPC"]).poll())[0]

    assert quote.data["change_percent"] == -0.4  # not -0.4015914209756259
