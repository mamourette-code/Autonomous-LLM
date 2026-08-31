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


def test_watchers_without_configuration_are_disabled(settings, db):
    scheduler = build_scheduler(settings, db)
    assert {w.name for w in scheduler.watchers} == {"email", "marine", "feeds"}
    # No IMAP host, no coordinates, no feeds in the test settings.
    assert all(not w.enabled for w in scheduler.watchers)


def test_marine_watcher_enables_with_coordinates(settings, db):
    settings.marine_latitude = 49.5
    settings.marine_longitude = -1.5
    scheduler = build_scheduler(settings, db)
    marine = next(w for w in scheduler.watchers if w.name == "marine")
    assert marine.enabled


async def test_marine_records_waves_when_wind_api_is_down(settings, db, monkeypatch):
    """A partial outage must not throw away the half that worked."""
    import httpx

    from autonomous.watchers.marine import MarineWatcher

    settings.marine_latitude = 49.6
    settings.marine_longitude = -1.6

    wave_payload = {
        "hourly": {
            "time": ["2026-01-01T00:00", "2026-01-01T01:00"],
            "wave_height": [1.4, 1.5],
            "wave_period": [5.5, 5.6],
            "wave_direction": [270, 271],
        }
    }

    async def fake_get(self, url, **kwargs):
        if "marine-api" in url:
            return httpx.Response(200, json=wave_payload, request=httpx.Request("GET", url))
        raise httpx.ConnectTimeout("", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    observations = await MarineWatcher(settings).poll()
    assert len(observations) == 2
    assert observations[0].data["wave_height_m"] == 1.4
    assert observations[0].data["wind_speed_kmh"] is None
    assert "wind ? km/h" in observations[0].title
