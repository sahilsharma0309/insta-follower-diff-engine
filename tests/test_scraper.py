"""Tests for the politeness primitives and the demo backend."""

from __future__ import annotations

import pytest

from scraper import (
    AuthenticationError,
    DemoScraper,
    FetchResult,
    FollowerRecord,
    RateLimitedError,
    RateLimiter,
    ScraperError,
    TransientScraperError,
    build_scraper,
    retry_with_backoff,
)


# ---------------------------------------------------------------------------
# FollowerRecord
# ---------------------------------------------------------------------------
class TestFollowerRecord:
    def test_normalises_handle(self):
        assert FollowerRecord(username="  @Alice ").username == "alice"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            FollowerRecord(username="   ")

    def test_fetch_result_usernames_is_a_set(self):
        result = FetchResult(
            records=[FollowerRecord(username="a"), FollowerRecord(username="b")]
        )
        assert result.usernames == {"a", "b"}


# ---------------------------------------------------------------------------
# RateLimiter — a fake clock keeps the tests instant
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestRateLimiter:
    def test_first_request_does_not_sleep(self):
        clock = FakeClock()
        limiter = RateLimiter(2.0, 4.0, 100, sleep=clock.sleep, clock=clock.time)
        limiter.acquire()
        assert clock.slept == []

    def test_consecutive_requests_are_spaced(self):
        clock = FakeClock()
        limiter = RateLimiter(2.0, 4.0, 100, sleep=clock.sleep, clock=clock.time)
        limiter.acquire()
        limiter.acquire()
        assert len(clock.slept) == 1
        assert 0 < clock.slept[0] <= 4.0

    def test_elapsed_time_counts_towards_the_gap(self):
        clock = FakeClock()
        limiter = RateLimiter(2.0, 2.0, 100, sleep=clock.sleep, clock=clock.time)
        limiter.acquire()
        clock.now += 5.0  # caller already spent longer than the required gap
        limiter.acquire()
        assert clock.slept == [], "no extra sleep when enough time already passed"

    def test_hourly_budget_blocks_until_the_window_slides(self):
        clock = FakeClock()
        limiter = RateLimiter(0.0, 0.0, 3, sleep=clock.sleep, clock=clock.time)
        for _ in range(3):
            limiter.acquire()
        assert clock.slept == []

        limiter.acquire()  # the fourth exceeds the budget
        assert len(clock.slept) == 1
        assert clock.slept[0] == pytest.approx(3600.0)

    def test_rejects_inverted_bounds(self):
        with pytest.raises(ValueError):
            RateLimiter(10.0, 1.0, 100)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
class TestRetryWithBackoff:
    def test_returns_immediately_on_success(self):
        calls = []

        def op():
            calls.append(1)
            return "ok"

        assert retry_with_backoff(op, max_retries=3, base_seconds=1, max_seconds=10) == "ok"
        assert len(calls) == 1

    def test_retries_transient_then_succeeds(self):
        slept: list[float] = []
        attempts = {"n": 0}

        def op():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TransientScraperError("boom")
            return "recovered"

        result = retry_with_backoff(
            op, max_retries=5, base_seconds=2, max_seconds=60, sleep=slept.append
        )
        assert result == "recovered"
        assert attempts["n"] == 3
        assert len(slept) == 2
        # Exponential growth (with ±25% jitter, so compare loosely).
        assert slept[1] > slept[0]

    def test_gives_up_after_max_retries(self):
        slept: list[float] = []

        def op():
            raise TransientScraperError("always down")

        with pytest.raises(TransientScraperError):
            retry_with_backoff(
                op, max_retries=2, base_seconds=1, max_seconds=10, sleep=slept.append
            )
        assert len(slept) == 2, "one sleep per retry, none after the final failure"

    def test_does_not_retry_fatal_errors(self):
        slept: list[float] = []
        attempts = {"n": 0}

        def op():
            attempts["n"] += 1
            raise AuthenticationError("bad password")

        with pytest.raises(AuthenticationError):
            retry_with_backoff(
                op, max_retries=5, base_seconds=1, max_seconds=10, sleep=slept.append
            )
        assert attempts["n"] == 1, "retrying a wrong password just burns attempts"
        assert slept == []

    def test_server_retry_after_wins_over_the_computed_delay(self):
        slept: list[float] = []
        attempts = {"n": 0}

        def op():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RateLimitedError("slow down", retry_after=120.0)
            return "ok"

        retry_with_backoff(
            op, max_retries=3, base_seconds=1, max_seconds=10, sleep=slept.append
        )
        # base*2^0 = 1s, but the server asked for 120s; jitter is ±25%.
        assert slept[0] >= 90.0

    def test_delay_is_capped(self):
        slept: list[float] = []

        def op():
            raise TransientScraperError("down")

        with pytest.raises(TransientScraperError):
            retry_with_backoff(
                op, max_retries=8, base_seconds=10, max_seconds=30, sleep=slept.append
            )
        assert max(slept) <= 30 * 1.25 + 0.01


# ---------------------------------------------------------------------------
# Demo backend
# ---------------------------------------------------------------------------
class TestDemoScraper:
    def test_returns_followers_without_network(self, settings):
        with DemoScraper(settings) as scraper:
            result = scraper.fetch_followers("campus_confessions")
        assert result.complete is True
        assert len(result.records) > 50
        assert all(r.username for r in result.records)

    def test_records_are_unique(self, settings):
        with DemoScraper(settings) as scraper:
            result = scraper.fetch_followers("campus_confessions")
        usernames = [r.username for r in result.records]
        assert len(usernames) == len(set(usernames))

    def test_two_profiles_have_different_audiences(self, settings):
        with DemoScraper(settings) as scraper:
            one = scraper.fetch_followers("page_one").usernames
            two = scraper.fetch_followers("page_two").usernames
        assert one != two

    def test_successive_runs_drift(self, settings):
        """Consecutive snapshots must differ, or there is nothing to diff."""
        with DemoScraper(settings) as scraper:
            first = scraper.fetch_followers("campus_confessions").usernames
            second = scraper.fetch_followers("campus_confessions").usernames
        assert first != second

    def test_profile_metadata_is_populated(self, settings):
        with DemoScraper(settings) as scraper:
            profile = scraper.fetch_profile("campus_confessions")
        assert profile.username == "campus_confessions"
        assert profile.user_id
        assert profile.follower_count and profile.follower_count > 0

    def test_run_cap_truncates_and_flags_incomplete(self, settings):
        from dataclasses import replace

        capped = replace(settings, max_followers_per_run=10)
        with DemoScraper(capped) as scraper:
            result = scraper.fetch_followers("campus_confessions")
        assert len(result.records) == 10
        assert result.complete is False, "a capped run must not look complete"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
class TestBuildScraper:
    def test_builds_the_configured_backend(self, settings):
        assert isinstance(build_scraper(settings), DemoScraper)

    def test_explicit_override_wins(self, settings):
        assert isinstance(build_scraper(settings, backend="demo"), DemoScraper)

    def test_unknown_backend_is_rejected(self, settings):
        with pytest.raises(ScraperError, match="Unknown scraper backend"):
            build_scraper(settings, backend="carrier-pigeon")
