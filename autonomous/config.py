"""Environment-driven configuration.

Every secret is read from the environment (optionally via a local .env file that
is never committed). Anything left unset simply disables the feature that needs
it, so the app always starts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM providers -----------------------------------------------------
    # provider is the default used for agent runs; each provider needs its key.
    provider: str = "gemini"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"

    # --- Storage -----------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"

    # --- Agent limits ------------------------------------------------------
    # Hard ceiling on tool-calling iterations, so a confused run cannot spin
    # forever or burn tokens unattended.
    max_steps: int = 12
    step_timeout_seconds: float = 120.0

    # --- Reactive rules ----------------------------------------------------
    # Rules start agent runs on their own when a watcher sees something
    # matching. This is the only path that spends money unprompted, so it is
    # capped: when the daily budget is spent, rules stop firing until midnight
    # UTC. Set rules_enabled=False to stop them entirely.
    rules_enabled: bool = True
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

    # Financial markets: index/FX/commodity levels and market headlines.
    # Both are keyless. Empty either list to switch that half off.
    markets_symbols: list[str] = Field(
        default_factory=lambda: [
            "^GSPC",  # S&P 500
            "^IXIC",  # Nasdaq Composite
            "^DJI",  # Dow Jones
            "^FTSE",  # FTSE 100
            "^STOXX50E",  # Euro Stoxx 50
            "EURUSD=X",
            "GC=F",  # Gold
            "CL=F",  # Crude oil
            "BTC-USD",
        ]
    )
    markets_feed_urls: list[str] = Field(
        default_factory=lambda: [
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
            "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
        ]
    )
    markets_poll_seconds: int = 3600

    # Any other RSS/Atom URLs you want watched, as a JSON list.
    feed_urls: list[str] = Field(default_factory=list)
    feed_poll_seconds: int = 1800

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
