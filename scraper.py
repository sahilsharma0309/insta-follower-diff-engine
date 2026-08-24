"""
scraper.py — pluggable follower-list extraction with rate limiting and retries.

Three interchangeable backends implement one interface (`BaseScraper`):

======================  ====================================================
`demo`  (default)       Deterministic synthetic data. No network, no
                        credentials. Lets the whole pipeline — diff engine,
                        database, dashboard — run and be tested offline.
`playwright`            Real browser. Authenticates once, persists the
                        session cookies, then reads Instagram's own paginated
                        web endpoint the way the site itself does.
`instaloader`           Uses the `instaloader` library if you already have it
                        set up; thin wrapper, same interface.
======================  ====================================================

Select one with `IG_SCRAPER_BACKEND`. Everything above the backend —
`tracker.py`, `app.py` — is written against the interface and never learns
which one it got.

Being a good citizen
--------------------
Follower lists are paginated 50 at a time, so a 20k-follower profile is 400
requests. The defaults here (2.5-6 s randomised gap, 180 requests/hour ceiling,
exponential backoff that honours `Retry-After`) are deliberately slower than
the network allows. Rate limiting is not an inconvenience to route around: it
is what keeps a login usable, and a scraper that backs off politely outlives a
fast one. Note that automated collection is contrary to Instagram's Terms of
Use regardless of pacing — see the README's "Scope and responsible use".
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

from config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Instagram's public web client id. Its private JSON endpoints reject
#: requests without it, answering with HTML instead of JSON.
WEB_APP_ID = "936619743392459"


# ---------------------------------------------------------------------------
# Exceptions — split by whether retrying could possibly help
# ---------------------------------------------------------------------------
class ScraperError(RuntimeError):
    """Base class for every scraping failure."""


class TransientScraperError(ScraperError):
    """A failure worth retrying: network reset, 5xx, malformed partial read."""


class RateLimitedError(TransientScraperError):
    """HTTP 429 or an Instagram 'Please wait a few minutes' body."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AuthenticationError(ScraperError):
    """Session missing, expired, or challenged. Retrying will not fix it."""


class ProfileNotFoundError(ScraperError):
    """The username does not resolve."""


class PrivateProfileError(ScraperError):
    """The profile is private and the logged-in account cannot see it."""


class BackendUnavailableError(ScraperError):
    """The chosen backend's optional dependency is not installed."""


# ---------------------------------------------------------------------------
# Data carriers — the contract between a backend and the rest of the app
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FollowerRecord:
    """One account in a target's follower list."""

    username: str
    user_id: str | None = None
    full_name: str | None = None
    profile_pic_url: str | None = None
    is_private: bool = False
    is_verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "username", self.username.strip().lstrip("@").lower())
        if not self.username:
            raise ValueError("FollowerRecord.username cannot be empty")


@dataclass(frozen=True)
class ProfileInfo:
    """Header metadata for a target profile."""

    username: str
    user_id: str | None = None
    full_name: str | None = None
    biography: str | None = None
    follower_count: int | None = None
    is_private: bool = False
    is_verified: bool = False


@dataclass
class FetchResult:
    """Outcome of one follower-list fetch.

    `complete` is the field that matters most. When a run is truncated — rate
    limited, capped by `IG_MAX_FOLLOWERS_PER_RUN`, or interrupted — the diff
    engine must not interpret the accounts it never reached as unfollows.
    """

    records: list[FollowerRecord] = field(default_factory=list)
    complete: bool = True
    reported_follower_count: int | None = None
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def usernames(self) -> set[str]:
        return {record.username for record in self.records}


# ---------------------------------------------------------------------------
# Politeness primitives
# ---------------------------------------------------------------------------
class RateLimiter:
    """Randomised inter-request delay plus a hard hourly ceiling.

    Two independent brakes:

    1. A jittered sleep between consecutive requests. The jitter matters —
       requests landing exactly 3.000 s apart are a stronger bot signal than
       the request volume itself.
    2. A sliding one-hour window. Once `max_per_hour` requests have gone out,
       `acquire()` blocks until the oldest one ages out of the window.
    """

    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        max_per_hour: int,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_delay > max_delay:
            raise ValueError("min_delay must be <= max_delay")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_per_hour = max_per_hour
        self._sleep = sleep
        self._clock = clock
        self._history: deque[float] = deque()
        self._last_request: float | None = None

    def acquire(self) -> None:
        """Block until it is polite to make the next request."""
        now = self._clock()

        # Brake 2: sliding hourly window.
        cutoff = now - 3600.0
        while self._history and self._history[0] < cutoff:
            self._history.popleft()
        if len(self._history) >= self.max_per_hour:
            wait = self._history[0] + 3600.0 - now
            if wait > 0:
                log.warning(
                    "Hourly request budget (%d) exhausted; pausing %.0f s",
                    self.max_per_hour,
                    wait,
                )
                self._sleep(wait)
                now = self._clock()
                while self._history and self._history[0] < now - 3600.0:
                    self._history.popleft()

        # Brake 1: jittered gap since the previous request.
        if self._last_request is not None:
            target_gap = random.uniform(self.min_delay, self.max_delay)
            elapsed = now - self._last_request
            if elapsed < target_gap:
                self._sleep(target_gap - elapsed)
                now = self._clock()

        self._last_request = now
        self._history.append(now)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "RateLimiter":
        return cls(
            settings.min_request_delay,
            settings.max_request_delay,
            settings.max_requests_per_hour,
            **kwargs,
        )


def retry_with_backoff(
    operation: Callable[[], T],
    *,
    max_retries: int,
    base_seconds: float,
    max_seconds: float,
    description: str = "request",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `operation`, retrying transient failures with exponential backoff.

    Delay grows as ``base * 2**attempt`` and is capped at `max_seconds`, with
    ±25% jitter so that several workers retrying after a shared outage do not
    synchronise into a thundering herd. A `RateLimitedError` carrying a
    `retry_after` always wins over the computed delay — when a server tells you
    how long to wait, arguing with it is how logins get flagged.

    Non-transient errors (auth, 404, private profile) propagate immediately;
    retrying a wrong password just burns attempts toward a lockout.
    """
    attempt = 0
    while True:
        try:
            return operation()
        except TransientScraperError as exc:
            if attempt >= max_retries:
                log.error("%s failed after %d retries: %s", description, attempt, exc)
                raise
            delay = min(base_seconds * (2**attempt), max_seconds)
            server_hint = getattr(exc, "retry_after", None)
            if server_hint:
                delay = max(delay, float(server_hint))
            delay *= random.uniform(0.75, 1.25)
            attempt += 1
            log.warning(
                "%s failed (%s); retry %d/%d in %.1f s",
                description,
                exc,
                attempt,
                max_retries,
                delay,
            )
            sleep(delay)


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------
class BaseScraper(ABC):
    """Interface every backend implements.

    Backends are context managers because the real ones own a browser process
    that must be torn down even when a fetch raises.
    """

    name = "base"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.rate_limiter = RateLimiter.from_settings(self.settings)

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        """Acquire resources (browser, session). Default: nothing to do."""

    def close(self) -> None:
        """Release resources. Must be safe to call twice."""

    def __enter__(self) -> "BaseScraper":
        self.open()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- data --------------------------------------------------------------
    @abstractmethod
    def fetch_profile(self, username: str) -> ProfileInfo:
        """Header metadata for one profile."""

    @abstractmethod
    def iter_followers(self, username: str) -> Iterator[FollowerRecord]:
        """Yield follower records, paginating lazily."""

    # -- shared driver -----------------------------------------------------
    def fetch_followers(self, username: str) -> FetchResult:
        """Drain `iter_followers` into a `FetchResult`, enforcing the run cap.

        Implemented once here so every backend gets identical truncation
        semantics, de-duplication and partial-failure handling. A mid-stream
        error keeps whatever was collected and marks the result incomplete
        rather than discarding the work.
        """
        started = time.monotonic()
        cap = self.settings.max_followers_per_run
        records: list[FollowerRecord] = []
        seen: set[str] = set()
        complete = True
        error: str | None = None

        try:
            for record in self.iter_followers(username):
                if record.username in seen:
                    # Instagram's cursor pagination can repeat a record when
                    # the underlying list shifts mid-scrape.
                    continue
                seen.add(record.username)
                records.append(record)
                if cap and len(records) >= cap:
                    complete = False
                    log.warning(
                        "Stopped at IG_MAX_FOLLOWERS_PER_RUN=%d for %s; "
                        "this snapshot is partial and will not log unfollows.",
                        cap,
                        username,
                    )
                    break
        except (ProfileNotFoundError, PrivateProfileError, AuthenticationError):
            raise
        except ScraperError as exc:
            complete = False
            error = str(exc)
            log.error("Follower fetch for %s ended early: %s", username, exc)

        reported: int | None = None
        try:
            reported = self.fetch_profile(username).follower_count
        except ScraperError:
            log.debug("Could not read reported follower count for %s", username)

        return FetchResult(
            records=records,
            complete=complete,
            reported_follower_count=reported,
            error=error,
            duration_seconds=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# Backend 1: demo — offline, deterministic, no credentials
# ---------------------------------------------------------------------------
_DEMO_WORDS = (
    "aarav", "diya", "kabir", "meera", "rohan", "ananya", "vihaan", "ishita",
    "arjun", "saanvi", "dev", "tara", "nikhil", "priya", "yash", "kavya",
    "aditya", "riya", "manav", "neha", "karan", "sana", "veer", "aisha",
    "raghav", "juhi", "omkar", "zara", "harsh", "myra", "siddh", "anvi",
)
_DEMO_SUFFIX = ("", "_", ".", "01", "07", "_x", "23", "official", "_ig", "99")

#: Reference instant for the demo timeline (2026-01-01T00:00:00Z). The tick is
#: measured from here rather than from the Unix epoch — see `DemoScraper._tick`.
_DEMO_EPOCH = 1767225600.0


class DemoScraper(BaseScraper):
    """Synthetic follower lists that evolve realistically over time.

    Exists so the project is genuinely runnable on a fresh clone: `cli.py
    snapshot` works, the diff engine produces real FOLLOW/UNFOLLOW events, and
    the dashboard has something to draw — all without credentials or network.
    It is also what the test suite runs against, which keeps the tests fast and
    deterministic.

    The simulated set drifts by a "tick", derived from wall-clock time plus a
    per-call counter, so consecutive snapshots differ the way real ones do.
    """

    name = "demo"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_count: int = 120,
        growth_per_tick: int = 1,
        # One minute per tick, so two snapshots a minute apart genuinely differ.
        tick_seconds: int = 60,
        pool_size: int = 700,
        window_ticks: int = 480,
    ) -> None:
        super().__init__(settings)
        self.base_count = base_count
        self.growth_per_tick = growth_per_tick
        self.tick_seconds = tick_seconds
        self.pool_size = pool_size
        #: Ticks before the simulated timeline wraps, keeping state bounded.
        self.window_ticks = window_ticks
        self._call_index = 0
        # The demo backend must never sleep — it makes no requests.
        self.rate_limiter = RateLimiter(0.0, 0.0, 10**9)

    # -- deterministic name pool ------------------------------------------
    def _pool(self, username: str) -> list[str]:
        rng = random.Random(f"pool:{username}")
        names: list[str] = []
        seen: set[str] = set()
        while len(names) < self.pool_size:
            handle = (
                rng.choice(_DEMO_WORDS)
                + rng.choice(_DEMO_WORDS)[:3]
                + rng.choice(_DEMO_SUFFIX)
            )
            if handle not in seen:
                seen.add(handle)
                names.append(handle)
        return names

    def _tick(self) -> int:
        """Counter driving the simulated timeline.

        Bounded on purpose. Deriving it from raw epoch seconds would make it
        ~6 million by 2026, and the simulated state is a function of the tick,
        so an unbounded tick means unbounded work per call.

        The clock is folded into a **triangle wave** rather than a sawtooth:
        the tick ramps 0 → `window_ticks` → 0 and back. A sawtooth would snap
        from a full audience to the starting one in a single step, which reads
        downstream as a mass unfollow; ramping down instead produces a gradual,
        realistic decline. `_call_index` advances it once per fetch so
        back-to-back snapshots in one process always have something to diff.
        """
        elapsed = max(0.0, time.time() - _DEMO_EPOCH)
        phase = int(elapsed // self.tick_seconds) % (2 * self.window_ticks)
        wave = phase if phase < self.window_ticks else 2 * self.window_ticks - phase
        return wave + self._call_index

    def _active_usernames(self, username: str, tick: int) -> list[str]:
        pool = self._pool(username)
        # The audience grows steadily with the tick...
        size = min(self.base_count + tick * self.growth_per_tick, len(pool))
        active = pool[:size]

        # ...while a growing prefix of a fixed random permutation drifts away.
        # Using a prefix makes departures inherently cumulative — an account
        # that left at tick N is still gone at tick N+1 — without looping over
        # every past tick to work that out.
        order = self._departure_order(username)
        departed_count = min(int(tick * 0.55), max(0, size - 10))
        departed = set(order[:departed_count])
        return [name for name in active if name not in departed]

    def _departure_order(self, username: str) -> list[str]:
        """A stable shuffle of the pool: the order in which accounts leave."""
        pool = list(self._pool(username))
        random.Random(f"churn:{username}").shuffle(pool)
        return pool

    # -- interface ---------------------------------------------------------
    def fetch_profile(self, username: str) -> ProfileInfo:
        tick = self._tick()
        rng = random.Random(f"profile:{username}")
        return ProfileInfo(
            username=username,
            user_id=str(rng.randrange(10**9, 10**10)),
            full_name=f"{username.replace('_', ' ').title()} (demo)",
            biography="Synthetic profile generated by the demo backend.",
            follower_count=len(self._active_usernames(username, tick)),
            is_private=False,
            is_verified=False,
        )

    def iter_followers(self, username: str) -> Iterator[FollowerRecord]:
        tick = self._tick()
        self._call_index += 1  # next call sees a slightly later timeline
        for handle in self._active_usernames(username, tick):
            rng = random.Random(f"user:{handle}")
            yield FollowerRecord(
                username=handle,
                user_id=str(rng.randrange(10**9, 10**10)),
                full_name=handle.replace("_", " ").replace(".", " ").title(),
                profile_pic_url=f"https://picsum.photos/seed/{handle}/150",
                is_private=rng.random() < 0.35,
                is_verified=rng.random() < 0.03,
            )


# ---------------------------------------------------------------------------
# Backend 2: Playwright
# ---------------------------------------------------------------------------
class PlaywrightScraper(BaseScraper):
    """Real extraction through a headless Chromium session.

    Approach: authenticate once in a real browser, persist the resulting
    cookies (`storage_state`), then issue the same paginated JSON request the
    Instagram web app issues for its own followers dialog:

        GET /api/v1/friendships/<user_id>/followers/?count=50&max_id=<cursor>

    Reading that endpoint from inside the authenticated browser context is far
    more robust than scrolling and scraping the DOM: no virtualised-list
    races, no class names that change weekly, and a real cursor instead of
    "scroll until the height stops growing". A DOM fallback is kept for the
    case where the endpoint shape changes.

    Session persistence is the point. Every fresh login is a security event on
    the account; reusing a stored session means one login instead of one per
    run.
    """

    name = "playwright"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._profile_cache: dict[str, ProfileInfo] = {}

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendUnavailableError(
                "The playwright backend needs the package and a browser:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            ) from exc

        settings = self.settings
        settings.validate_for_live_scrape()
        settings.ensure_directories()

        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": settings.headless}
        if settings.browser_channel:
            launch_kwargs["channel"] = settings.browser_channel
        # Proxy hook: routes both navigation and the JSON calls below.
        if settings.proxy_config:
            launch_kwargs["proxy"] = settings.proxy_config
            log.info("Routing browser traffic through %s", settings.proxy_server)

        self._browser = self._playwright.chromium.launch(**launch_kwargs)

        context_kwargs: dict[str, Any] = {
            "user_agent": settings.user_agent,
            "locale": settings.locale,
            "timezone_id": settings.timezone_id,
            "viewport": {"width": 1440, "height": 900},
        }
        state_path = settings.session_state_path
        if state_path.exists():
            log.info("Reusing stored session from %s", state_path)
            context_kwargs["storage_state"] = str(state_path)

        self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(settings.request_timeout_seconds * 1000)
        # Headless Chromium advertises `navigator.webdriver`, which Instagram's
        # web app uses to serve a degraded, unparseable page. Clearing it gets
        # us the standard response; it is not an anti-detection suite.
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = self._context.new_page()
        self._ensure_authenticated()

    def close(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is None:
                continue
            try:
                closer()
            except Exception as exc:  # pragma: no cover - teardown best effort
                log.debug("Ignoring teardown error: %s", exc)
        self._playwright = self._browser = self._context = self._page = None

    # -- auth --------------------------------------------------------------
    def _has_session_cookie(self) -> bool:
        cookies = self._context.cookies("https://www.instagram.com")
        return any(c.get("name") == "sessionid" and c.get("value") for c in cookies)

    def _save_session(self) -> None:
        path = self.settings.session_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(path))
        try:
            path.chmod(0o600)  # cookies are credential-equivalent
        except OSError:  # pragma: no cover - Windows
            pass
        log.info("Saved session cookies to %s", path)

    def _ensure_authenticated(self) -> None:
        """Reuse the stored session if it is still valid, else log in once."""
        self._page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        if self._has_session_cookie():
            log.info("Existing session accepted; skipping login")
            return

        if not self.settings.ig_password:
            raise AuthenticationError(
                "No stored session and IG_PASSWORD is unset. Set IG_USERNAME/"
                "IG_PASSWORD once to mint a session, or drop an existing "
                f"storage_state JSON at {self.settings.session_state_path}."
            )
        self._login()

    def _login(self) -> None:
        page = self._page
        log.info("Logging in as %s", self.settings.ig_username)
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
        try:
            page.fill("input[name='username']", self.settings.ig_username)
            page.fill("input[name='password']", self.settings.ig_password)
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")
        except Exception as exc:
            raise AuthenticationError(f"Login form interaction failed: {exc}") from exc

        # A challenge (2FA, "suspicious login", email code) cannot be resolved
        # unattended. Say so plainly instead of retrying into a lockout.
        if "challenge" in page.url or "two_factor" in page.url:
            raise AuthenticationError(
                "Instagram issued a login challenge. Re-run with IG_HEADLESS=false, "
                "complete the challenge by hand, and the session will be saved "
                "for subsequent runs."
            )
        if not self._has_session_cookie():
            raise AuthenticationError(
                "Login did not produce a session cookie — check the credentials."
            )
        self._save_session()

    # -- HTTP --------------------------------------------------------------
    def _api_headers(self, referer: str) -> dict[str, str]:
        return {
            "x-ig-app-id": WEB_APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json",
            "referer": referer,
        }

    def _get_json(self, url: str, *, referer: str) -> dict[str, Any]:
        """One rate-limited, error-classified JSON GET inside the browser context."""
        self.rate_limiter.acquire()
        try:
            response = self._context.request.get(
                url,
                headers=self._api_headers(referer),
                timeout=self.settings.request_timeout_seconds * 1000,
            )
        except Exception as exc:  # network layer: worth a retry
            raise TransientScraperError(f"Request to {url} failed: {exc}") from exc

        status = response.status
        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimitedError(
                "Rate limited by Instagram (HTTP 429)",
                retry_after=float(retry_after) if retry_after else None,
            )
        if status in (401, 403):
            raise AuthenticationError(
                f"HTTP {status} — the session is no longer valid. Delete "
                f"{self.settings.session_state_path} and log in again."
            )
        if status == 404:
            raise ProfileNotFoundError(f"HTTP 404 for {url}")
        if status >= 500:
            raise TransientScraperError(f"HTTP {status} from Instagram")
        if status != 200:
            raise ScraperError(f"Unexpected HTTP {status} for {url}")

        try:
            payload = response.json()
        except Exception as exc:
            # An HTML body here almost always means a soft block or a login
            # wall served with a 200 status.
            body = (response.text() or "")[:200]
            if "wait a few minutes" in body.lower():
                raise RateLimitedError("Instagram asked us to wait a few minutes") from exc
            raise TransientScraperError(
                f"Expected JSON from {url}, got: {body!r}"
            ) from exc

        if isinstance(payload, dict) and payload.get("status") == "fail":
            message = str(payload.get("message", "")).lower()
            if "wait" in message or "limit" in message:
                raise RateLimitedError(f"Instagram: {payload.get('message')}")
            raise ScraperError(f"Instagram returned failure: {payload.get('message')}")
        return payload if isinstance(payload, dict) else {}

    def _get_json_with_retry(self, url: str, *, referer: str) -> dict[str, Any]:
        return retry_with_backoff(
            lambda: self._get_json(url, referer=referer),
            max_retries=self.settings.max_retries,
            base_seconds=self.settings.backoff_base_seconds,
            max_seconds=self.settings.backoff_max_seconds,
            description=f"GET {url.split('?')[0]}",
        )

    # -- interface ---------------------------------------------------------
    def fetch_profile(self, username: str) -> ProfileInfo:
        username = username.strip().lstrip("@").lower()
        if username in self._profile_cache:
            return self._profile_cache[username]

        payload = self._get_json_with_retry(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
            referer=f"https://www.instagram.com/{username}/",
        )
        user = (payload.get("data") or {}).get("user")
        if not user:
            raise ProfileNotFoundError(f"No profile data returned for @{username}")

        info = ProfileInfo(
            username=user.get("username", username),
            user_id=str(user.get("id")) if user.get("id") else None,
            full_name=user.get("full_name"),
            biography=user.get("biography"),
            follower_count=(user.get("edge_followed_by") or {}).get("count"),
            is_private=bool(user.get("is_private")),
            is_verified=bool(user.get("is_verified")),
        )
        self._profile_cache[username] = info
        return info

    def iter_followers(self, username: str) -> Iterator[FollowerRecord]:
        profile = self.fetch_profile(username)
        if not profile.user_id:
            raise ScraperError(f"Could not resolve a numeric id for @{username}")
        if profile.is_private:
            # Private profiles are visible only to approved followers; if the
            # logged-in account is not one, there is nothing to read.
            log.warning(
                "@%s is private — the follower list is only readable if the "
                "logged-in account already follows it.",
                username,
            )

        referer = f"https://www.instagram.com/{username}/"
        cursor: str | None = None
        page_number = 0

        while True:
            url = (
                f"https://www.instagram.com/api/v1/friendships/{profile.user_id}"
                f"/followers/?count={self.settings.page_size}&search_surface=follow_list_page"
            )
            if cursor:
                url += f"&max_id={cursor}"

            payload = self._get_json_with_retry(url, referer=referer)
            users = payload.get("users") or []
            page_number += 1
            log.debug("Page %d for @%s: %d records", page_number, username, len(users))

            if not users:
                if page_number == 1 and profile.is_private:
                    raise PrivateProfileError(
                        f"@{username} is private and its followers are not visible "
                        "to this account."
                    )
                return

            for user in users:
                handle = user.get("username")
                if not handle:
                    continue
                yield FollowerRecord(
                    username=handle,
                    user_id=str(user.get("pk") or user.get("id") or "") or None,
                    full_name=user.get("full_name") or None,
                    profile_pic_url=user.get("profile_pic_url") or None,
                    is_private=bool(user.get("is_private")),
                    is_verified=bool(user.get("is_verified")),
                )

            cursor = payload.get("next_max_id")
            if not cursor:
                return  # last page
            cursor = str(cursor)

    # -- fallback ----------------------------------------------------------
    def iter_followers_via_dom(self, username: str) -> Iterator[FollowerRecord]:
        """Scroll the followers dialog and read anchors out of the DOM.

        Kept as a documented escape hatch: if Instagram changes the JSON
        endpoint above, this path still works, at the cost of losing the
        metadata (ids, verified flags) the JSON carries. Not used by default.
        """
        page = self._page
        page.goto(
            f"https://www.instagram.com/{username}/followers/",
            wait_until="domcontentloaded",
        )
        dialog = page.wait_for_selector("div[role='dialog']")
        seen: set[str] = set()
        stagnant_rounds = 0

        while stagnant_rounds < 5:
            handles = page.eval_on_selector_all(
                "div[role='dialog'] a[href^='/']",
                "els => els.map(e => e.getAttribute('href'))",
            )
            fresh = 0
            for href in handles:
                handle = (href or "").strip("/").split("/")[0].lower()
                if handle and handle not in seen and "." not in href.strip("/")[:1]:
                    seen.add(handle)
                    fresh += 1
                    yield FollowerRecord(username=handle)
            stagnant_rounds = 0 if fresh else stagnant_rounds + 1
            self.rate_limiter.acquire()
            dialog.evaluate("el => { const s = el.querySelector('div[style]'); "
                            "(s || el).scrollBy(0, 1200); }")


# ---------------------------------------------------------------------------
# Backend 3: Instaloader
# ---------------------------------------------------------------------------
class InstaloaderScraper(BaseScraper):
    """Thin adapter over the `instaloader` library.

    Instaloader already handles Instagram's GraphQL pagination and session
    files, so this backend only adds our rate limiter and maps its objects
    onto `FollowerRecord`. Useful if you already have an `instaloader` session
    on disk and would rather not run a browser.
    """

    name = "instaloader"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._loader: Any = None

    def open(self) -> None:
        try:
            import instaloader
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendUnavailableError(
                "The instaloader backend needs: pip install instaloader"
            ) from exc

        settings = self.settings
        settings.validate_for_live_scrape()
        settings.ensure_directories()

        self._loader = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_comments=False,
            save_metadata=False,
            user_agent=settings.user_agent,
            # instaloader's own pacing, on top of our RateLimiter.
            request_timeout=settings.request_timeout_seconds,
            max_connection_attempts=max(1, settings.max_retries),
        )
        if settings.proxy_server:
            # instaloader delegates to requests, which reads these env-style keys.
            self._loader.context._session.proxies.update(
                {"http": settings.proxy_server, "https": settings.proxy_server}
            )

        session_file = settings.instaloader_session_path
        if session_file.exists():
            self._loader.load_session_from_file(settings.ig_username, str(session_file))
            log.info("Loaded instaloader session from %s", session_file)
        elif settings.ig_password:
            try:
                self._loader.login(settings.ig_username, settings.ig_password)
            except Exception as exc:
                raise AuthenticationError(f"instaloader login failed: {exc}") from exc
            self._loader.save_session_to_file(str(session_file))
            try:
                session_file.chmod(0o600)
            except OSError:  # pragma: no cover - Windows
                pass
        else:
            raise AuthenticationError(
                "No instaloader session file and no IG_PASSWORD to create one."
            )

    def close(self) -> None:
        if self._loader is not None:
            try:
                self._loader.close()
            except Exception as exc:  # pragma: no cover - teardown best effort
                log.debug("Ignoring instaloader teardown error: %s", exc)
        self._loader = None

    def _profile(self, username: str) -> Any:
        import instaloader

        def load() -> Any:
            self.rate_limiter.acquire()
            try:
                return instaloader.Profile.from_username(self._loader.context, username)
            except instaloader.exceptions.ProfileNotExistsException as exc:
                raise ProfileNotFoundError(f"@{username} does not exist") from exc
            except instaloader.exceptions.LoginRequiredException as exc:
                raise AuthenticationError(str(exc)) from exc
            except instaloader.exceptions.TooManyRequestsException as exc:
                raise RateLimitedError(str(exc)) from exc
            except instaloader.exceptions.ConnectionException as exc:
                raise TransientScraperError(str(exc)) from exc

        return retry_with_backoff(
            load,
            max_retries=self.settings.max_retries,
            base_seconds=self.settings.backoff_base_seconds,
            max_seconds=self.settings.backoff_max_seconds,
            description=f"instaloader profile @{username}",
        )

    def fetch_profile(self, username: str) -> ProfileInfo:
        profile = self._profile(username.strip().lstrip("@").lower())
        return ProfileInfo(
            username=profile.username,
            user_id=str(profile.userid),
            full_name=profile.full_name,
            biography=profile.biography,
            follower_count=profile.followers,
            is_private=profile.is_private,
            is_verified=profile.is_verified,
        )

    def iter_followers(self, username: str) -> Iterator[FollowerRecord]:
        import instaloader

        profile = self._profile(username.strip().lstrip("@").lower())
        if profile.is_private and not profile.followed_by_viewer:
            raise PrivateProfileError(
                f"@{username} is private and this account does not follow it."
            )
        try:
            for follower in profile.get_followers():
                # instaloader pages internally; pace ourselves per record so a
                # long list still respects the hourly budget.
                self.rate_limiter.acquire()
                yield FollowerRecord(
                    username=follower.username,
                    user_id=str(follower.userid),
                    full_name=follower.full_name or None,
                    profile_pic_url=follower.profile_pic_url or None,
                    is_private=follower.is_private,
                    is_verified=follower.is_verified,
                )
        except instaloader.exceptions.TooManyRequestsException as exc:
            raise RateLimitedError(str(exc)) from exc
        except instaloader.exceptions.LoginRequiredException as exc:
            raise AuthenticationError(str(exc)) from exc
        except instaloader.exceptions.ConnectionException as exc:
            raise TransientScraperError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_BACKENDS: dict[str, type[BaseScraper]] = {
    "demo": DemoScraper,
    "playwright": PlaywrightScraper,
    "instaloader": InstaloaderScraper,
}


def build_scraper(
    settings: Settings | None = None, *, backend: str | None = None
) -> BaseScraper:
    """Construct the configured backend.

    `backend` overrides `IG_SCRAPER_BACKEND`, which is how `cli.py --backend`
    works.
    """
    settings = settings or get_settings()
    chosen = (backend or settings.scraper_backend or "demo").lower()
    try:
        scraper_cls = _BACKENDS[chosen]
    except KeyError as exc:
        raise ScraperError(
            f"Unknown scraper backend {chosen!r}. Choose one of {sorted(_BACKENDS)}."
        ) from exc
    log.info("Using the %s scraper backend", chosen)
    return scraper_cls(settings)


__all__ = [
    "AuthenticationError",
    "BackendUnavailableError",
    "BaseScraper",
    "DemoScraper",
    "FetchResult",
    "FollowerRecord",
    "InstaloaderScraper",
    "PlaywrightScraper",
    "PrivateProfileError",
    "ProfileInfo",
    "ProfileNotFoundError",
    "RateLimitedError",
    "RateLimiter",
    "ScraperError",
    "TransientScraperError",
    "build_scraper",
    "retry_with_backoff",
]
