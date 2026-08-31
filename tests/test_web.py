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
    assert {w["name"] for w in body["watchers"]} == {"email", "marine", "feeds"}


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
