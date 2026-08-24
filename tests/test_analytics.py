"""Tests for the dashboard query layer."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from analytics import (
    daily_activity,
    export_filename,
    get_dashboard_metrics,
    growth_timeseries,
    peak_follow_hours,
    query_events,
    query_followers,
    snapshot_history,
    to_csv_bytes,
    to_json_bytes,
)
from timeutils import utcnow
from tracker import apply_snapshot


@pytest.fixture
def populated(session, target, fetch_factory, settings):
    """Three snapshots two hours apart: joins, a departure and a return."""
    base = utcnow() - timedelta(hours=6)
    apply_snapshot(
        session, target, fetch_factory(["alice", "bob", "carol"]),
        settings=settings, observed_at=base,
    )
    apply_snapshot(
        session, target, fetch_factory(["bob", "carol", "dave"]),
        settings=settings, observed_at=base + timedelta(hours=2),
    )
    apply_snapshot(
        session, target, fetch_factory(["bob", "dave", "erin", "alice"]),
        settings=settings, observed_at=base + timedelta(hours=4),
    )
    session.flush()
    return target


class TestDashboardMetrics:
    def test_headline_counts(self, session, populated):
        metrics = get_dashboard_metrics(session, populated.id)
        assert metrics.total_followers == 4      # bob, dave, erin, alice
        assert metrics.tracked_ever == 5         # + carol, who left
        assert metrics.snapshot_count == 3

    def test_24h_window(self, session, populated):
        metrics = get_dashboard_metrics(session, populated.id)
        # 3 + 1 + 2 follows, 1 + 1 unfollows, all inside the last 6 hours.
        assert metrics.gained_24h == 6
        assert metrics.lost_24h == 2
        assert metrics.net_24h == 4

    def test_peak_follow_window_is_reported(self, session, populated):
        metrics = get_dashboard_metrics(session, populated.id)
        assert metrics.peak_follow_hour_ist is not None
        assert 0 <= metrics.peak_follow_hour_ist <= 23
        assert "IST" in metrics.peak_follow_window

    def test_empty_target_is_all_zeroes(self, session, target):
        metrics = get_dashboard_metrics(session, target.id)
        assert metrics.total_followers == 0
        assert metrics.net_24h == 0
        assert metrics.peak_follow_window == "Not enough data"
        assert metrics.last_snapshot_label == "Never"

    def test_capture_completeness(self, session, target, fetch_factory, settings):
        apply_snapshot(
            session, target, fetch_factory(["a", "b"], reported=10), settings=settings
        )
        metrics = get_dashboard_metrics(session, target.id)
        assert metrics.capture_completeness == pytest.approx(0.2)


class TestQueryFollowers:
    def test_returns_all_by_default(self, session, populated):
        frame = query_followers(session, populated.id)
        assert len(frame) == 5

    def test_search_matches_substring(self, session, populated):
        assert len(query_followers(session, populated.id, search="ali")) == 1
        assert len(query_followers(session, populated.id, search="ALI")) == 1
        assert len(query_followers(session, populated.id, search="@alice")) == 1
        assert len(query_followers(session, populated.id, search="nobody")) == 0

    def test_search_escapes_like_wildcards(self, session, populated):
        # "%" must be searched literally, not treated as "match everything".
        assert len(query_followers(session, populated.id, search="%")) == 0

    def test_status_filters(self, session, populated):
        assert len(query_followers(session, populated.id, status="Currently Following")) == 4
        assert len(query_followers(session, populated.id, status="Unfollowed")) == 1
        unfollowed = query_followers(session, populated.id, status="Unfollowed")
        assert unfollowed.iloc[0]["username"] == "carol"

    def test_sort_latest_added_first(self, session, populated):
        frame = query_followers(session, populated.id, sort_mode="Latest Added")
        assert frame.iloc[0]["first_detected_utc"] >= frame.iloc[-1]["first_detected_utc"]

    def test_sort_oldest_tracked_first(self, session, populated):
        frame = query_followers(session, populated.id, sort_mode="Oldest Tracked")
        assert frame.iloc[0]["first_detected_utc"] <= frame.iloc[-1]["first_detected_utc"]

    def test_recently_unfollowed_puts_leavers_first(self, session, populated):
        frame = query_followers(session, populated.id, sort_mode="Recently Unfollowed")
        assert frame.iloc[0]["username"] == "carol"
        # Accounts that never left must sort last, not first on a NULL.
        assert frame.iloc[-1]["unfollowed_utc"] is None or frame.iloc[-1]["status"] != "UNFOLLOWED"

    def test_date_range_filters_on_first_detected(self, session, populated):
        cutoff = utcnow() - timedelta(hours=3)
        recent = query_followers(session, populated.id, date_from=cutoff)
        assert set(recent["username"]) == {"erin"}

    def test_returning_follower_keeps_original_detection_date(self, session, populated):
        frame = query_followers(session, populated.id, search="alice")
        row = frame.iloc[0]
        assert row["times_followed"] == 2
        assert row["status"] == "RETAINED"

    def test_columns_exist_even_when_empty(self, session, target):
        frame = query_followers(session, target.id)
        assert frame.empty
        assert "first_detected_ist" in frame.columns


class TestQueryEvents:
    def test_newest_first(self, session, populated):
        frame = query_events(session, populated.id)
        assert len(frame) == 8  # 6 follows + 2 unfollows
        assert frame.iloc[0]["timestamp_utc"] >= frame.iloc[-1]["timestamp_utc"]

    def test_filter_by_event_type(self, session, populated):
        assert len(query_events(session, populated.id, event_types=["UNFOLLOW"])) == 2
        assert len(query_events(session, populated.id, event_types=["FOLLOW"])) == 6

    def test_both_timezones_present(self, session, populated):
        row = query_events(session, populated.id).iloc[0]
        assert row["timestamp_utc"].utcoffset().total_seconds() == 0
        assert row["timestamp_ist"].utcoffset() == timedelta(hours=5, minutes=30)


class TestTimeSeries:
    def test_growth_series_is_chronological(self, session, populated):
        frame = growth_timeseries(session, populated.id)
        assert list(frame["total_followers"]) == [3, 3, 4]
        assert frame["timestamp_utc"].is_monotonic_increasing

    def test_daily_activity_buckets(self, session, populated):
        frame = daily_activity(session, populated.id, days=7)
        assert frame["follows"].sum() == 6
        assert frame["unfollows"].sum() == 2
        assert (frame["net"] == frame["follows"] - frame["unfollows"]).all()

    def test_peak_hours_covers_all_24(self, session, populated):
        frame = peak_follow_hours(session, populated.id)
        assert len(frame) == 24
        assert list(frame["hour_ist"]) == list(range(24))
        assert frame["follows"].sum() == 6

    def test_snapshot_history(self, session, populated):
        frame = snapshot_history(session, populated.id)
        assert len(frame) == 3
        assert set(frame["status"]) == {"SUCCESS"}


class TestExports:
    def test_csv_round_trips(self, session, populated):
        frame = query_followers(session, populated.id)
        raw = to_csv_bytes(frame).decode("utf-8")
        assert "username" in raw.splitlines()[0]
        assert "alice" in raw

    def test_csv_keeps_the_utc_offset(self, session, populated):
        raw = to_csv_bytes(query_events(session, populated.id)).decode("utf-8")
        # Offsets must survive; without them UTC and IST columns are ambiguous.
        assert "+00:00" in raw
        assert "+05:30" in raw

    def test_json_has_a_metadata_envelope(self, session, populated):
        payload = json.loads(
            to_json_bytes(
                query_events(session, populated.id), meta={"target": "demo_page"}
            )
        )
        assert payload["target"] == "demo_page"
        assert payload["row_count"] == 8
        assert len(payload["records"]) == 8
        assert "exported_at_ist" in payload

    def test_export_filename_is_timestamped(self):
        name = export_filename("demo_page", "followers", "csv")
        assert name.startswith("demo_page_followers_")
        assert name.endswith("IST.csv")
