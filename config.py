"""
config.py — every tunable knob in one place, loaded from the environment.

Design rules followed here:

* **Nothing secret is hard-coded.** Credentials, proxies and the database URL
  come from environment variables (or a gitignored `.env`), never from source.
* **The module imports cleanly with no environment at all.** Every setting has
  a working default so `python -m instagram_tracker.cli seed-demo` runs on a
  fresh clone. Validation that *would* fail without credentials is deferred to
  `Settings.validate_for_live_scrape()`, which only the live scraper calls.
* **Values are parsed once, here.** Callers get typed attributes, not strings,
  so nobody downstream re-implements "is this env var truthy?".

Usage::

    from config import get_settings
    settings = get_settings()          # cached singleton
    settings = get_settings(refresh=True)   # re-read the environment
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a hard requirement
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        """No-op stand-in so the module still imports without python-dotenv."""
        return False


# --- Paths -----------------------------------------------------------------
# Everything runtime-generated lives under PROJECT_ROOT so a single directory
# delete resets the tool completely.
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "runtime"

# Load `.env` from the package directory first, then the repo root, so the
# tracker works whether you run it from inside the folder or from the repo top.
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")


# --- Small env parsing helpers ---------------------------------------------
_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}


def _env_str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None or value.strip() == "" else value.strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = _env_str(key)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ConfigError(f"{key}={raw!r} is not a boolean (use true/false)")


def _env_int(key: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key}={value} is below the minimum of {minimum}")
    return value


def _env_float(key: str, default: float, *, minimum: float | None = None) -> float:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not a number") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key}={value} is below the minimum of {minimum}")
    return value


def _env_list(key: str, default: tuple[str, ...] = ()) -> list[str]:
    """Parse a comma-separated env var into a de-duplicated, ordered list."""
    raw = _env_str(key)
    if not raw:
        return list(default)
    seen: dict[str, None] = {}
    for chunk in raw.replace("\n", ",").split(","):
        item = chunk.strip().lstrip("@")
        if item:
            seen.setdefault(item.lower(), None)
    return list(seen)


class ConfigError(RuntimeError):
    """Raised when an environment variable is present but unusable."""


# --- Scrape scheduling -----------------------------------------------------
#: Friendly interval names accepted by IG_SCRAPE_INTERVAL, in minutes.
INTERVAL_ALIASES: dict[str, int] = {
    "15min": 15,
    "30min": 30,
    "hourly": 60,
    "6hourly": 360,
    "12hourly": 720,
    "daily": 1440,
    "weekly": 10080,
}


def parse_interval(raw: str) -> int:
    """Turn ``"hourly"`` / ``"90"`` / ``"90m"`` into a minute count."""
    text = raw.strip().lower()
    if text in INTERVAL_ALIASES:
        return INTERVAL_ALIASES[text]
    if text.endswith("m"):
        text = text[:-1]
    elif text.endswith("h"):
        try:
            return int(float(text[:-1]) * 60)
        except ValueError as exc:
            raise ConfigError(f"Unrecognised interval {raw!r}") from exc
    try:
        minutes = int(text)
    except ValueError as exc:
        raise ConfigError(
            f"Unrecognised interval {raw!r}. Use one of "
            f"{sorted(INTERVAL_ALIASES)} or a number of minutes."
        ) from exc
    if minutes < 1:
        raise ConfigError("Scrape interval must be at least 1 minute")
    return minutes


@dataclass(frozen=True)
class TargetConfig:
    """A profile to monitor, as declared in configuration (not the DB)."""

    username: str
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "username", self.username.strip().lstrip("@").lower())
        if not self.label:
            object.__setattr__(self, "label", self.username)


@dataclass(frozen=True)
class Settings:
    """Fully-resolved runtime configuration."""

    # --- Storage ----------------------------------------------------------
    database_url: str
    data_dir: Path
    session_dir: Path
    export_dir: Path
    log_level: str

    # --- Targets ----------------------------------------------------------
    targets: tuple[TargetConfig, ...]

    # --- Scraper selection ------------------------------------------------
    #: "demo" | "playwright" | "instaloader"
    scraper_backend: str
    ig_username: str
    ig_password: str
    headless: bool
    browser_channel: str
    user_agent: str
    locale: str
    timezone_id: str

    # --- Politeness / anti-block -----------------------------------------
    #: Randomised sleep bounds between paginated requests, in seconds.
    min_request_delay: float
    max_request_delay: float
    #: Hard ceiling on requests per hour per process; the limiter blocks above it.
    max_requests_per_hour: int
    #: Retry policy for transient failures (429 / 5xx / network resets).
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    request_timeout_seconds: float
    #: Stop paginating a follower list after this many records (0 = unlimited).
    max_followers_per_run: int
    page_size: int

    # --- Proxy ------------------------------------------------------------
    proxy_server: str
    proxy_username: str
    proxy_password: str

    # --- Scheduling -------------------------------------------------------
    scrape_interval_minutes: int
    jitter_seconds: int
    run_on_start: bool

    # --- Diff safety ------------------------------------------------------
    #: If a snapshot loses more than this fraction of the known follower set,
    #: treat it as a truncated scrape and refuse to log UNFOLLOW events.
    max_unfollow_ratio: float
    #: ...but only once the loss is at least this many accounts. On a small
    #: page a ratio carries no signal — losing 2 of 3 followers is 67% and
    #: entirely ordinary — so the guard needs an absolute floor as well.
    min_unfollows_for_guard: int

    # --- Derived ----------------------------------------------------------
    extra: dict[str, str] = field(default_factory=dict, repr=False)

    # -- Derived helpers ---------------------------------------------------
    @property
    def proxy_config(self) -> dict[str, str] | None:
        """Playwright-shaped proxy dict, or None when no proxy is configured."""
        if not self.proxy_server:
            return None
        proxy: dict[str, str] = {"server": self.proxy_server}
        if self.proxy_username:
            proxy["username"] = self.proxy_username
            proxy["password"] = self.proxy_password
        return proxy

    @property
    def session_state_path(self) -> Path:
        """Where Playwright's `storage_state` (cookies) is cached on disk.

        Keyed by login account so multiple accounts can coexist. This file is
        credential-equivalent — it is gitignored, and `ensure_directories()`
        creates its parent with owner-only permissions.
        """
        stem = self.ig_username or "anonymous"
        return self.session_dir / f"{stem}.storage.json"

    @property
    def instaloader_session_path(self) -> Path:
        stem = self.ig_username or "anonymous"
        return self.session_dir / f"{stem}.instaloader"

    def ensure_directories(self) -> None:
        """Create the runtime directories; tighten permissions on secrets."""
        for path in (self.data_dir, self.session_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)
        try:
            # Cookies are as good as a password: keep them owner-readable only.
            self.session_dir.chmod(0o700)
        except OSError:  # pragma: no cover - Windows / exotic filesystems
            pass

    def validate_for_live_scrape(self) -> None:
        """Fail fast, with an actionable message, before touching the network.

        Called only by the live backends — the demo backend deliberately runs
        with no credentials at all.
        """
        problems: list[str] = []
        if self.scraper_backend not in SCRAPER_BACKENDS:
            problems.append(
                f"IG_SCRAPER_BACKEND={self.scraper_backend!r} is not one of "
                f"{sorted(SCRAPER_BACKENDS)}"
            )
        if self.scraper_backend != "demo" and not self.ig_username:
            problems.append(
                "IG_USERNAME is required for live scraping — Instagram does not "
                "serve follower lists to logged-out clients."
            )
        if self.min_request_delay > self.max_request_delay:
            problems.append(
                "IG_MIN_REQUEST_DELAY must not exceed IG_MAX_REQUEST_DELAY"
            )
        if not self.targets:
            problems.append(
                "No targets configured. Set IG_TARGETS=profile_one,profile_two "
                "or add one with `python cli.py add-target <username>`."
            )
        if problems:
            raise ConfigError(
                "Configuration is not usable for live scraping:\n  - "
                + "\n  - ".join(problems)
            )


#: Backends that `scraper.build_scraper()` knows how to construct.
SCRAPER_BACKENDS = frozenset({"demo", "playwright", "instaloader"})

#: A current desktop Chrome UA. Instagram's web endpoints vary their response
#: shape for clients they consider ancient, so this is about getting a parseable
#: response, not about disguising the client.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def load_settings() -> Settings:
    """Read the environment and build a `Settings` instance."""
    data_dir = Path(_env_str("IG_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()

    # Default to a SQLite file inside the runtime dir. Point DATABASE_URL at
    # Postgres (postgresql+psycopg2://user:pass@host/db) and nothing else in
    # the codebase needs to change — the ORM layer is dialect-agnostic.
    database_url = _env_str(
        "IG_DATABASE_URL", f"sqlite:///{(data_dir / 'tracker.db').as_posix()}"
    )

    targets = tuple(TargetConfig(username=name) for name in _env_list("IG_TARGETS"))

    min_delay = _env_float("IG_MIN_REQUEST_DELAY", 2.5, minimum=0.0)
    max_delay = _env_float("IG_MAX_REQUEST_DELAY", 6.0, minimum=0.0)

    settings = Settings(
        database_url=database_url,
        data_dir=data_dir,
        session_dir=Path(_env_str("IG_SESSION_DIR", str(data_dir / "sessions"))),
        export_dir=Path(_env_str("IG_EXPORT_DIR", str(data_dir / "exports"))),
        log_level=_env_str("IG_LOG_LEVEL", "INFO").upper(),
        targets=targets,
        scraper_backend=_env_str("IG_SCRAPER_BACKEND", "demo").lower(),
        ig_username=_env_str("IG_USERNAME").lstrip("@").lower(),
        ig_password=_env_str("IG_PASSWORD"),
        headless=_env_bool("IG_HEADLESS", True),
        browser_channel=_env_str("IG_BROWSER_CHANNEL"),
        user_agent=_env_str("IG_USER_AGENT", DEFAULT_USER_AGENT),
        locale=_env_str("IG_LOCALE", "en-US"),
        timezone_id=_env_str("IG_TIMEZONE_ID", "Asia/Kolkata"),
        min_request_delay=min_delay,
        max_request_delay=max_delay,
        max_requests_per_hour=_env_int("IG_MAX_REQUESTS_PER_HOUR", 180, minimum=1),
        max_retries=_env_int("IG_MAX_RETRIES", 5, minimum=0),
        backoff_base_seconds=_env_float("IG_BACKOFF_BASE", 3.0, minimum=0.1),
        backoff_max_seconds=_env_float("IG_BACKOFF_MAX", 300.0, minimum=1.0),
        request_timeout_seconds=_env_float("IG_REQUEST_TIMEOUT", 45.0, minimum=1.0),
        max_followers_per_run=_env_int("IG_MAX_FOLLOWERS_PER_RUN", 0, minimum=0),
        page_size=_env_int("IG_PAGE_SIZE", 50, minimum=1),
        proxy_server=_env_str("IG_PROXY_SERVER"),
        proxy_username=_env_str("IG_PROXY_USERNAME"),
        proxy_password=_env_str("IG_PROXY_PASSWORD"),
        scrape_interval_minutes=parse_interval(_env_str("IG_SCRAPE_INTERVAL", "hourly")),
        jitter_seconds=_env_int("IG_SCHEDULE_JITTER", 180, minimum=0),
        run_on_start=_env_bool("IG_RUN_ON_START", True),
        max_unfollow_ratio=_env_float("IG_MAX_UNFOLLOW_RATIO", 0.5, minimum=0.0),
        min_unfollows_for_guard=_env_int("IG_MIN_UNFOLLOWS_FOR_GUARD", 10, minimum=1),
    )
    return settings


# --- Cached accessor -------------------------------------------------------
_CACHED: Settings | None = None


def get_settings(*, refresh: bool = False) -> Settings:
    """Return the process-wide `Settings`, building it on first use.

    Streamlit re-executes `app.py` top to bottom on every interaction, so the
    cache matters: without it every widget click would re-parse the whole
    environment.
    """
    global _CACHED
    if _CACHED is None or refresh:
        _CACHED = load_settings()
    return _CACHED


__all__ = [
    "ConfigError",
    "DEFAULT_USER_AGENT",
    "INTERVAL_ALIASES",
    "PROJECT_ROOT",
    "SCRAPER_BACKENDS",
    "Settings",
    "TargetConfig",
    "get_settings",
    "load_settings",
    "parse_interval",
]
