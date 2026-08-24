"""
analytics.py — every read query the dashboard needs, as plain functions.

Kept separate from `app.py` so the queries are testable without spinning up
Streamlit, and reusable from the CLI's `export` command. Each function returns
a pandas DataFrame (or a small dataclass) ready to render — the UI layer does
presentation only, never aggregation.

All filtering happens in SQL, not in pandas. Loading 50k follower rows into a
DataFrame just to drop 49k of them wastes the indexes defined in `models.py`.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import pandas as pd
from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from models import EventType, Follower, FollowerEvent, Snapshot, SnapshotStatus
from timeutils import ensure_utc, humanise_delta, to_ist, utcnow

log = logging.getLogger(__name__)

#: The sort modes offered by the dashboard's "Chronological Sort" dropdown.
SortMode = Literal[
    "Latest Added",
    "Oldest Tracked",
    "Recently Unfollowed",
    "Longest Retained",
    "Username (A-Z)",
]

SORT_MODES: tuple[str, ...] = (
    "Latest Added",
    "Oldest Tracked",
    "Recently Unfollowed",
    "Longest Retained",
    "Username (A-Z)",
)

StatusFilter = Literal["All", "Currently Following", "New", "Retained", "Unfollowed"]

STATUS_FILTERS: tuple[str, ...] = (
    "All",
    "Currently Following",
    "New",
    "Retained",
    "Unfollowed",
)


# ---------------------------------------------------------------------------
# Metric cards
# ---------------------------------------------------------------------------
@dataclass
class DashboardMetrics:
    """The numbers behind the metric cards at the top of the dashboard."""

    total_followers: int = 0
    tracked_ever: int = 0
    gained_24h: int = 0
    lost_24h: int = 0
    gained_7d: int = 0
    lost_7d: int = 0
    peak_follow_hour_ist: int | None = None
    peak_follow_hour_count: int = 0
    last_snapshot_at: datetime | None = None
    last_snapshot_status: str | None = None
    snapshot_count: int = 0
    reported_follower_count: int | None = None

    @property
    def net_24h(self) -> int:
        return self.gained_24h - self.lost_24h

    @property
    def net_7d(self) -> int:
        return self.gained_7d - self.lost_7d

    @property
    def peak_follow_window(self) -> str:
        """Full label for the busiest follow hour, e.g. "21:00-22:00 IST".

        Used in prose (the Trends caption), where there is room for it. Metric
        cards use `peak_follow_hour_label` instead — five cards share one row,
        and this string overflows the column at that width.
        """
        if self.peak_follow_hour_ist is None:
            return "Not enough data"
        start = self.peak_follow_hour_ist
        return f"{start:02d}:00-{(start + 1) % 24:02d}:00 IST"

    @property
    def peak_follow_hour_label(self) -> str:
        """Compact form for a metric card, e.g. "21:00"."""
        if self.peak_follow_hour_ist is None:
            return "—"
        return f"{self.peak_follow_hour_ist:02d}:00"

    @property
    def last_snapshot_label(self) -> str:
        if self.last_snapshot_at is None:
            return "Never"
        return humanise_delta(self.last_snapshot_at)

    @property
    def capture_completeness(self) -> float | None:
        """Fraction of the profile's claimed followers we actually hold.

        A number well under 1.0 means pagination is being truncated — usually
        the run cap or a rate limit, not a real follower loss.
        """
        if not self.reported_follower_count:
            return None
        return self.total_followers / self.reported_follower_count

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _count_events(
    session: Session, target_id: int, event_type: EventType, since: datetime
) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(FollowerEvent)
            .where(
                FollowerEvent.target_id == target_id,
                FollowerEvent.event_type == event_type,
                FollowerEvent.timestamp >= since,
            )
        )
        or 0
    )


def get_dashboard_metrics(
    session: Session, target_id: int, *, now: datetime | None = None
) -> DashboardMetrics:
    """Everything the metric-card row needs, in a handful of indexed queries."""
    now = ensure_utc(now) or utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    metrics = DashboardMetrics()

    metrics.total_followers = (
        session.scalar(
            select(func.count())
            .select_from(Follower)
            .where(Follower.target_id == target_id, Follower.currently_following.is_(True))
        )
        or 0
    )
    metrics.tracked_ever = (
        session.scalar(
            select(func.count()).select_from(Follower).where(Follower.target_id == target_id)
        )
        or 0
    )

    metrics.gained_24h = _count_events(session, target_id, EventType.FOLLOW, day_ago)
    metrics.lost_24h = _count_events(session, target_id, EventType.UNFOLLOW, day_ago)
    metrics.gained_7d = _count_events(session, target_id, EventType.FOLLOW, week_ago)
    metrics.lost_7d = _count_events(session, target_id, EventType.UNFOLLOW, week_ago)

    latest = session.scalar(
        select(Snapshot)
        .where(Snapshot.target_id == target_id)
        .order_by(Snapshot.timestamp.desc())
        .limit(1)
    )
    if latest is not None:
        metrics.last_snapshot_at = latest.timestamp
        metrics.last_snapshot_status = (
            latest.status.value if isinstance(latest.status, SnapshotStatus) else str(latest.status)
        )
        metrics.reported_follower_count = latest.reported_followers

    metrics.snapshot_count = (
        session.scalar(
            select(func.count()).select_from(Snapshot).where(Snapshot.target_id == target_id)
        )
        or 0
    )

    # `peak_follow_hours` always returns all 24 rows, so "not empty" is not the
    # same as "has data" — without the sum check every fresh target would be
    # reported as peaking at midnight.
    peak = peak_follow_hours(session, target_id)
    if not peak.empty and peak["follows"].sum() > 0:
        top = peak.iloc[peak["follows"].argmax()]
        metrics.peak_follow_hour_ist = int(top["hour_ist"])
        metrics.peak_follow_hour_count = int(top["follows"])

    return metrics


# ---------------------------------------------------------------------------
# Follower table
# ---------------------------------------------------------------------------
def _apply_status_filter(stmt: Select, status: str) -> Select:
    if status == "Currently Following":
        return stmt.where(Follower.currently_following.is_(True))
    if status == "Unfollowed":
        return stmt.where(Follower.currently_following.is_(False))
    if status == "New":
        # Mirrors Follower.status: first cycle, first snapshot.
        return stmt.where(
            Follower.currently_following.is_(True),
            Follower.snapshots_present <= 1,
            Follower.times_followed <= 1,
        )
    if status == "Retained":
        return stmt.where(
            Follower.currently_following.is_(True),
            Follower.snapshots_present > 1,
        )
    return stmt


def _apply_sort(stmt: Select, sort_mode: str) -> Select:
    if sort_mode == "Latest Added":
        return stmt.order_by(Follower.first_detected_at.desc(), Follower.id.desc())
    if sort_mode == "Oldest Tracked":
        return stmt.order_by(Follower.first_detected_at.asc(), Follower.id.asc())
    if sort_mode == "Recently Unfollowed":
        # NULLs (still following) sort last regardless of dialect: SQLite puts
        # NULL first on DESC, Postgres puts it first too — so key on the flag.
        return stmt.order_by(
            case((Follower.unfollowed_at.is_(None), 1), else_=0).asc(),
            Follower.unfollowed_at.desc(),
        )
    if sort_mode == "Longest Retained":
        return stmt.order_by(Follower.snapshots_present.desc(), Follower.first_detected_at.asc())
    return stmt.order_by(func.lower(Follower.instagram_username).asc())


def query_followers(
    session: Session,
    target_id: int,
    *,
    search: str = "",
    sort_mode: str = "Latest Added",
    status: str = "All",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    date_field: Literal["first_detected_at", "unfollowed_at", "last_seen_at"] = "first_detected_at",
    limit: int | None = None,
) -> pd.DataFrame:
    """The main follower table, filtered and sorted in SQL.

    `date_from`/`date_to` bound `date_field`, so the same function serves both
    "who joined this week" and "who left this week".
    """
    stmt = select(Follower).where(Follower.target_id == target_id)

    if search:
        # Case-insensitive contains. Escape the LIKE wildcards so a user typing
        # "100%" searches for that literal instead of matching everything.
        needle = search.strip().lstrip("@").lower()
        needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{needle}%"
        stmt = stmt.where(
            func.lower(Follower.instagram_username).like(pattern, escape="\\")
            | func.lower(func.coalesce(Follower.full_name, "")).like(pattern, escape="\\")
        )

    stmt = _apply_status_filter(stmt, status)

    column = getattr(Follower, date_field)
    if date_from is not None:
        stmt = stmt.where(column >= ensure_utc(date_from))
    if date_to is not None:
        stmt = stmt.where(column <= ensure_utc(date_to))

    stmt = _apply_sort(stmt, sort_mode)
    if limit:
        stmt = stmt.limit(limit)

    rows = list(session.scalars(stmt))
    return _followers_to_frame(rows)


def _followers_to_frame(rows: list[Follower]) -> pd.DataFrame:
    columns = [
        "username",
        "full_name",
        "status",
        "first_detected_utc",
        "first_detected_ist",
        "first_detected_ago",
        "unfollowed_utc",
        "unfollowed_ist",
        "last_seen_utc",
        "snapshots_present",
        "times_followed",
        "is_private",
        "is_verified",
        "user_id",
        "profile_url",
        "profile_pic_url",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(
        [
            {
                "username": row.instagram_username,
                "full_name": row.full_name or "",
                "status": row.status,
                "first_detected_utc": ensure_utc(row.first_detected_at),
                "first_detected_ist": to_ist(row.first_detected_at),
                "first_detected_ago": humanise_delta(row.first_detected_at),
                "unfollowed_utc": ensure_utc(row.unfollowed_at),
                "unfollowed_ist": to_ist(row.unfollowed_at),
                "last_seen_utc": ensure_utc(row.last_seen_at),
                "snapshots_present": row.snapshots_present,
                "times_followed": row.times_followed,
                "is_private": row.is_private,
                "is_verified": row.is_verified,
                "user_id": row.user_id or "",
                "profile_url": row.profile_url,
                "profile_pic_url": row.profile_pic_url or "",
            }
            for row in rows
        ],
        columns=columns,
    )


# ---------------------------------------------------------------------------
# Event timeline
# ---------------------------------------------------------------------------
def query_events(
    session: Session,
    target_id: int,
    *,
    search: str = "",
    event_types: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int | None = 2000,
) -> pd.DataFrame:
    """The chronological FOLLOW/UNFOLLOW feed, newest first."""
    stmt = select(FollowerEvent).where(FollowerEvent.target_id == target_id)

    if search:
        needle = search.strip().lstrip("@").lower()
        needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(
            func.lower(FollowerEvent.instagram_username).like(f"%{needle}%", escape="\\")
        )
    if event_types:
        stmt = stmt.where(FollowerEvent.event_type.in_(event_types))
    if date_from is not None:
        stmt = stmt.where(FollowerEvent.timestamp >= ensure_utc(date_from))
    if date_to is not None:
        stmt = stmt.where(FollowerEvent.timestamp <= ensure_utc(date_to))

    stmt = stmt.order_by(FollowerEvent.timestamp.desc(), FollowerEvent.id.desc())
    if limit:
        stmt = stmt.limit(limit)

    rows = list(session.scalars(stmt))
    columns = ["event", "username", "timestamp_utc", "timestamp_ist", "when", "snapshot_id"]
    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(
        [
            {
                "event": (
                    row.event_type.value
                    if isinstance(row.event_type, EventType)
                    else str(row.event_type)
                ),
                "username": row.instagram_username,
                "timestamp_utc": ensure_utc(row.timestamp),
                "timestamp_ist": to_ist(row.timestamp),
                "when": humanise_delta(row.timestamp),
                "snapshot_id": row.snapshot_id,
            }
            for row in rows
        ],
        columns=columns,
    )


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------
def growth_timeseries(
    session: Session,
    target_id: int,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> pd.DataFrame:
    """Follower total per snapshot — the line chart behind "growth"."""
    stmt = select(Snapshot).where(Snapshot.target_id == target_id)
    if date_from is not None:
        stmt = stmt.where(Snapshot.timestamp >= ensure_utc(date_from))
    if date_to is not None:
        stmt = stmt.where(Snapshot.timestamp <= ensure_utc(date_to))
    rows = list(session.scalars(stmt.order_by(Snapshot.timestamp.asc())))

    columns = [
        "timestamp_utc",
        "timestamp_ist",
        "total_followers",
        "new_followers",
        "lost_followers",
        "net_change",
        "status",
        "captured_followers",
        "reported_followers",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(
        [
            {
                "timestamp_utc": ensure_utc(row.timestamp),
                "timestamp_ist": to_ist(row.timestamp),
                "total_followers": row.total_followers,
                "new_followers": row.new_followers,
                "lost_followers": row.lost_followers,
                "net_change": row.net_change,
                "status": (
                    row.status.value
                    if isinstance(row.status, SnapshotStatus)
                    else str(row.status)
                ),
                "captured_followers": row.captured_followers,
                "reported_followers": row.reported_followers,
            }
            for row in rows
        ],
        columns=columns,
    )


def daily_activity(
    session: Session,
    target_id: int,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Follows and unfollows bucketed by IST calendar day.

    Bucketing is done in Python rather than SQL because the IST offset (+05:30)
    is a half-hour one; `date_trunc`/`strftime` on the stored UTC value would
    silently bucket by UTC day and shift every evening event to the wrong date.
    """
    now = ensure_utc(now) or utcnow()
    since = now - timedelta(days=days)

    rows = session.execute(
        select(FollowerEvent.event_type, FollowerEvent.timestamp).where(
            FollowerEvent.target_id == target_id,
            FollowerEvent.timestamp >= since,
        )
    ).all()

    columns = ["date_ist", "follows", "unfollows", "net"]
    if not rows:
        return pd.DataFrame(columns=columns)

    frame = pd.DataFrame(
        [
            {
                "date_ist": to_ist(timestamp).date(),
                "event": value.value if isinstance(value, EventType) else str(value),
            }
            for value, timestamp in rows
        ]
    )
    pivot = (
        frame.pivot_table(index="date_ist", columns="event", aggfunc="size", fill_value=0)
        .reindex(columns=[EventType.FOLLOW.value, EventType.UNFOLLOW.value], fill_value=0)
        .rename(columns={EventType.FOLLOW.value: "follows", EventType.UNFOLLOW.value: "unfollows"})
        .reset_index()
    )
    pivot["net"] = pivot["follows"] - pivot["unfollows"]
    return pivot[columns].sort_values("date_ist")


def peak_follow_hours(
    session: Session,
    target_id: int,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> pd.DataFrame:
    """Follow counts per hour-of-day in IST — the "when do people follow" chart.

    Returns all 24 hours so the chart has no gaps, and so "no activity at 04:00"
    reads as a real zero rather than a missing bar.
    """
    stmt = select(FollowerEvent.timestamp).where(
        FollowerEvent.target_id == target_id,
        FollowerEvent.event_type == EventType.FOLLOW,
    )
    if date_from is not None:
        stmt = stmt.where(FollowerEvent.timestamp >= ensure_utc(date_from))
    if date_to is not None:
        stmt = stmt.where(FollowerEvent.timestamp <= ensure_utc(date_to))

    timestamps = list(session.scalars(stmt))
    counts = {hour: 0 for hour in range(24)}
    for timestamp in timestamps:
        local = to_ist(timestamp)
        if local is not None:
            counts[local.hour] += 1

    return pd.DataFrame(
        {
            "hour_ist": list(counts),
            "label": [f"{hour:02d}:00" for hour in counts],
            "follows": list(counts.values()),
        }
    )


def snapshot_history(session: Session, target_id: int, *, limit: int = 100) -> pd.DataFrame:
    """Recent scrape runs, for the "is the scraper healthy?" tab."""
    rows = list(
        session.scalars(
            select(Snapshot)
            .where(Snapshot.target_id == target_id)
            .order_by(Snapshot.timestamp.desc())
            .limit(limit)
        )
    )
    columns = [
        "snapshot_id",
        "timestamp_utc",
        "timestamp_ist",
        "status",
        "total_followers",
        "captured_followers",
        "reported_followers",
        "new_followers",
        "lost_followers",
        "duration_seconds",
        "error_message",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(
        [
            {
                "snapshot_id": row.id,
                "timestamp_utc": ensure_utc(row.timestamp),
                "timestamp_ist": to_ist(row.timestamp),
                "status": (
                    row.status.value
                    if isinstance(row.status, SnapshotStatus)
                    else str(row.status)
                ),
                "total_followers": row.total_followers,
                "captured_followers": row.captured_followers,
                "reported_followers": row.reported_followers,
                "new_followers": row.new_followers,
                "lost_followers": row.lost_followers,
                "duration_seconds": round(row.duration_seconds or 0.0, 2),
                "error_message": row.error_message or "",
            }
            for row in rows
        ],
        columns=columns,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _isoformat_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Render datetime columns as ISO-8601 strings with their offset intact.

    pandas would otherwise write a naive-looking local string into the CSV,
    which is the fastest way to lose the UTC/IST distinction the whole tool
    exists to preserve.
    """
    out = frame.copy()
    for column in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[column]):
            out[column] = out[column].apply(lambda v: "" if pd.isna(v) else v.isoformat())
        elif out[column].dtype == object:
            out[column] = out[column].apply(
                lambda v: v.isoformat() if isinstance(v, datetime) else v
            )
    return out


def to_csv_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    _isoformat_frame(frame).to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def to_json_bytes(frame: pd.DataFrame, *, meta: dict[str, Any] | None = None) -> bytes:
    """JSON export with a metadata envelope.

    The envelope records when and for whom the export was produced — an
    exported follower log is only interpretable alongside its capture time.
    """
    payload: dict[str, Any] = {
        "exported_at_utc": utcnow().isoformat(),
        "exported_at_ist": to_ist(utcnow()).isoformat(),
        "row_count": int(len(frame)),
        "records": json.loads(_isoformat_frame(frame).to_json(orient="records")),
    }
    if meta:
        payload = {**meta, **payload}
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def export_filename(target_username: str, kind: str, extension: str) -> str:
    stamp = to_ist(utcnow()).strftime("%Y%m%d_%H%M")
    return f"{target_username}_{kind}_{stamp}IST.{extension}"


__all__ = [
    "DashboardMetrics",
    "SORT_MODES",
    "STATUS_FILTERS",
    "daily_activity",
    "export_filename",
    "get_dashboard_metrics",
    "growth_timeseries",
    "peak_follow_hours",
    "query_events",
    "query_followers",
    "snapshot_history",
    "to_csv_bytes",
    "to_json_bytes",
]
