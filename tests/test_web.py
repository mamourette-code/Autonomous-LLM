from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autonomous.web.app import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_status_lists_tools_and_watchers(client):
    body = client.get("/api/status").json()
    assert body["provider"] == "mock"
    assert "fetch_url" in body["tools"]
    assert {w["name"] for w in body["watchers"]} == {"email", "markets", "feeds"}


def test_index_and_static_assets_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_run_endpoint_executes_and_persists(client):
    created = client.post("/api/runs", json={"goal": "say hello"})
    assert created.status_code == 201
    run_id = created.json()["id"]

    for _ in range(50):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            break
    assert run["status"] == "succeeded"
    # The mock provider with no script answers rather than calling tools.
    assert run["steps"][-1]["kind"] == "answer"
    assert any(r["id"] == run_id for r in client.get("/api/runs").json())


def test_empty_goal_is_rejected(client):
    assert client.post("/api/runs", json={"goal": ""}).status_code == 422


def test_missing_run_is_404(client):
    assert client.get("/api/runs/99999").status_code == 404


def test_polling_an_unconfigured_watcher_is_409(client):
    assert client.post("/api/watchers/email/poll").status_code == 409
    assert client.post("/api/watchers/nope/poll").status_code == 404


def test_markets_endpoint_shapes_quotes_and_headlines(client, settings):
    db = client.app.state.db
    settings.markets_symbols = ["^GSPC", "^FTSE"]
    db.add_observations(
        [
            {
                "source": "markets",
                "key": "quote:^FTSE:1",
                "title": "FTSE 100: 100 GBP (+1.00%)",
                "data": {"kind": "quote", "symbol": "^FTSE", "price": 100, "name": "FTSE 100"},
            },
            {
                "source": "markets",
                "key": "quote:^GSPC:1",
                "title": "S&P 500: 5000 USD (+0.50%)",
                "data": {"kind": "quote", "symbol": "^GSPC", "price": 5000, "name": "S&P 500"},
            },
            {
                "source": "markets",
                "key": "headline:1",
                "title": "Stocks rally",
                "url": "https://example.invalid/1",
                "data": {"kind": "headline", "source": "WSJ Markets"},
            },
        ]
    )
    body = client.get("/api/markets").json()

    # Configured order wins over insertion order.
    assert [q["symbol"] for q in body["quotes"]] == ["^GSPC", "^FTSE"]
    assert body["headlines"][0]["title"] == "Stocks rally"
    assert body["headlines"][0]["source"] == "WSJ Markets"


def test_markets_endpoint_keeps_only_the_latest_level_per_symbol(client):
    db = client.app.state.db
    for i, price in enumerate([100, 200]):
        db.add_observations(
            [
                {
                    "source": "markets",
                    "key": f"quote:^X:{i}",
                    "title": f"X: {price}",
                    "created_at": f"2026-01-0{i + 1}T00:00:00+00:00",
                    "data": {"kind": "quote", "symbol": "^X", "price": price, "name": "X"},
                }
            ]
        )
    quotes = client.get("/api/markets").json()["quotes"]
    assert len(quotes) == 1
    assert quotes[0]["price"] == 200


def test_rules_endpoint_reports_the_budget(client):
    body = client.get("/api/rules").json()
    assert body["enabled"] is True
    assert body["budget_used"] == 0
    assert body["budget_per_day"] > 0


def test_a_watcher_poll_fires_a_rule_and_starts_a_run(client, settings):
    """The whole reactive path: watcher observes -> rule matches -> run starts."""
    from autonomous.rules import Rule
    from autonomous.watchers.base import Observation, Watcher

    class Spike(Watcher):
        name = "spike"
        interval_seconds = 60

        async def poll(self):
            return [
                Observation(
                    key="quote:^GSPC:1",
                    title="S&P 500: 5000 (-4.00%)",
                    data={
                        "kind": "quote",
                        "symbol": "^GSPC",
                        "name": "S&P 500",
                        "change_percent": -4.0,
                    },
                )
            ]

    app = client.app
    app.state.rules.rules = [
        Rule(
            name="Sharp move",
            goal="Why did {name} move {change_percent}%?",
            kind="quote",
            change_percent_abs_above=2.0,
        )
    ]
    app.state.scheduler.watchers.append(Spike())
    app.state.scheduler.status["spike"] = type(app.state.scheduler.status["markets"])(
        name="spike", interval_seconds=60, enabled=True
    )

    assert client.post("/api/watchers/spike/poll").json()["new_observations"] == 1

    runs = client.get("/api/runs").json()
    triggered = [r for r in runs if r["trigger"] == "Sharp move"]
    assert len(triggered) == 1
    assert triggered[0]["goal"] == "Why did S&P 500 move -4.0%?"

    # And it counts against the daily budget.
    assert client.get("/api/rules").json()["budget_used"] == 1
