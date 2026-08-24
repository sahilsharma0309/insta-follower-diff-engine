"""
timeutils.py — timezone helpers shared across the tracker.

Every timestamp that reaches the database is stored as an **aware UTC**
datetime. UTC is the only sane storage format: it is monotonic, has no DST
discontinuities, and compares correctly across machines.

The dashboard, however, is read by a human in India, so the requirement is
that every event carries "exact UTC *and* IST" timestamps. Rather than store
both (two columns that can drift out of sync), we store UTC once and derive
IST on read — that is what `to_ist()` and `fmt_dual()` are for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# --- Timezone objects ------------------------------------------------------
UTC = timezone.utc

try:  # Python 3.9+ ships zoneinfo; the tzdata package may still be missing.
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - exercised only on tzdata-less systems
    # India has observed a fixed UTC+05:30 offset with no DST since 1945, so a
    # static offset is a faithful fallback rather than an approximation.
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def utcnow() -> datetime:
    """Current time as an aware UTC datetime.

    Used instead of `datetime.utcnow()`, which returns a *naive* datetime and
    is the single most common source of timezone bugs in Python codebases.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to aware UTC.

    Naive values are *assumed* to already be UTC — that assumption holds
    because the only naive datetimes we ever see come back from SQLite, which
    silently drops the offset it was handed on write.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_ist(value: datetime | None) -> datetime | None:
    """Convert an aware-or-naive UTC datetime to Asia/Kolkata."""
    value = ensure_utc(value)
    return None if value is None else value.astimezone(IST)


def fmt_utc(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    value = ensure_utc(value)
    return "" if value is None else f"{value.strftime(fmt)} UTC"


def fmt_ist(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    value = to_ist(value)
    return "" if value is None else f"{value.strftime(fmt)} IST"


def fmt_dual(value: datetime | None) -> str:
    """Render one instant in both zones, e.g. for CSV exports and tooltips."""
    if value is None:
        return ""
    return f"{fmt_utc(value)} / {fmt_ist(value)}"


def humanise_delta(value: datetime | None, *, now: datetime | None = None) -> str:
    """"3h 12m ago" style relative label used by the dashboard tables."""
    value = ensure_utc(value)
    if value is None:
        return ""
    now = ensure_utc(now) or utcnow()
    seconds = int((now - value).total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"
