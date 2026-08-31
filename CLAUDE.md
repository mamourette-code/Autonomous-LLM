# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## What this is

A self-hosted autonomous assistant, built to be extended over time. Two halves,
and the split is the central design decision — keep it:

- **Watchers observe, continuously.** They poll a source on an interval and
  write observations. They never call the model and never act on anything.
- **Agent runs act, on demand.** The user states a goal in the UI; the agent
  plans, calls tools and iterates until it answers.

A watcher that started calling the model, or an agent run that started polling
in the background, would blur that line. Add to the correct half.

## Commands

```bash
uv pip install -e ".[dev]"       # install (pip install -e ".[dev]" works too)
autonomous serve                 # web UI + watchers on 127.0.0.1:8000
autonomous run "goal"            # one goal from the terminal
autonomous poll markets          # poll a single watcher once
pytest                           # full suite
pytest tests/test_agent.py -k budget   # a single test
ruff check . && ruff format .    # lint, then format
```

`PROVIDER=mock` runs the whole agent loop with no API key and no network — use
it for tests and for any change to the loop itself.

## Layout

| Path | Holds |
|---|---|
| `autonomous/config.py` | Every setting. Env-driven, all optional. |
| `autonomous/errors.py` | `describe(exc)` — never surface a bare `str(exc)`. |
| `autonomous/providers/` | LLM backends behind one protocol. |
| `autonomous/tools/` | What an agent run can do. |
| `autonomous/agent/loop.py` | The tool-calling loop. |
| `autonomous/watchers/` | The continuous half, plus the scheduler. |
| `autonomous/storage/db.py` | SQLite schema and all queries. |
| `autonomous/web/` | FastAPI app and the single-page UI. |
| `tests/` | pytest, `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed). |

## Architecture

**Providers.** `autonomous/providers/base.py` defines `Message`, `ToolSpec`,
`ToolCall`, `LLMResponse` and the `LLMProvider` protocol. The agent loop sees
only those. Adding a backend = one new module implementing `complete()` plus a
branch in `build_provider`; nothing else changes. Gemini is the default
(`google-genai`), with automatic function calling **disabled** on purpose — the
loop drives each tool call so it can be persisted, shown in the UI and bounded.

**Agent loop.** One iteration = one model call plus any tools it requested,
executed together via `asyncio.gather`. It ends when the model returns text
instead of tool calls, or when `MAX_STEPS` is exhausted (recorded as a failed
run — this is the guard against a model looping forever unattended). Every step
is written to SQLite as it happens, which is what lets the UI follow a live run.

**Tools.** A `Tool` is an async function plus a JSON Schema. Two rules the code
depends on:
1. *Tool failures are messages, not exceptions.* `ToolRegistry.call` catches
   everything and returns the error as a string for the model to react to. Never
   let a tool failure kill a run.
2. *Arguments are filtered.* Models invent parameters; `_filter_args` drops any
   the function does not accept.

**Events.** `events.py` is an in-process bus. The agent loop publishes
`run.started` / `run.step` / `run.finished`; the scheduler publishes
`watcher.polled` / `watcher.failed`; `/api/stream` relays them as SSE. Two
properties to preserve: publishing never blocks and never raises (a dead
subscriber must not break a run), and subscriber queues are bounded, dropping
oldest — a browser tab that stops reading loses history, not liveness.

**Auth.** Off when `AUTH_TOKEN` is unset, which is right for localhost. When
set, a middleware gates everything except `/login`, `/healthz` and `/static/`.
The session cookie holds a hash of the token, never the token, so a stolen
cookie cannot be replayed as a bearer credential. Compare secrets only with
`compare_digest`. Any new public path must be added to `auth.PUBLIC_PATHS`
deliberately — think about what it exposes first.

**Panel.** `web/static/` is plain HTML/CSS/JS, no build step and no framework —
keep it that way. `/api/markets` does the shaping (latest level per symbol, in
configured order, plus headlines) so the front end just renders. Color roles are
CSS custom properties defined for light, OS-dark and an explicit `data-theme`
stamp; add new colors as roles, never as raw hex in a rule. Direction is encoded
by arrow *and* sign *and* hue — never hue alone, and never red/green, which is
the one pair severe CVD cannot separate.

**Watchers.** Subclass `Watcher`: `name`, `interval_seconds`, `enabled`, and
`async poll()` returning `Observation`s. Observations de-duplicate on
`(source, key)`, so returning the same items every poll is fine and expected —
choose a stable `key`. `enabled` must be False when configuration is missing;
the scheduler skips those and the app starts regardless. Each watcher runs in
its own task with capped exponential backoff, so one dead source never stalls
the others. Blocking libraries (`imaplib`, `feedparser`) go through
`asyncio.to_thread`.

Partial failure should degrade, not discard: `MarineWatcher` records wave data
even when the separate wind API is unreachable. Prefer that pattern.

## Conventions

- **Secrets come from the environment, never from arguments.** `services.json`
  holds environment variable *names*; `call_service` injects the credential at
  call time. The model chooses a service and a path — never a header — so it
  cannot send a token to a host you did not configure. Preserve that property.
- **Mutating capability is opt-in.** `browser_interact` needs
  `BROWSER_ACTIONS_ENABLED`; service writes need `"allow_writes": true`. New
  side-effecting tools follow suit: mark them `mutating=True` and gate them.
- **Never commit secrets.** `.env`, `services.json` and `data/` are gitignored.
  Document new settings in `.env.example` with a blank or placeholder value.
- Use `describe(exc)` for error text. Several httpx exceptions stringify to `""`
  and would otherwise show up as a blank error in the UI.
- Keep `data/` disposable — it is a cache of runs and observations, not state
  anyone should have to preserve.
- Test new work against the mock provider and fake watchers, as the existing
  tests do. No test should need a network or an API key.

## Git conventions

- **Never commit directly to `main`.** Work on a feature branch and open a PR.
- Claude Code sessions use branches named `claude/<short-description>-<suffix>`.
- Push with `git push -u origin <branch-name>`.
- Imperative commit subjects; explain *why* in the body when it is not obvious.
- **Do not open a pull request unless the user explicitly asks for one.**
- Do not rewrite history on a branch someone else may have checked out.

## Keeping this file current

Update it in the same commit that changes what it describes. It is loaded into
every session, so it should carry what is *not* obvious from reading the code —
the design decisions above, not a tour of the modules. Delete entries that go
stale; a wrong CLAUDE.md is worse than a thin one.
