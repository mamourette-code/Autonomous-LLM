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
| `AUTH_TOKEN` | Required to sign in. **Set this before exposing the panel** |
| `RULES_ENABLED` / `MAX_AUTO_RUNS_PER_DAY` | Reactive rules and their daily cap |
| `HOST` / `PORT` | Where the panel binds. Default is localhost only |
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

## Running it all the time

`deploy/install.sh` installs the panel as a background service that starts when
you log in — a systemd user unit on Linux, a launchd agent on macOS:

```bash
./deploy/install.sh              # install and start
./deploy/install.sh --uninstall  # stop and remove
```

Linux logs go to `journalctl --user -u autonomous -f`; macOS to
`data/panel.log`. On Linux, `sudo loginctl enable-linger $USER` keeps it
running while you are logged out.

**A laptop sleeps.** Watchers only poll, and rules only fire, while the machine
is awake — and the panel is reachable only from that machine. If you want it
genuinely always-on and reachable from your phone, it needs to live on a
machine that stays up; the app is ready for that (see *Exposing the panel*).

## Reacting on its own

Rules turn a new observation into an agent run without you being there. Copy
`rules.example.json` to `rules.json`:

```json
{
  "name": "Sharp index move",
  "when": { "kind": "quote", "change_percent_abs_above": 2.0 },
  "goal": "{name} moved {change_percent}% today, to {price}. What drove it?",
  "cooldown_minutes": 240
}
```

Conditions combine with AND: `kind` (`quote` or `headline`), `source`,
`symbol`, `title_matches` (any of, case-insensitive), and
`change_percent_above` / `_below` / `_abs_above`. Goals can use `{title}`,
`{url}`, `{source}`, `{symbol}`, `{name}`, `{price}`, `{change_percent}`.

This is the only path that spends money without you asking, so it is bounded
three ways:

- **Only genuinely new observations fire a rule** — a re-reported headline cannot
  trigger the same run twice.
- **Per-rule `cooldown_minutes`** — a burst of thirty similar headlines produces
  one run, not thirty.
- **`MAX_AUTO_RUNS_PER_DAY`** caps automatic runs across all rules; when it is
  spent they stop until midnight UTC. The panel shows the count.

Set `RULES_ENABLED=false` to stop them entirely. Automatic runs are badged with
the rule that started them.

## Exposing the panel

By default the panel binds to `127.0.0.1` and has no authentication, which is
correct for a panel only you can reach. **The moment it listens on anything
else, set `AUTH_TOKEN`** — the panel can read your inbox, spend your API
credits and call every service you configured.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # generate one
echo 'AUTH_TOKEN=<paste it>' >> .env
```

With a token set, the browser gets a login form and a signed HttpOnly session
cookie; scripts can use `Authorization: Bearer <token>`. The cookie stores a
hash of the token, never the token itself. Put the panel behind HTTPS
(a reverse proxy is fine) and set `COOKIE_SECURE=true`. `/healthz` stays open
for health checks.

## The markets panel

The panel leads with a KPI row: one stat tile per symbol showing the level, the
change against the **prior session's close**, and a sparkline of the last month
of daily closes. Symbols are Yahoo Finance tickers, so anything that site quotes
works — indices, FX pairs, futures, crypto.

The panel updates over a server-sent event stream: run steps appear as they
happen and the market tiles refresh when a watcher polls, with slow polling as
a fallback if a proxy buffers the stream. The dot beside "live" shows the
connection.

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
├── rules.py         # reactive rules: observation -> agent run
└── web/             # FastAPI app, auth, event stream, panel
```
