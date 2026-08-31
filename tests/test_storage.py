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
    assert len(db.add_observations([item])) == 1
    # Re-reporting the same item returns nothing new, so a rule cannot re-fire.
    assert db.add_observations([item]) == []
    new = db.add_observations([{**item, "key": "def", "title": "Second"}])
    assert [o["title"] for o in new] == ["Second"]

    items = db.list_observations()
    assert {i["title"] for i in items} == {"First post", "Second"}


def test_observations_round_trip_structured_data(db):
    db.add_observations(
        [{"source": "marine", "key": "k", "title": "wave", "data": {"wave_height_m": 1.2}}]
    )
    assert db.list_observations(source="marine")[0]["data"] == {"wave_height_m": 1.2}
    assert db.list_observations(source="email") == []


def test_add_observations_reports_only_the_new_rows_from_a_mixed_batch(db):
    db.add_observations([{"source": "s", "key": "old", "title": "Already seen"}])

    new = db.add_observations(
        [
            {"source": "s", "key": "old", "title": "Already seen"},
            {"source": "s", "key": "fresh", "title": "Brand new"},
        ]
    )
    assert [o["key"] for o in new] == ["fresh"]


def test_runs_are_counted_by_trigger(db):
    db.create_run("manual one", "mock", "m")
    db.create_run("auto one", "mock", "m", trigger="Big move")
    db.create_run("auto two", "mock", "m", trigger="Big move")

    assert db.count_runs_since("2000-01-01", automatic=True) == 2
    assert db.count_runs_since("2000-01-01", automatic=False) == 1
    assert db.count_runs_since("2999-01-01", automatic=True) == 0


def test_trigger_column_is_added_to_an_older_database(tmp_path):
    """A database created before triggers existed must still open."""
    import sqlite3

    from autonomous.storage import Database

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT NOT NULL,"
        " status TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, result TEXT,"
        " error TEXT, created_at TEXT NOT NULL, finished_at TEXT)"
    )
    legacy.execute(
        "INSERT INTO runs (goal, status, provider, model, created_at)"
        " VALUES ('old run', 'succeeded', 'mock', 'm', '2026-01-01T00:00:00+00:00')"
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    try:
        assert db.list_runs()[0]["goal"] == "old run"
        assert db.list_runs()[0]["trigger"] is None
        assert db.create_run("new run", "mock", "m", trigger="rule") > 0
    finally:
        db.close()
