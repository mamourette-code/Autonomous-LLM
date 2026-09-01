# Autonomous-LLM

A self-hosted autonomous assistant with two halves:

- **Watchers** run continuously, one per branch of interest, collecting market
  levels and headlines into a local feed. They only ever *observe*: no model
  calls, no actions — which is what makes the daily brief so cheap.
- **Task runs** happen when *you* ask for one. A goal goes to an LLM that plans,
  calls tools (fetch a page, read a feed, call a configured API, drive a headless
  browser, read the watcher feed) and iterates until it can answer.

Everything runs locally against a SQLite file. The panel at `127.0.0.1:8000`
leads with an engine — one cylinder per branch of interest. Hover a cylinder and
its piston rises; click it and you get that branch's update for the day.

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

**Model:** the default is `gemini-3.6-flash`. Google retires older models to new
users, so if you get a 404 saying the model is unavailable, list what your key
can actually reach and set `GEMINI_MODEL`:

```bash
.venv/bin/python -c "from google import genai; import os; \
  [print(m.name) for m in genai.Client(api_key=os.environ['GEMINI_API_KEY']).models.list()]"
```

**Free-tier rate limits are tight.** One task run makes several model calls, and
the free tier allows about five requests per minute per model. The provider
retries on 429 and 503, honouring the delay the API asks for, so runs get
slower rather than failing — but keep `MAX_AUTO_RUNS_PER_DAY` modest, or move to
a paid tier before leaning on rules.

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
| `DAILY_BRIEF_ENABLED` / `DAILY_BRIEF_MAX_CALLS` | The daily brief and its call budget |
| `RULES_ENABLED` / `MAX_AUTO_RUNS_PER_DAY` | Reactive rules (off by default) and their cap |
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

## Reacting on its own (off by default)

The daily brief is the update path. Rules are the alternative: they turn a new
observation into an agent run without you being there, which spends
unpredictably. Set `RULES_ENABLED=true` to use them. Copy
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

## Branches and the daily brief

Each branch of interest is one cylinder in the engine. Three ship by default —
**Financial Markets**, **Technology & AI**, **World & Geopolitics** — defined in
`autonomous/branches.py` and overridable by copying `branches.example.json` to
`branches.json`:

```json
{
  "slug": "cycling",
  "name": "Pro Cycling",
  "tagline": "races, transfers",
  "feeds": ["https://example.org/cycling.rss"],
  "symbols": [],
  "focus": "Race results and team news. Skip rumour."
}
```

Add a branch and a cylinder appears. A branch with `symbols` (Yahoo Finance
tickers) also gets level tiles; one with only `feeds` is headlines alone.

### Why it costs so little

The brief runs on start — the service starts at login — and then once a day. It
deliberately **does not** use the tool-calling agent loop, which spends three to
five model calls answering one question because it has to go and fetch things.
The watchers have already fetched everything, for free, so a brief is a single
completion per branch with the material handed to it.

Three things hold the cost down, in order of effect:

1. **No tools.** One call per branch, not a loop.
2. **Nothing new, nothing spent.** A branch whose watcher found nothing since the
   last brief is skipped entirely — it costs zero calls.
3. **Batching.** Add more branches than `DAILY_BRIEF_MAX_CALLS` and they are
   grouped, so the total never exceeds the budget.

Measured on the three default branches: **3 model calls** for the full brief.

### The engine

The panel leads with a **twin-turbo V8 in WebGL**, modelled on the Koenigsegg:
90-degree banks, the gold intake plenum down the valley, velocity stacks,
turbos outboard.

- **Drag to orbit**, wheel to zoom.
- **Hover a cylinder** and its runner lifts; **click** it to open that branch.
- **Cutaway** makes the bank castings translucent and shows the internals: the
  crankshaft, eight pistons and their rods, driven by real slider-crank motion
  on a cross-plane firing order — not a canned loop.
- The engine idles, and revs while a brief is generating.

Branches map onto cylinders in order; a V8 gives you room for eight. Cylinders
beyond your branch count sit dark, and a spark plug lights only on a cylinder
that has an update to read.

Three.js is **vendored** into `autonomous/web/static/vendor/`, loaded through an
import map. No build step, no CDN, works offline. If WebGL is unavailable the
stage is removed and a flat, keyboard-reachable cylinder row takes over, so the
panel keeps working.

`window.autonomousEngine` is a debug handle in the browser console:
`setRevs(7)`, `setCutaway(true)`, `pistonHeights()`, `frameCount`.

## Layout## Layout

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
├── branches.py      # your branches of interest
├── brief.py         # the daily brief, on a call budget
├── rules.py         # reactive rules: observation -> agent run (off by default)
└── web/             # FastAPI app, auth, event stream, panel
```
