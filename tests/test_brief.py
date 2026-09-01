from __future__ import annotations

from datetime import UTC, datetime

from autonomous.branches import Branch
from autonomous.brief import BriefGenerator, _batch, _format_material
from autonomous.providers import LLMResponse, MockProvider


def branch(slug, name=None):
    return Branch(slug=slug, name=name or slug.title(), focus="what changed", feeds=["x"])


def seed(db, source, *, quotes=0, headlines=0):
    items = []
    for i in range(quotes):
        items.append(
            {
                "source": source,
                "key": f"quote:{source}:{i}",
                "title": f"Sym{i}: 100",
                "data": {
                    "kind": "quote",
                    "symbol": f"S{i}",
                    "name": f"Sym{i}",
                    "price": 100 + i,
                    "change_percent": 1.5,
                    "currency": "USD",
                },
            }
        )
    for i in range(headlines):
        items.append(
            {
                "source": source,
                "key": f"head:{source}:{i}",
                "title": f"{source} headline {i}",
                "data": {"kind": "headline", "source": "Feed"},
            }
        )
    db.add_observations(items)


def today():
    return datetime.now(UTC).date().isoformat()


# --- batching -----------------------------------------------------------


def test_each_branch_gets_its_own_call_when_within_budget():
    branches = [branch(s) for s in ("markets", "tech", "world")]
    assert _batch(branches, 5) == [[b] for b in branches]


def test_branches_are_batched_when_they_exceed_the_budget():
    branches = [branch(f"b{i}") for i in range(8)]
    groups = _batch(branches, 5)

    assert len(groups) == 5  # never more calls than the budget
    assert sum(len(g) for g in groups) == 8  # and nothing is dropped


def test_a_zero_budget_makes_no_calls():
    assert _batch([branch("a")], 0) == []


# --- material -----------------------------------------------------------


def test_material_keeps_only_the_latest_level_per_symbol(db):
    observations = [
        {
            "title": "x",
            "data": {
                "kind": "quote",
                "symbol": "^GSPC",
                "name": "S&P",
                "price": 5100,
                "change_percent": 1.0,
            },
        },
        {
            "title": "x",
            "data": {
                "kind": "quote",
                "symbol": "^GSPC",
                "name": "S&P",
                "price": 5000,
                "change_percent": 0.5,
            },
        },
    ]
    material, count = _format_material(observations)

    assert count == 1
    assert "5100" in material and "5000" not in material


def test_material_is_empty_when_there_is_nothing():
    material, count = _format_material([])
    assert material == "" and count == 0


# --- generation ---------------------------------------------------------


async def test_one_call_per_branch_and_summaries_are_stored(settings, db):
    branches = [branch("markets"), branch("tech")]
    for b in branches:
        seed(db, b.slug, headlines=3)

    provider = MockProvider(
        [
            LLMResponse(text="Markets update text"),
            LLMResponse(text="Tech update text"),
        ]
    )
    sections = await BriefGenerator(provider, db, settings, branches).run()

    assert len(provider.calls) == 2  # one call per branch, no tool loop
    assert {s.branch for s in sections} == {"markets", "tech"}
    assert db.get_brief("markets", today())["summary"] == "Markets update text"
    assert db.get_brief("tech", today())["sources"] == 3


async def test_a_branch_with_nothing_new_costs_no_call(settings, db):
    """The main efficiency win: silence is free."""
    branches = [branch("markets"), branch("tech")]
    seed(db, "markets", headlines=2)  # tech has nothing

    provider = MockProvider([LLMResponse(text="Markets only")])
    sections = await BriefGenerator(provider, db, settings, branches).run()

    assert len(provider.calls) == 1
    assert [s.branch for s in sections] == ["markets"]
    assert db.get_brief("tech", today()) is None


async def test_a_branch_already_briefed_today_is_not_redone(settings, db):
    branches = [branch("markets")]
    seed(db, "markets", headlines=2)
    db.save_brief("markets", today(), "already written", 2)

    provider = MockProvider([LLMResponse(text="should not be used")])
    assert await BriefGenerator(provider, db, settings, branches).run() == []
    assert provider.calls == []
    assert db.get_brief("markets", today())["summary"] == "already written"


async def test_force_regenerates_an_existing_brief(settings, db):
    branches = [branch("markets")]
    seed(db, "markets", headlines=2)
    db.save_brief("markets", today(), "stale", 2)

    provider = MockProvider([LLMResponse(text="fresh")])
    await BriefGenerator(provider, db, settings, branches).run(force=True)

    assert db.get_brief("markets", today())["summary"] == "fresh"


async def test_the_budget_is_never_exceeded(settings, db):
    """Eight branches must still fit in five calls."""
    settings.daily_brief_max_calls = 5
    branches = [branch(f"b{i}") for i in range(8)]
    for b in branches:
        seed(db, b.slug, headlines=2)

    sections = "\n".join(f"=== b{i} ===\nUpdate {i}" for i in range(8))
    provider = MockProvider(lambda _m: LLMResponse(text=sections))
    await BriefGenerator(provider, db, settings, branches).run()

    assert len(provider.calls) == 5


async def test_a_batched_answer_is_split_per_branch(settings, db):
    settings.daily_brief_max_calls = 1
    branches = [branch("markets"), branch("tech")]
    for b in branches:
        seed(db, b.slug, headlines=2)

    provider = MockProvider(
        [LLMResponse(text="=== markets ===\nIndices fell.\n\n=== tech ===\nA model shipped.")]
    )
    await BriefGenerator(provider, db, settings, branches).run()

    assert len(provider.calls) == 1
    assert db.get_brief("markets", today())["summary"] == "Indices fell."
    assert db.get_brief("tech", today())["summary"] == "A model shipped."


async def test_a_batched_answer_without_markers_is_not_lost(settings, db):
    """If the model ignores the format, keep the text rather than the call."""
    settings.daily_brief_max_calls = 1
    branches = [branch("markets"), branch("tech")]
    for b in branches:
        seed(db, b.slug, headlines=2)

    provider = MockProvider([LLMResponse(text="One blob with no section markers.")])
    sections = await BriefGenerator(provider, db, settings, branches).run()

    assert len(sections) == 1
    assert db.get_brief("markets", today())["summary"].startswith("One blob")


async def test_a_failing_call_does_not_lose_the_other_branches(settings, db):
    branches = [branch("markets"), branch("tech")]
    for b in branches:
        seed(db, b.slug, headlines=2)

    calls = {"n": 0}

    def flaky(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return LLMResponse(text="Tech update")

    sections = await BriefGenerator(MockProvider(flaky), db, settings, branches).run()

    assert [s.branch for s in sections] == ["tech"]
    assert db.get_brief("tech", today())["summary"] == "Tech update"


async def test_the_brief_asks_for_no_tools(settings, db):
    """A tool loop would cost several calls per branch instead of one."""
    branches = [branch("markets")]
    seed(db, "markets", headlines=2)

    captured = {}

    def capture(messages):
        captured["messages"] = messages
        return LLMResponse(text="ok")

    provider = MockProvider(capture)
    await BriefGenerator(provider, db, settings, branches).run()

    # One user turn carrying the pre-gathered material, and that is all.
    assert len(captured["messages"]) == 1
    assert "markets headline 0" in captured["messages"][0].text
