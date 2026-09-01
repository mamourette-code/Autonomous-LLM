"""Environment-driven configuration.

Every secret is read from the environment (optionally via a local .env file that
is never committed). Anything left unset simply disables the feature that needs
it, so the app always starts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM providers -----------------------------------------------------
    # provider is the default used for agent runs; each provider needs its key.
    provider: str = "gemini"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"

    # --- Storage -----------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"

    # --- Agent limits ------------------------------------------------------
    # Hard ceiling on tool-calling iterations, so a confused run cannot spin
    # forever or burn tokens unattended.
    max_steps: int = 12
    # Generous, because a step may wait out a rate limit before it even starts.
    step_timeout_seconds: float = 300.0
    # Free Gemini tiers cap requests per minute, and a busy model returns 503.
    # Both are retried, honouring the delay the API asks for.
    provider_max_retries: int = 2
    provider_retry_max_wait: float = 65.0

    # --- Daily brief -------------------------------------------------------
    # One update per branch, generated once a day. Each branch costs a single
    # model call - the watchers have already gathered the material for free -
    # and a branch with nothing new costs none at all. If you add more branches
    # than the budget allows, they are batched so the total never exceeds it.
    daily_brief_enabled: bool = True
    daily_brief_max_calls: int = 5
    # Wait this long after start for the first watcher polls to land.
    daily_brief_startup_delay_seconds: float = 25.0

    # --- Reactive rules ----------------------------------------------------
    # Off: the daily brief is the update path. Rules fire agent runs whenever a
    # watcher sees something matching, which spends unpredictably. Turn on only
    # with a rules.json and an eye on the budget.
    rules_enabled: bool = False
    max_auto_runs_per_day: int = 12

    # --- Tools -------------------------------------------------------------
    http_timeout_seconds: float = 30.0
    http_max_bytes: int = 2_000_000
    user_agent: str = "autonomous-llm/0.1 (+https://github.com/mamourette-code/Autonomous-LLM)"
    # Browser tools can *read* pages by default. Clicking, typing and submitting
    # forms on real sites is off until you explicitly turn it on.
    browser_enabled: bool = False
    browser_actions_enabled: bool = False

    # --- Watchers (the continuously running half) --------------------------
    watchers_enabled: bool = True

    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_mailbox: str = "INBOX"
    email_poll_seconds: int = 300

    # Branches of interest live in branches.json (see branches.example.json),
    # which also carries their feeds and symbols.

    # --- Web UI ------------------------------------------------------------
    # 127.0.0.1 keeps the panel on this machine. Change it only together with
    # auth_token - see the note there.
    host: str = "127.0.0.1"
    port: int = 8000

    # Shared access token. Unset means no authentication, which is only safe
    # while the panel is bound to localhost. Any other binding must set it.
    auth_token: str | None = None
    # Send the session cookie only over HTTPS. Turn this on once the panel is
    # served over TLS (directly or behind a reverse proxy).
    cookie_secure: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autonomous.db"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
