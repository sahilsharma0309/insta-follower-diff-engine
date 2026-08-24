"""Tests for the diff engine — the part that must never be wrong."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from models import EventType, Follower, FollowerEvent, Snapshot, SnapshotStatus
from timeutils import utcnow
from tracker import apply_snapshot, compute_diff, normalise_username, run_snapshot


# ---------------------------------------------------------------------------
# The pure function
# ---------------------------------------------------------------------------
class TestComputeDiff:
    def test_splits_into_new_lost_and_retained(self):
        diff = compute_diff({"a", "b", "c"}, {"b", "c", "d"})
        assert diff.new == {"d"}
        assert diff.lost == {"a"}
        assert diff.retained == {"b", "c"}
        assert diff.net_change == 0

    def test_first_ever_run_is_all_new(self):
        diff = compute_diff(set(), {"a", "b"})
        assert diff.new == {"a", "b"}
        assert diff.lost == set()
        assert diff.net_change == 2

    def test_empty_current_is_all_lost(self):
        diff = compute_diff({"a", "b"}, set())
        assert diff.lost == {"a", "b"}
        assert diff.net_change == -2

    def test_no_change_is_falsy(self):
        assert not compute_diff({"a"}, {"a"})
        assert compute_diff({"a"}, {"b"})

    def test_both_empty(self):
        diff = compute_diff(set(), set())
        assert diff.new == diff.lost == diff.retained == frozenset()


def test_normalise_username_strips_at_and_case():
    assert normalise_username("  @Campus_Confessions ") == "campus_confessions"


# ---------------------------------------------------------------------------
# Applying a snapshot
# ---------------------------------------------------------------------------
class TestApplySnapshot:
    def test_first_snapshot_creates_followers_and_events(
        self, session, target, fetch_factory, settings
    ):
        outcome = apply_snapshot(
            session, target, fetch_factory(["alice", "bob"]), settings=settings
        )

        assert outcome.total_followers == 2
        assert outcome.new_followers == ["alice", "bob"]
        assert outcome.lost_followers == []
        assert outcome.status == SnapshotStatus.SUCCESS

        rows = session.scalars(select(Follower)).all()
        assert {r.instagram_username for r in rows} == {"alice", "bob"}
        assert all(r.currently_following for r in rows)
        assert all(r.status == "NEW" for r in rows)

        events = session.scalars(select(FollowerEvent)).all()
        assert len(events) == 2
        assert all(e.event_type == EventType.FOLLOW for e in events)

    def test_second_snapshot_detects_join_and_leave(
        self, session, target, fetch_factory, settings
    ):
        apply_snapshot(session, target, fetch_factory(["alice", "bob"]), settings=settings)
        outcome = apply_snapshot(
            session, target, fetch_factory(["bob", "carol"]), settings=settings
        )

        assert outcome.new_followers == ["carol"]
        assert outcome.lost_followers == ["alice"]
        assert outcome.retained_count == 1
        assert outcome.total_followers == 2
        assert outcome.net_change == 0

        alice = session.scalar(
            select(Follower).where(Follower.instagram_username == "alice")
        )
        assert alice.currently_following is False
        assert alice.unfollowed_at is not None
        assert alice.status == "UNFOLLOWED"

        bob = session.scalar(select(Follower).where(Follower.instagram_username == "bob"))
        assert bob.status == "RETAINED"
        assert bob.snapshots_present == 2

    def test_unfollow_event_is_logged(self, session, target, fetch_factory, settings):
        apply_snapshot(session, target, fetch_factory(["alice"]), settings=settings)
        apply_snapshot(session, target, fetch_factory([]), settings=settings)

        events = session.scalars(
            select(FollowerEvent).order_by(FollowerEvent.id)
        ).all()
        assert [e.event_type for e in events] == [EventType.FOLLOW, EventType.UNFOLLOW]
        assert events[-1].instagram_username == "alice"

    def test_returning_follower_reuses_row_and_keeps_original_first_detected(
        self, session, target, fetch_factory, settings
    ):
        first = utcnow() - timedelta(days=5)
        apply_snapshot(
            session, target, fetch_factory(["alice"]), settings=settings, observed_at=first
        )
        apply_snapshot(session, target, fetch_factory([]), settings=settings)
        apply_snapshot(session, target, fetch_factory(["alice"]), settings=settings)

        rows = session.scalars(
            select(Follower).where(Follower.instagram_username == "alice")
        ).all()
        assert len(rows) == 1, "a returning follower must not create a duplicate row"

        alice = rows[0]
        assert alice.currently_following is True
        assert alice.unfollowed_at is None
        assert alice.times_followed == 2
        # The original discovery time is preserved, not reset by the return.
        assert abs((alice.first_detected_at - first).total_seconds()) < 1
        # Second cycle, so it reads as RETAINED rather than a brand-new find.
        assert alice.status == "RETAINED"

        events = session.scalars(
            select(FollowerEvent).order_by(FollowerEvent.id)
        ).all()
        assert [e.event_type for e in events] == [
            EventType.FOLLOW,
            EventType.UNFOLLOW,
            EventType.FOLLOW,
        ]

    def test_snapshot_row_records_the_counts(self, session, target, fetch_factory, settings):
        apply_snapshot(session, target, fetch_factory(["a", "b", "c"]), settings=settings)
        apply_snapshot(session, target, fetch_factory(["b", "c", "d"]), settings=settings)

        latest = session.scalars(
            select(Snapshot).order_by(Snapshot.id.desc())
        ).first()
        assert latest.total_followers == 3
        assert latest.new_followers == 1
        assert latest.lost_followers == 1
        assert latest.retained_followers == 2
        assert latest.net_change == 0

    def test_metadata_is_refreshed_without_clobbering(self, session, target, settings):
        from scraper import FetchResult, FollowerRecord

        rich = FetchResult(
            records=[
                FollowerRecord(
                    username="alice",
                    user_id="123",
                    full_name="Alice A",
                    profile_pic_url="https://example.test/a.jpg",
                    is_verified=True,
                )
            ]
        )
        apply_snapshot(session, target, rich, settings=settings)

        # A later run returns the same account with no metadata attached.
        bare = FetchResult(records=[FollowerRecord(username="alice")])
        apply_snapshot(session, target, bare, settings=settings)

        alice = session.scalar(
            select(Follower).where(Follower.instagram_username == "alice")
        )
        assert alice.user_id == "123", "a null must not overwrite a known id"
        assert alice.full_name == "Alice A"
        assert alice.profile_pic_url == "https://example.test/a.jpg"


# ---------------------------------------------------------------------------
# The safety guards — the reason this is not just set arithmetic
# ---------------------------------------------------------------------------
class TestUnfollowGuards:
    def test_incomplete_fetch_never_logs_unfollows(
        self, session, target, fetch_factory, settings
    ):
        apply_snapshot(session, target, fetch_factory(["a", "b", "c"]), settings=settings)

        # A truncated run returns only one of the three known followers.
        outcome = apply_snapshot(
            session, target, fetch_factory(["a"], complete=False), settings=settings
        )

        assert outcome.lost_followers == []
        assert outcome.skipped_unfollows is True
        assert "incomplete" in outcome.skip_reason
        assert outcome.status == SnapshotStatus.PARTIAL

        still_following = session.scalars(
            select(Follower).where(Follower.currently_following.is_(True))
        ).all()
        assert len(still_following) == 3, "a short read must not evict known followers"

        assert (
            session.scalars(
                select(FollowerEvent).where(
                    FollowerEvent.event_type == EventType.UNFOLLOW
                )
            ).all()
            == []
        )

    def test_implausible_collapse_is_rejected(self, session, target, fetch_factory, settings):
        apply_snapshot(
            session, target, fetch_factory([f"user{i}" for i in range(100)]), settings=settings
        )
        # A "complete" run that lost 90% of the set — almost certainly a short
        # read from Instagram rather than a real exodus.
        outcome = apply_snapshot(
            session, target, fetch_factory([f"user{i}" for i in range(10)]), settings=settings
        )

        assert outcome.lost_followers == []
        assert outcome.skipped_unfollows is True
        assert "plausibility" in outcome.skip_reason
        assert outcome.status == SnapshotStatus.PARTIAL

    def test_normal_churn_is_below_the_threshold(
        self, session, target, fetch_factory, settings
    ):
        apply_snapshot(
            session, target, fetch_factory([f"user{i}" for i in range(100)]), settings=settings
        )
        # Losing 10 of 100 is ordinary churn and must be recorded.
        outcome = apply_snapshot(
            session, target, fetch_factory([f"user{i}" for i in range(10, 100)]), settings=settings
        )
        assert len(outcome.lost_followers) == 10
        assert outcome.skipped_unfollows is False
        assert outcome.status == SnapshotStatus.SUCCESS

    def test_new_follows_still_recorded_on_a_partial_run(
        self, session, target, fetch_factory, settings
    ):
        apply_snapshot(session, target, fetch_factory(["a", "b"]), settings=settings)
        outcome = apply_snapshot(
            session, target, fetch_factory(["a", "z"], complete=False), settings=settings
        )
        # Follows are safe to trust on a partial read: seeing an account is
        # positive evidence, whereas not seeing one is not.
        assert outcome.new_followers == ["z"]
        assert outcome.lost_followers == []

    def test_threshold_is_configurable(self, session, target, fetch_factory, settings):
        from dataclasses import replace

        strict = replace(settings, max_unfollow_ratio=0.05)
        apply_snapshot(
            session, target, fetch_factory([f"u{i}" for i in range(100)]), settings=strict
        )
        outcome = apply_snapshot(
            session, target, fetch_factory([f"u{i}" for i in range(10, 100)]), settings=strict
        )
        assert outcome.skipped_unfollows is True


# ---------------------------------------------------------------------------
# End-to-end against the demo backend
# ---------------------------------------------------------------------------
def test_run_snapshot_end_to_end_with_demo_backend(session, settings):
    from scraper import build_scraper

    with build_scraper(settings) as scraper:
        outcome = run_snapshot(session, scraper, "campus_confessions", settings=settings)

    assert outcome.status == SnapshotStatus.SUCCESS
    assert outcome.total_followers > 0
    assert len(outcome.new_followers) == outcome.total_followers

    stored = session.scalars(select(Follower)).all()
    assert len(stored) == outcome.total_followers
    assert all(row.first_detected_at.tzinfo is not None for row in stored)


def test_fatal_scrape_error_records_a_failed_snapshot(session, settings):
    """An expired session is fatal: the run is FAILED, not silently partial."""
    from scraper import AuthenticationError, DemoScraper

    class Broken(DemoScraper):
        def iter_followers(self, username):
            raise AuthenticationError("session expired")
            yield  # pragma: no cover - unreachable; keeps this a generator

    with Broken(settings) as scraper:
        outcome = run_snapshot(session, scraper, "demo_page", settings=settings)

    assert outcome.status == SnapshotStatus.FAILED
    assert "session expired" in (outcome.error or "")
    snapshot = session.scalars(select(Snapshot).order_by(Snapshot.id.desc())).first()
    assert snapshot.status == SnapshotStatus.FAILED
    assert "session expired" in snapshot.error_message


def test_failed_run_carries_the_last_known_total_forward(
    session, target, fetch_factory, settings
):
    """A failed run must not draw a cliff to zero in the growth chart."""
    from scraper import AuthenticationError, DemoScraper

    apply_snapshot(session, target, fetch_factory(["a", "b", "c"]), settings=settings)

    class Broken(DemoScraper):
        def iter_followers(self, username):
            raise AuthenticationError("session expired")
            yield  # pragma: no cover

    with Broken(settings) as scraper:
        outcome = run_snapshot(session, scraper, "demo_page", settings=settings)

    assert outcome.total_followers == 3
    snapshot = session.scalars(select(Snapshot).order_by(Snapshot.id.desc())).first()
    assert snapshot.total_followers == 3


def test_mid_stream_failure_keeps_partial_results(session, settings):
    """A generic error part-way through keeps what was collected, marked partial."""
    from scraper import DemoScraper, ScraperError

    class HalfBroken(DemoScraper):
        def iter_followers(self, username):
            yield from [
                record
                for _, record in zip(range(5), super(HalfBroken, self).iter_followers(username))
            ]
            raise ScraperError("connection reset mid-pagination")

    with HalfBroken(settings) as scraper:
        result = scraper.fetch_followers("demo_page")

    assert len(result.records) == 5
    assert result.complete is False
    assert "connection reset" in (result.error or "")
