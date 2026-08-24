"""
models.py — SQLAlchemy 2.0 ORM schema plus the session/engine factory.

Schema overview
---------------

    targets          one row per monitored profile
      └── followers        one row per (target, follower) pair, ever seen
            └── follower_events   append-only FOLLOW / UNFOLLOW audit log
      └── snapshots        one row per scrape run

`followers` is deliberately a *materialised current state* table rather than a
per-snapshot membership table:

* `currently_following` holds the live set, so the diff in `tracker.py` is a
  single indexed query instead of a scan over every historical snapshot.
* `first_detected_at` / `unfollowed_at` give the chronological timeline the
  dashboard needs without any aggregation.
* `follower_events` remains the append-only source of truth, so a follower who
  leaves and returns keeps a complete, ordered history (`times_followed`
  counts the cycles).

The alternative — storing full membership for every snapshot — costs
O(followers × snapshots) rows to answer questions this schema answers in O(1).

Everything is dialect-agnostic: the same models run on SQLite (default) and
PostgreSQL (set `IG_DATABASE_URL`).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from timeutils import ensure_utc, utcnow


# ---------------------------------------------------------------------------
# A timezone-correct DateTime that behaves identically on SQLite and Postgres
# ---------------------------------------------------------------------------
class UTCDateTime(TypeDecorator):
    """Store aware UTC datetimes; read them back aware.

    SQLite has no native timestamp type — SQLAlchemy serialises datetimes to
    ISO strings and hands back *naive* objects, silently discarding the offset.
    Comparing a naive datetime to an aware one raises TypeError, so without
    this decorator every read path would need defensive coercion. Doing it once
    here means the rest of the codebase can assume "datetimes are aware UTC".
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value)


class Base(DeclarativeBase):
    """Declarative base for every table in the tracker."""


class EventType(str, enum.Enum):
    """The two state transitions we record.

    Inherits from `str` so the value round-trips through pandas, JSON exports
    and Streamlit widgets without an explicit `.value` at every call site.
    RETAINED is intentionally *not* an event: retention is the absence of a
    transition, and writing a row per follower per snapshot for it would
    balloon the audit log for zero information gain. It is exposed as a
    derived status on `Follower.status` instead.
    """

    FOLLOW = "FOLLOW"
    UNFOLLOW = "UNFOLLOW"


class SnapshotStatus(str, enum.Enum):
    """Outcome of a scrape run.

    PARTIAL matters: a snapshot that was truncated (hit a rate limit, the run
    cap, or a mid-stream error) must never be diffed for unfollows, or every
    follower we simply did not reach would be logged as having left.
    """

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
class Target(Base):
    """A public profile being monitored."""

    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    instagram_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    biography: Mapped[str | None] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Follower count as reported by the profile header — useful as a
    #: cross-check against how many rows we actually managed to page through.
    reported_follower_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    last_scraped_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    followers: Mapped[list["Follower"]] = relationship(
        back_populates="target", cascade="all, delete-orphan", passive_deletes=True
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="target", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list["FollowerEvent"]] = relationship(
        back_populates="target", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Target {self.username!r} id={self.id}>"


class Follower(Base):
    """One account that has followed a target at least once.

    Rows are never deleted. An account that unfollows keeps its row with
    `currently_following=False` and a populated `unfollowed_at`, which is what
    makes the "Recently Unfollowed" view possible at all.
    """

    __tablename__ = "followers"
    __table_args__ = (
        # The natural key. Also the index the diff engine reads on every run.
        UniqueConstraint("target_id", "instagram_username", name="uq_follower_per_target"),
        Index("ix_followers_target_current", "target_id", "currently_following"),
        Index("ix_followers_target_first_seen", "target_id", "first_detected_at"),
        Index("ix_followers_target_unfollowed", "target_id", "unfollowed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    instagram_username: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    profile_pic_url: Mapped[str | None] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Chronology -------------------------------------------------------
    #: First moment this account was ever observed following the target.
    first_detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    #: Most recent snapshot in which the account was present.
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    #: Set when the account disappears; cleared if it comes back.
    unfollowed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: The persistence flag: is this account in the target's current follower set?
    currently_following: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    #: How many distinct follow cycles we have seen (2+ means they came back).
    times_followed: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Number of snapshots in which this follower was present — the "RETAINED"
    #: persistence signal surfaced in the dashboard.
    snapshots_present: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    target: Mapped["Target"] = relationship(back_populates="followers")
    events: Mapped[list["FollowerEvent"]] = relationship(
        back_populates="follower", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def status(self) -> str:
        """Derived label used throughout the dashboard.

        NEW is scoped to the follower's *first* cycle so a returning account
        reads as RETAINED rather than being presented as a brand-new find.
        """
        if not self.currently_following:
            return "UNFOLLOWED"
        if self.snapshots_present <= 1 and self.times_followed <= 1:
            return "NEW"
        return "RETAINED"

    @property
    def profile_url(self) -> str:
        return f"https://www.instagram.com/{self.instagram_username}/"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Follower {self.instagram_username!r} target={self.target_id} {self.status}>"


class FollowerEvent(Base):
    """Append-only log of follow / unfollow transitions.

    Nothing in this table is ever updated or deleted. Every chart in the
    dashboard — growth over time, peak follow hours, the chronological
    timeline — is derived from it.
    """

    __tablename__ = "follower_events"
    __table_args__ = (
        Index("ix_events_target_time", "target_id", "timestamp"),
        Index("ix_events_target_type_time", "target_id", "event_type", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    follower_id: Mapped[int] = mapped_column(
        ForeignKey("followers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), index=True
    )

    # Stored as a plain VARCHAR rather than a native ENUM: Postgres native
    # enums need a migration to add a value, and SQLite has none at all.
    event_type: Mapped[EventType] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )
    #: Denormalised for fast exports — avoids a join just to render a username.
    instagram_username: Mapped[str] = mapped_column(String(150), nullable=False, index=True)

    follower: Mapped["Follower"] = relationship(back_populates="events")
    target: Mapped["Target"] = relationship(back_populates="events")
    snapshot: Mapped["Snapshot | None"] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.event_type} {self.instagram_username!r} at {self.timestamp:%Y-%m-%d %H:%M}>"


class Snapshot(Base):
    """One scrape run against one target."""

    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snapshots_target_time", "target_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Size of the follower set after this run — the series behind the graph.
    total_followers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: How many records the scrape actually returned (< total when truncated).
    captured_followers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: What the profile header claimed, for cross-checking completeness.
    reported_followers: Mapped[int | None] = mapped_column(Integer)

    new_followers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lost_followers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retained_followers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[SnapshotStatus] = mapped_column(
        String(16), default=SnapshotStatus.SUCCESS, nullable=False
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    error_message: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )

    target: Mapped["Target"] = relationship(back_populates="snapshots")
    events: Mapped[list["FollowerEvent"]] = relationship(back_populates="snapshot")

    @property
    def net_change(self) -> int:
        return self.new_followers - self.lost_followers

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Snapshot target={self.target_id} total={self.total_followers} "
            f"net={self.net_change:+d} {self.status}>"
        )


# ---------------------------------------------------------------------------
# Engine / session plumbing
# ---------------------------------------------------------------------------
def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


@event.listens_for(Engine, "connect")
def _tune_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
    """Enable the SQLite pragmas this workload needs.

    * ``foreign_keys`` is OFF by default in SQLite, which would silently make
      every ``ondelete="CASCADE"`` above a no-op.
    * ``journal_mode=WAL`` lets the Streamlit dashboard read while the
      scheduler writes, instead of the two deadlocking on a table lock.
    """
    # Applies to SQLite connections only; other drivers reach here too.
    if type(dbapi_connection).__module__.split(".")[0] not in {"sqlite3", "pysqlite3"}:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
    finally:
        cursor.close()


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Build an Engine with dialect-appropriate options."""
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if _is_sqlite(database_url):
        # Streamlit renders on a worker thread that differs from the one that
        # opened the connection; SQLite's default check would reject that.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        # Long-lived scheduler processes otherwise accumulate stale sockets.
        kwargs["pool_pre_ping"] = True
    return create_engine(database_url, **kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """Create any missing tables. Safe to call repeatedly."""
    if _is_sqlite(engine.url.render_as_string(hide_password=False)):
        # `sqlite:///runtime/tracker.db` fails if `runtime/` does not exist.
        db_path = engine.url.database
        if db_path and db_path != ":memory:":
            from pathlib import Path

            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


__all__ = [
    "Base",
    "EventType",
    "Follower",
    "FollowerEvent",
    "Snapshot",
    "SnapshotStatus",
    "Target",
    "UTCDateTime",
    "create_db_engine",
    "create_session_factory",
    "init_db",
]
