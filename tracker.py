"""
tracker.py — the diff engine: turn successive follower lists into events.

The algorithm is plain set arithmetic::

    new      = current - previous      -> FOLLOW   event, first_detected_at
    lost     = previous - current      -> UNFOLLOW event, unfollowed_at
    retained = current & previous      -> bump persistence counters

What makes this production-grade is not the arithmetic — it is refusing to
apply it when the input is untrustworthy. Two guards sit in front of the
unfollow branch:

1. **Incomplete fetch.** If the scrape was truncated (rate limited, hit the
   per-run cap, died mid-pagination), every follower we simply never reached
   looks identical to one who left. Truncated snapshots therefore process
   follows only.
2. **Implausible collapse.** Even a "complete" fetch can be quietly short —
   Instagram occasionally serves a half-empty list under load. If a run would
   remove more than `IG_MAX_UNFOLLOW_RATIO` of the known set, we log the
   snapshot as PARTIAL and skip the unfollows rather than corrupt the history.

Both guards fail in the same safe direction: a missed unfollow is corrected by
the next healthy run, whereas a false mass-unfollow permanently poisons the
event log, and `follower_events` is append-only by design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import Settings, get_settings
from models import (
    EventType,
    Follower,
    FollowerEvent,
    Snapshot,
    SnapshotStatus,
    Target,
)
from scraper import BaseScraper, FetchResult, FollowerRecord, ScraperError
from timeutils import utcnow

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure diff — no database, no I/O, trivially testable
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiffSets:
    """The three-way split between two follower sets."""

    new: frozenset[str]
    lost: frozenset[str]
    retained: frozenset[str]

    @property
    def net_change(self) -> int:
        return len(self.new) - len(self.lost)

    def __bool__(self) -> bool:
        """True when anything actually changed."""
        return bool(self.new or self.lost)


def compute_diff(previous: set[str] | frozenset[str], current: set[str] | frozenset[str]) -> DiffSets:
    """Set difference between the previous and current follower sets.

    Kept free of any database or network dependency so the core logic can be
    tested exhaustively in microseconds.
    """
    previous = frozenset(previous)
    current = frozenset(current)
    return DiffSets(
        new=current - previous,
        lost=previous - current,
        retained=current & previous,
    )


@dataclass
class SnapshotOutcome:
    """Everything a caller needs to report on one completed run."""

    target_username: str
    snapshot_id: int | None
    status: SnapshotStatus
    total_followers: int
    new_followers: list[str] = field(default_factory=list)
    lost_followers: list[str] = field(default_factory=list)
    retained_count: int = 0
    skipped_unfollows: bool = False
    skip_reason: str | None = None
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def net_change(self) -> int:
        return len(self.new_followers) - len(self.lost_followers)

    def summary(self) -> str:
        """One-line log/CLI summary."""
        parts = [
            f"@{self.target_username}: {self.total_followers} followers",
            f"+{len(self.new_followers)}",
            f"-{len(self.lost_followers)}",
            f"({self.status.value.lower()}, {self.duration_seconds:.1f}s)",
        ]
        if self.skipped_unfollows:
            parts.append(f"[unfollow detection skipped: {self.skip_reason}]")
        if self.error:
            parts.append(f"[error: {self.error}]")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Target helpers
# ---------------------------------------------------------------------------
def normalise_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def get_target(session: Session, username: str) -> Target | None:
    return session.scalar(
        select(Target).where(Target.username == normalise_username(username))
    )


def get_or_create_target(session: Session, username: str, **fields: object) -> Target:
    """Fetch the target row, creating it on first sight. Idempotent."""
    username = normalise_username(username)
    target = get_target(session, username)
    if target is None:
        target = Target(username=username, **fields)  # type: ignore[arg-type]
        session.add(target)
        session.flush()  # assign target.id for the FKs below
        log.info("Registered new target @%s (id=%s)", username, target.id)
    else:
        for key, value in fields.items():
            if value is not None:
                setattr(target, key, value)
    return target


def sync_targets_from_config(session: Session, settings: Settings | None = None) -> list[Target]:
    """Ensure every profile in `IG_TARGETS` exists as a row."""
    settings = settings or get_settings()
    return [get_or_create_target(session, t.username) for t in settings.targets]


def list_targets(session: Session, *, active_only: bool = False) -> list[Target]:
    stmt = select(Target).order_by(Target.username)
    if active_only:
        stmt = stmt.where(Target.is_active.is_(True))
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# The diff application
# ---------------------------------------------------------------------------
def _current_follower_rows(session: Session, target_id: int) -> dict[str, Follower]:
    """Every follower row for a target, keyed by username.

    All rows are loaded, not just the currently-following ones: a returning
    account must reuse its existing row so the FOLLOW/UNFOLLOW history and
    `first_detected_at` survive, instead of getting a duplicate that the
    unique constraint would reject anyway.
    """
    rows = session.scalars(select(Follower).where(Follower.target_id == target_id))
    return {row.instagram_username: row for row in rows}


def _apply_metadata(row: Follower, record: FollowerRecord) -> None:
    """Refresh mutable profile metadata, never overwriting good data with None."""
    if record.user_id:
        row.user_id = record.user_id
    if record.full_name is not None:
        row.full_name = record.full_name
    if record.profile_pic_url:
        row.profile_pic_url = record.profile_pic_url
    row.is_private = record.is_private
    row.is_verified = record.is_verified


def apply_snapshot(
    session: Session,
    target: Target,
    fetch: FetchResult,
    *,
    settings: Settings | None = None,
    observed_at: datetime | None = None,
) -> SnapshotOutcome:
    """Diff `fetch` against stored state, write events, persist the snapshot.

    This is the single write path for follower state. It is deliberately
    synchronous and transactional: the caller's `session_scope()` commits once,
    so a crash midway leaves no half-applied diff.
    """
    settings = settings or get_settings()
    now = observed_at or utcnow()

    existing = _current_follower_rows(session, target.id)
    previous_set = {name for name, row in existing.items() if row.currently_following}
    records = {record.username: record for record in fetch.records}
    current_set = set(records)

    diff = compute_diff(previous_set, current_set)

    # --- Guard the unfollow branch ---------------------------------------
    skip_unfollows = False
    skip_reason: str | None = None

    if not fetch.complete:
        skip_unfollows = True
        skip_reason = "scrape was incomplete"
    elif diff.lost and previous_set:
        loss_ratio = len(diff.lost) / len(previous_set)
        # Both conditions must hold. The ratio catches a mass disappearance;
        # the absolute floor stops the guard from firing on a page so small
        # that any single departure is a large percentage.
        if (
            loss_ratio > settings.max_unfollow_ratio
            and len(diff.lost) >= settings.min_unfollows_for_guard
        ):
            skip_unfollows = True
            skip_reason = (
                f"{loss_ratio:.0%} of the follower set ({len(diff.lost)} accounts) "
                f"vanished in one run, above the {settings.max_unfollow_ratio:.0%} "
                "plausibility threshold"
            )
            log.warning(
                "@%s: refusing to log %d unfollows — %s. Treating as a short read.",
                target.username,
                len(diff.lost),
                skip_reason,
            )

    snapshot = Snapshot(
        target_id=target.id,
        timestamp=now,
        captured_followers=len(records),
        reported_followers=fetch.reported_follower_count,
        duration_seconds=fetch.duration_seconds,
        error_message=fetch.error,
        status=(
            SnapshotStatus.PARTIAL
            if (skip_unfollows or not fetch.complete)
            else SnapshotStatus.SUCCESS
        ),
    )
    session.add(snapshot)
    session.flush()  # assign snapshot.id for the event FKs

    new_usernames: list[str] = []
    lost_usernames: list[str] = []

    # --- FOLLOW: brand-new accounts and returning ones --------------------
    for username in sorted(diff.new):
        record = records[username]
        row = existing.get(username)
        if row is None:
            row = Follower(
                target_id=target.id,
                instagram_username=username,
                first_detected_at=now,
                last_seen_at=now,
                currently_following=True,
                times_followed=1,
                snapshots_present=1,
            )
            _apply_metadata(row, record)
            session.add(row)
            session.flush()  # need row.id for the event below
            existing[username] = row
        else:
            # A returning follower: keep first_detected_at (their true first
            # appearance) and count this as a fresh follow cycle.
            row.currently_following = True
            row.unfollowed_at = None
            row.last_seen_at = now
            row.times_followed += 1
            row.snapshots_present += 1
            _apply_metadata(row, record)

        session.add(
            FollowerEvent(
                follower_id=row.id,
                target_id=target.id,
                snapshot_id=snapshot.id,
                event_type=EventType.FOLLOW,
                timestamp=now,
                instagram_username=username,
            )
        )
        new_usernames.append(username)

    # --- UNFOLLOW ---------------------------------------------------------
    if not skip_unfollows:
        for username in sorted(diff.lost):
            row = existing.get(username)
            if row is None:  # pragma: no cover - previous_set is built from existing
                continue
            row.currently_following = False
            row.unfollowed_at = now
            session.add(
                FollowerEvent(
                    follower_id=row.id,
                    target_id=target.id,
                    snapshot_id=snapshot.id,
                    event_type=EventType.UNFOLLOW,
                    timestamp=now,
                    instagram_username=username,
                )
            )
            lost_usernames.append(username)

    # --- RETAINED: persistence flags, no events ---------------------------
    for username in diff.retained:
        row = existing[username]
        row.last_seen_at = now
        row.snapshots_present += 1
        _apply_metadata(row, records[username])

    # --- Finalise ---------------------------------------------------------
    total_now = len(previous_set) + len(new_usernames) - len(lost_usernames)
    snapshot.total_followers = total_now
    snapshot.new_followers = len(new_usernames)
    snapshot.lost_followers = len(lost_usernames)
    snapshot.retained_followers = len(diff.retained)

    target.last_scraped_at = now
    if fetch.reported_follower_count is not None:
        target.reported_follower_count = fetch.reported_follower_count

    outcome = SnapshotOutcome(
        target_username=target.username,
        snapshot_id=snapshot.id,
        status=snapshot.status,
        total_followers=total_now,
        new_followers=new_usernames,
        lost_followers=lost_usernames,
        retained_count=len(diff.retained),
        skipped_unfollows=skip_unfollows,
        skip_reason=skip_reason,
        error=fetch.error,
        duration_seconds=fetch.duration_seconds,
    )
    log.info(outcome.summary())
    return outcome


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_snapshot(
    session: Session,
    scraper: BaseScraper,
    username: str,
    *,
    settings: Settings | None = None,
) -> SnapshotOutcome:
    """Scrape one target and apply the diff.

    A scrape failure is recorded as a FAILED snapshot rather than raised: the
    scheduler must survive one bad target, and a visible gap in the snapshot
    history is more useful than silence.
    """
    settings = settings or get_settings()
    username = normalise_username(username)
    target = get_or_create_target(session, username)

    # Refresh the profile header first — cheap, and it keeps full_name and the
    # reported follower count current even if pagination later fails.
    try:
        profile = scraper.fetch_profile(username)
        target.full_name = profile.full_name or target.full_name
        target.instagram_user_id = profile.user_id or target.instagram_user_id
        target.biography = profile.biography or target.biography
        target.is_private = profile.is_private
        target.is_verified = profile.is_verified
    except ScraperError as exc:
        log.warning("Could not refresh profile header for @%s: %s", username, exc)

    try:
        fetch = scraper.fetch_followers(username)
    except ScraperError as exc:
        log.error("Snapshot for @%s failed: %s", username, exc)
        # Carry the last known count forward so the time series shows a flat
        # segment during an outage rather than a spurious drop to zero.
        last_known = (
            session.scalar(
                select(func.count())
                .select_from(Follower)
                .where(
                    Follower.target_id == target.id,
                    Follower.currently_following.is_(True),
                )
            )
            or 0
        )
        snapshot = Snapshot(
            target_id=target.id,
            timestamp=utcnow(),
            status=SnapshotStatus.FAILED,
            error_message=str(exc),
            total_followers=last_known,
        )
        session.add(snapshot)
        session.flush()
        return SnapshotOutcome(
            target_username=username,
            snapshot_id=snapshot.id,
            status=SnapshotStatus.FAILED,
            total_followers=last_known,
            error=str(exc),
        )

    return apply_snapshot(session, target, fetch, settings=settings)


def run_all_targets(
    session: Session,
    scraper: BaseScraper,
    *,
    usernames: list[str] | None = None,
    settings: Settings | None = None,
) -> list[SnapshotOutcome]:
    """Snapshot every configured/active target in turn.

    Sequential on purpose: parallel scrapes multiply the request rate against
    a single account, which is exactly how a login gets flagged.
    """
    settings = settings or get_settings()
    if usernames:
        names = [normalise_username(u) for u in usernames]
    else:
        sync_targets_from_config(session, settings)
        names = [t.username for t in list_targets(session, active_only=True)]

    if not names:
        log.warning(
            "No targets to scrape. Set IG_TARGETS or run: python cli.py add-target <username>"
        )
        return []

    outcomes: list[SnapshotOutcome] = []
    for name in names:
        try:
            outcomes.append(run_snapshot(session, scraper, name, settings=settings))
        except Exception as exc:  # keep the batch alive
            log.exception("Unhandled error while snapshotting @%s: %s", name, exc)
            outcomes.append(
                SnapshotOutcome(
                    target_username=name,
                    snapshot_id=None,
                    status=SnapshotStatus.FAILED,
                    total_followers=0,
                    error=str(exc),
                )
            )
    return outcomes


__all__ = [
    "DiffSets",
    "SnapshotOutcome",
    "apply_snapshot",
    "compute_diff",
    "get_or_create_target",
    "get_target",
    "list_targets",
    "normalise_username",
    "run_all_targets",
    "run_snapshot",
    "sync_targets_from_config",
]
