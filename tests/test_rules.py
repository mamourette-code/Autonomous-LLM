from __future__ import annotations

import json

import pytest

from autonomous.rules import Rule, RuleEngine, load_rules


def quote(symbol="^GSPC", change=3.0, name="S&P 500", price=5000):
    return {
        "source": "markets",
        "title": f"{name}: {price}",
        "url": None,
        "data": {
            "kind": "quote",
            "symbol": symbol,
            "name": name,
            "price": price,
            "change_percent": change,
        },
    }


def headline(title="Fed cuts rates", url="https://example.invalid/1"):
    return {
        "source": "markets",
        "title": title,
        "url": url,
        "data": {"kind": "headline", "source": "WSJ Markets"},
    }


class Recorder:
    def __init__(self):
        self.started: list[tuple[str, str]] = []

    async def __call__(self, goal: str, trigger: str) -> int:
        self.started.append((goal, trigger))
        return len(self.started)


def engine(settings, db, rules, recorder):
    # Rules ship switched off - the daily brief is the default update path -
    # so these tests turn them on explicitly.
    settings.rules_enabled = True
    return RuleEngine(rules, settings, db, recorder)


# --- matching -----------------------------------------------------------


def test_threshold_matches_only_past_the_limit():
    rule = Rule(name="big", goal="g", kind="quote", change_percent_abs_above=2.0)
    assert rule.matches(quote(change=3.0))
    assert rule.matches(quote(change=-3.0))
    assert not rule.matches(quote(change=1.0))
    assert not rule.matches(quote(change=-1.0))


def test_directional_thresholds():
    up = Rule(name="up", goal="g", change_percent_above=1.0)
    down = Rule(name="down", goal="g", change_percent_below=-1.0)
    assert up.matches(quote(change=2.0)) and not up.matches(quote(change=-2.0))
    assert down.matches(quote(change=-2.0)) and not down.matches(quote(change=2.0))


def test_a_quote_rule_ignores_headlines_and_vice_versa():
    quote_rule = Rule(name="q", goal="g", kind="quote", change_percent_abs_above=1.0)
    headline_rule = Rule(name="h", goal="g", kind="headline", title_matches=["fed"])
    assert not quote_rule.matches(headline())
    assert not headline_rule.matches(quote())


def test_title_matching_is_case_insensitive_and_any_of():
    rule = Rule.from_dict({"name": "cb", "goal": "g", "when": {"title_matches": ["Fed", "ECB"]}})
    assert rule.matches(headline("FED holds rates"))
    assert rule.matches(headline("ecb surprises markets"))
    assert not rule.matches(headline("Oil rallies"))


def test_a_quote_without_a_change_never_matches_a_threshold():
    rule = Rule(name="big", goal="g", change_percent_abs_above=2.0)
    stale = quote()
    stale["data"]["change_percent"] = None
    assert not rule.matches(stale)


def test_goal_placeholders_are_filled_and_unknown_ones_survive():
    rule = Rule(name="r", goal="{name} moved {change_percent}% — see {title} / {mystery}")
    rendered = rule.render(quote(change=2.5))
    assert "S&P 500 moved 2.5%" in rendered
    assert "{mystery}" in rendered


# --- firing -------------------------------------------------------------


async def test_a_matching_observation_starts_a_run(settings, db):
    recorder = Recorder()
    rule = Rule(
        name="Sharp move",
        goal="Why did {name} move {change_percent}%?",
        kind="quote",
        change_percent_abs_above=2.0,
    )

    started = await engine(settings, db, [rule], recorder).react("markets", [quote(change=3.0)])

    assert started == [1]
    goal, trigger = recorder.started[0]
    assert goal == "Why did S&P 500 move 3.0%?"
    assert trigger == "Sharp move"


async def test_nothing_fires_without_a_match(settings, db):
    recorder = Recorder()
    rule = Rule(name="r", goal="g", kind="quote", change_percent_abs_above=5.0)
    assert await engine(settings, db, [rule], recorder).react("markets", [quote(change=1.0)]) == []
    assert recorder.started == []


async def test_one_run_per_batch_not_one_per_matching_item(settings, db):
    """A burst of similar headlines must produce one run, not thirty."""
    recorder = Recorder()
    rule = Rule(name="cb", goal="{title}", kind="headline", title_matches=["fed"])

    batch = [headline(f"Fed speaker number {i}") for i in range(30)]
    started = await engine(settings, db, [rule], recorder).react("markets", batch)

    assert len(started) == 1


async def test_cooldown_suppresses_a_second_firing(settings, db):
    recorder = Recorder()
    rule = Rule(name="cb", goal="g", kind="headline", title_matches=["fed"], cooldown_minutes=60)
    eng = engine(settings, db, [rule], recorder)

    assert await eng.react("markets", [headline("Fed cuts")]) != []
    assert await eng.react("markets", [headline("Fed speaks again")]) == []
    assert len(recorder.started) == 1


async def test_cooldown_expires(settings, db):
    from datetime import UTC, datetime, timedelta

    recorder = Recorder()
    rule = Rule(name="cb", goal="g", kind="headline", title_matches=["fed"], cooldown_minutes=60)
    eng = engine(settings, db, [rule], recorder)

    await eng.react("markets", [headline("Fed cuts")])
    eng._last_fired["cb"] = datetime.now(UTC) - timedelta(minutes=61)

    assert await eng.react("markets", [headline("Fed again")]) != []
    assert len(recorder.started) == 2


async def test_the_daily_budget_stops_runs(settings, db):
    """The backstop against a runaway spend."""
    settings.max_auto_runs_per_day = 2
    recorder = Recorder()
    # Distinct rules so per-rule cooldown is not what stops it.
    rules = [
        Rule(name=f"rule-{i}", goal="g", kind="headline", title_matches=["fed"]) for i in range(5)
    ]
    for i in range(2):
        db.create_run(f"earlier auto run {i}", "mock", "m", trigger="rule-x")

    started = await engine(settings, db, rules, recorder).react("markets", [headline("Fed")])

    assert started == []
    assert recorder.started == []


async def test_manual_runs_do_not_consume_the_automatic_budget(settings, db):
    settings.max_auto_runs_per_day = 1
    recorder = Recorder()
    for i in range(5):
        db.create_run(f"manual {i}", "mock", "m")

    rule = Rule(name="r", goal="g", kind="headline", title_matches=["fed"])
    assert await engine(settings, db, [rule], recorder).react("markets", [headline("Fed")]) != []


async def test_rules_are_off_by_default(settings, db):
    """Shipping default: the daily brief updates you, not unprompted runs."""
    assert settings.rules_enabled is False
    recorder = Recorder()
    rule = Rule(name="r", goal="g", kind="headline", title_matches=["fed"])
    off = RuleEngine([rule], settings, db, recorder)

    assert await off.react("markets", [headline("Fed")]) == []
    assert recorder.started == []


async def test_a_failing_rule_does_not_stop_the_others(settings, db):
    settings.rules_enabled = True
    calls: list[str] = []

    async def flaky(goal: str, trigger: str) -> int:
        calls.append(trigger)
        if trigger == "first":
            raise RuntimeError("provider down")
        return 1

    rules = [
        Rule(name="first", goal="g", kind="headline", title_matches=["fed"]),
        Rule(name="second", goal="g", kind="headline", title_matches=["fed"]),
    ]
    started = await RuleEngine(rules, settings, db, flaky).react("markets", [headline("Fed")])

    assert calls == ["first", "second"]
    assert started == [1]


# --- loading ------------------------------------------------------------


def test_rules_load_from_the_example_file(tmp_path):
    from autonomous.config import REPO_ROOT

    rules = load_rules(REPO_ROOT / "rules.example.json")
    assert {r.name for r in rules} == {"Sharp index move", "Central bank news"}

    sharp = next(r for r in rules if r.name == "Sharp index move")
    assert sharp.change_percent_abs_above == pytest.approx(2.0)
    assert sharp.cooldown_minutes == 240


def test_a_missing_rules_file_is_not_an_error(tmp_path):
    assert load_rules(tmp_path / "nope.json") == []


def test_rules_round_trip_through_json(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "Oil spike",
                        "when": {"kind": "quote", "symbol": "CL=F", "change_percent_above": 3},
                        "goal": "Why is oil up {change_percent}%?",
                        "cooldown_minutes": 30,
                    }
                ]
            }
        )
    )
    rule = load_rules(path)[0]
    assert rule.symbol == "CL=F"
    assert rule.matches(quote(symbol="CL=F", change=4.0))
    assert not rule.matches(quote(symbol="^GSPC", change=4.0))
