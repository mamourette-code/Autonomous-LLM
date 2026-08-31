from __future__ import annotations


def test_run_lifecycle(db):
    run_id = db.create_run("do a thing", "mock", "mock-1")
    assert db.get_run(run_id)["status"] == "running"

    db.add_step(run_id, 1, "tool_call", name="fetch_url", content='{"url": "https://x"}')
    db.add_step(run_id, 1, "tool_result", name="fetch_url", content="HTTP 200")
    db.finish_run(run_id, status="succeeded", result="done")

    run = db.get_run(run_id)
    assert run["status"] == "succeeded"
    assert run["result"] == "done"
    assert run["finished_at"]

    steps = db.list_steps(run_id)
    assert [s["kind"] for s in steps] == ["tool_call", "tool_result"]


def test_observations_are_deduplicated(db):
    item = {"source": "feeds", "key": "abc", "title": "First post", "url": "https://x/1"}
    assert db.add_observations([item]) == 1
    assert db.add_observations([item]) == 0
    assert db.add_observations([{**item, "key": "def", "title": "Second"}]) == 1

    items = db.list_observations()
    assert {i["title"] for i in items} == {"First post", "Second"}


def test_observations_round_trip_structured_data(db):
    db.add_observations(
        [{"source": "marine", "key": "k", "title": "wave", "data": {"wave_height_m": 1.2}}]
    )
    assert db.list_observations(source="marine")[0]["data"] == {"wave_height_m": 1.2}
    assert db.list_observations(source="email") == []
