# Autonomous-LLM

A self-hosted autonomous assistant with two halves:

- **Watchers** run continuously, polling sources you care about — financial
  market levels and headlines, an IMAP inbox, any RSS/Atom feed — and writing
  what they find to a local feed. They only ever *observe*: no model calls,
  no actions.
- **Task runs** happen when *you* ask for one. A goal goes to an LLM that plans,
  calls tools (fetch a page, read a feed, call a configured API, drive a headless
  browser, read the watcher feed) and iterates until it can answer.

Everything runs locally against a SQLite file. The panel at `127.0.0.1:8000`
leads with market levels and headlines, starts task runs and shows each step as
it happens.

## Quick start

```bash
git clone https://github.com/mamourette-code/Autonomous-LLM
cd Autonomous-LLM

python -m venv .venv && source .venv/bin/activate    # or: uv venv
pip install -e ".[dev]"                              # or: uv pip install -e ".[dev]"

cp .env.example .env        # then add your GEMINI_API_KEY
autonomous serve            # http://127.0.0.1:8000
```

Get a Gemini API key from https://aistudio.google.com/apikey.

Without a key the app still starts: watchers run, and `PROVIDER=mock` lets you
exercise the agent loop offline.

## Commands

| Command | Does |
|---|---|
| `autonomous serve` | Start the web UI and the watchers |
| `autonomous run "your goal"` | Run one goal from the terminal |
| `autonomous poll markets` | Poll one watcher once and print what is new |
| `pytest` | Run the test suite |
| `ruff check . && ruff format .` | Lint and format |

## Configuration

All configuration is environment variables, read from `.env` (gitignored). See
`.env.example` for the full list. Nothing is required — an unset feature simply
switches itself off.

| Setting | Purpose |
|---|---|
| `GEMINI_API_KEY`, `GEMINI_MODEL` | The model backend |
| `MAX_STEPS` | Ceiling on tool-calling iterations per run |
| `MARKETS_SYMBOLS` / `MARKETS_FEED_URLS` | The markets watcher. On by default, keyless |
| `IMAP_HOST` / `IMAP_USER` / `IMAP_PASSWORD` | Enables the email watcher (read-only) |
| `FEED_URLS` | Any other RSS/Atom feeds to watch |
| `BROWSER_ENABLED` | Headless-browser page reading |
| `BROWSER_ACTIONS_ENABLED` | Lets the agent click, type and submit forms |

### Third-party APIs

Copy `services.example.json` to `services.json` and declare each service's base
URL plus the *environment variable name* holding its credential. Tokens are
injected at call time — the model picks a service and a path, never a header, so
it cannot leak a credential to a host you did not configure. Writes are refused
unless a service sets `"allow_writes": true`.

### Headless browser

```bash
pip install -e ".[browser]" && playwright install chromium
```

Then set `BROWSER_ENABLED=true`. Acting on pages (`browser_interact`) additionally
needs `BROWSER_ACTIONS_ENABLED=true`; it is off by default because it operates on
real sites under your identity.

## The markets panel

The panel leads with a KPI row: one stat tile per symbol showing the level, the
change against the **prior session's close**, and a sparkline of the last month
of daily closes. Symbols are Yahoo Finance tickers, so anything that site quotes
works — indices, FX pairs, futures, crypto.

Direction is shown three ways at once — an arrow, a signed number, and hue — so
it never depends on color alone. The up/down hues are blue and red rather than
the conventional green/red: red-green is the one pair that the most common forms
of color blindness cannot separate. Both hues are validated against the light
and dark surfaces.

## Layout

```
autonomous/
├── config.py        # env-driven settings
├── errors.py        # exception -> readable message
├── cli.py           # serve / run / poll
├── providers/       # LLM backends behind one protocol (gemini, mock)
├── tools/           # what the agent can do: web, browser, services, memory
├── agent/loop.py    # goal -> tool calls -> answer
├── watchers/        # the continuously running half + scheduler
├── storage/         # SQLite: runs, steps, observations
└── web/             # FastAPI app and single-page UI
```
