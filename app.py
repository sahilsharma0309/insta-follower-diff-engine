"""
app.py — the Streamlit dashboard.

    streamlit run app.py

This module is presentation only. Every number it shows comes from
`analytics.py`; every write it triggers goes through `tracker.py`. Keeping the
split strict means the dashboard can be replaced (FastAPI + React, a notebook,
a CLI report) without touching the engine.

Layout
------
Sidebar   target picker, "snapshot now", date/time range, sort, status filter,
          live search, CSV/JSON export
Cards     Total Followers · Net 24h · New 24h · Lost 24h · Peak Follow Window
Tabs      Followers · Timeline · Trends · Scrape Health

Chart colour choices follow one validated palette: a single blue for
single-series charts, and a blue/red diverging pair for gains vs losses (the
pair clears the colour-vision separation checks — worst-case ΔE 21.6 under
protanopia). Bars are never ramped by their own value: bar length already
encodes magnitude, so spending hue on it too would double-encode and leave
nothing for identity.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from analytics import (
    SORT_MODES,
    STATUS_FILTERS,
    DashboardMetrics,
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
from config import get_settings
from db import bootstrap, session_scope
from timeutils import IST, fmt_dual, to_ist, utcnow
from tracker import list_targets

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Palette — the validated slots this dashboard uses
# ---------------------------------------------------------------------------
# Dark values are the same hues re-stepped for a dark surface, not an
# automatic inversion of the light ones.
_LIGHT = {"blue": "#2a78d6", "red": "#e34948", "neutral": "#a3a29c", "grid": "#e6e5e1"}
_DARK = {"blue": "#3987e5", "red": "#e66767", "neutral": "#6b6a65", "grid": "#3a3a37"}


def palette() -> dict[str, str]:
    """Pick the palette matching the viewer's Streamlit theme."""
    try:  # st.context.theme landed in Streamlit 1.46
        if getattr(st.context, "theme", None) and st.context.theme.type == "dark":
            return _DARK
    except Exception:
        pass
    try:
        if str(st.get_option("theme.base") or "").lower() == "dark":
            return _DARK
    except Exception:
        pass
    return _LIGHT


st.set_page_config(
    page_title="Instagram Follower Tracker",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      /* Tighten Streamlit's default vertical rhythm so the cards sit above the fold */
      .block-container { padding-top: 2.2rem; padding-bottom: 3rem; }
      div[data-testid="stMetric"] {
          background: rgba(128,128,128,0.06);
          border: 1px solid rgba(128,128,128,0.18);
          border-radius: 10px;
          padding: 0.85rem 1rem;
      }
      div[data-testid="stMetricLabel"] { opacity: 0.75; font-size: 0.8rem; }
      .subtle { opacity: 0.65; font-size: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data access helpers
# ---------------------------------------------------------------------------
def load_targets() -> list[dict[str, object]]:
    """Target rows as plain dicts — ORM objects must not outlive their session."""
    with session_scope() as session:
        return [
            {
                "id": target.id,
                "username": target.username,
                "full_name": target.full_name or "",
                "is_active": target.is_active,
                "reported": target.reported_follower_count,
                "last_scraped_at": target.last_scraped_at,
            }
            for target in list_targets(session)
        ]


def ist_bounds(start: date, end: date, start_time: time, end_time: time) -> tuple[datetime, datetime]:
    """Combine the picker's IST date+time into aware UTC bounds.

    The user thinks in IST; the database stores UTC. Converting here — once,
    at the edge — keeps every query below in a single timezone.
    """
    lower = datetime.combine(start, start_time).replace(tzinfo=IST)
    upper = datetime.combine(end, end_time).replace(tzinfo=IST)
    return lower, upper


def run_snapshot_now(username: str) -> str:
    """Trigger one live scrape from the UI and return a summary line."""
    from scraper import ScraperError, build_scraper
    from tracker import run_snapshot

    settings = get_settings()
    try:
        with build_scraper(settings) as scraper, session_scope(settings) as session:
            outcome = run_snapshot(session, scraper, username, settings=settings)
        return outcome.summary()
    except ScraperError as exc:
        return f"Snapshot failed: {exc}"


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def growth_chart(frame: pd.DataFrame, colors: dict[str, str]) -> alt.Chart:
    """Follower total over time — one series, so one hue and no legend."""
    base = alt.Chart(frame).encode(
        x=alt.X(
            "timestamp_ist:T",
            title="Snapshot time (IST)",
            axis=alt.Axis(grid=False, labelAngle=0),
        )
    )
    area = base.mark_area(opacity=0.12, color=colors["blue"]).encode(
        y=alt.Y(
            "total_followers:Q",
            title="Followers",
            # Growth charts are read for shape, not for distance from zero;
            # zero=False keeps a 2% change from looking like a flat line.
            scale=alt.Scale(zero=False, nice=True),
            axis=alt.Axis(grid=True, gridColor=colors["grid"], gridOpacity=0.7),
        )
    )
    line = base.mark_line(color=colors["blue"], strokeWidth=2).encode(
        y=alt.Y("total_followers:Q", scale=alt.Scale(zero=False, nice=True))
    )
    points = (
        base.mark_circle(size=64, color=colors["blue"], opacity=0)
        .encode(
            y=alt.Y("total_followers:Q", scale=alt.Scale(zero=False, nice=True)),
            opacity=alt.condition(
                alt.datum.new_followers + alt.datum.lost_followers > 0,
                alt.value(0.85),
                alt.value(0.0),
            ),
            tooltip=[
                alt.Tooltip("timestamp_ist:T", title="When (IST)", format="%d %b %H:%M"),
                alt.Tooltip("total_followers:Q", title="Total followers"),
                alt.Tooltip("new_followers:Q", title="Gained"),
                alt.Tooltip("lost_followers:Q", title="Lost"),
                alt.Tooltip("status:N", title="Snapshot"),
            ],
        )
    )
    return (area + line + points).properties(height=280)


def activity_chart(frame: pd.DataFrame, colors: dict[str, str]) -> alt.Chart:
    """Gains above the axis, losses below — a diverging bar around zero.

    Follows and unfollows are opposite polarity, not two arbitrary categories,
    so this is the diverging case: two hues that read as opposite, meeting at a
    neutral zero line. One axis; the net is the visible difference.
    """
    long = pd.concat(
        [
            pd.DataFrame(
                {
                    "date_ist": frame["date_ist"],
                    "count": frame["follows"],
                    "direction": "Gained",
                    "magnitude": frame["follows"],
                }
            ),
            pd.DataFrame(
                {
                    "date_ist": frame["date_ist"],
                    # Negative so losses render below the baseline.
                    "count": -frame["unfollows"],
                    "direction": "Lost",
                    "magnitude": frame["unfollows"],
                }
            ),
        ]
    )
    bars = (
        alt.Chart(long)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3, stroke=None)
        .encode(
            x=alt.X("date_ist:T", title="Date (IST)", axis=alt.Axis(grid=False, labelAngle=0)),
            y=alt.Y(
                "count:Q",
                title="Followers gained / lost",
                axis=alt.Axis(grid=True, gridColor=colors["grid"], gridOpacity=0.7),
            ),
            color=alt.Color(
                "direction:N",
                title=None,
                scale=alt.Scale(
                    domain=["Gained", "Lost"], range=[colors["blue"], colors["red"]]
                ),
                legend=alt.Legend(orient="top", direction="horizontal"),
            ),
            tooltip=[
                alt.Tooltip("date_ist:T", title="Date (IST)", format="%d %b %Y"),
                alt.Tooltip("direction:N", title="Direction"),
                alt.Tooltip("magnitude:Q", title="Accounts"),
            ],
        )
    )
    zero = (
        alt.Chart(pd.DataFrame({"y": [0]}))
        .mark_rule(color=colors["neutral"], strokeWidth=1)
        .encode(y="y:Q")
    )
    return (bars + zero).properties(height=260)


def peak_hours_chart(frame: pd.DataFrame, colors: dict[str, str]) -> alt.Chart:
    """Follows by hour of day in IST.

    Every bar is the same hue on purpose: bar height already encodes the count,
    so shading the bars by that same count would double-encode one variable and
    add no information.
    """
    return (
        alt.Chart(frame)
        .mark_bar(
            color=colors["blue"],
            cornerRadiusTopLeft=3,
            cornerRadiusTopRight=3,
            size=18,
        )
        .encode(
            x=alt.X(
                "label:N",
                title="Hour of day (IST)",
                sort=list(frame["label"]),
                axis=alt.Axis(grid=False, labelAngle=0, labelOverlap=False),
            ),
            y=alt.Y(
                "follows:Q",
                title="New followers detected",
                axis=alt.Axis(grid=True, gridColor=colors["grid"], gridOpacity=0.7),
            ),
            tooltip=[
                alt.Tooltip("label:N", title="Hour (IST)"),
                alt.Tooltip("follows:Q", title="New followers"),
            ],
        )
        .properties(height=240)
    )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
bootstrap()
settings = get_settings()
colors = palette()
targets = load_targets()

st.sidebar.title("📊 Follower Tracker")

if not targets:
    st.sidebar.info("No profiles are being tracked yet.")
    st.title("Instagram Follower Tracker")
    st.warning("There is nothing to display — the database has no targets yet.")
    st.markdown(
        """
### Get started in one command

Generate three weeks of synthetic history and reload this page:

```bash
python cli.py seed-demo
```

Or start tracking a real profile:

```bash
python cli.py add-target some_public_page
python cli.py snapshot --backend playwright
```
        """
    )
    st.stop()

target_labels = {
    f"@{t['username']}" + (f" — {t['full_name']}" if t["full_name"] else ""): t
    for t in targets
}
selected_label = st.sidebar.selectbox("Target profile", list(target_labels))
target = target_labels[selected_label]
target_id = int(target["id"])
target_username = str(target["username"])

st.sidebar.caption(f"Backend: `{settings.scraper_backend}`  ·  every {settings.scrape_interval_minutes} min")

if st.sidebar.button("Run snapshot now", width="stretch", type="primary"):
    with st.spinner(f"Scraping @{target_username}…"):
        message = run_snapshot_now(target_username)
    if message.lower().startswith("snapshot failed"):
        st.sidebar.error(message)
    else:
        st.sidebar.success(message)

st.sidebar.divider()

# --- Search ----------------------------------------------------------------
# Streamlit reruns the script on every keystroke-committed change, so a plain
# text input already behaves as live search — no callback plumbing needed.
search = st.sidebar.text_input(
    "Search username", placeholder="e.g. aarav", help="Matches username or display name."
)

# --- Chronological sort ----------------------------------------------------
sort_mode = st.sidebar.selectbox("Chronological sort", SORT_MODES, index=0)
status_filter = st.sidebar.selectbox("Follower status", STATUS_FILTERS, index=0)

# --- Date & time range -----------------------------------------------------
st.sidebar.markdown("**Date range** <span class='subtle'>(IST)</span>", unsafe_allow_html=True)
today_ist = to_ist(utcnow()).date()
default_start = today_ist - timedelta(days=14)
date_range = st.sidebar.date_input(
    "Detected between",
    value=(default_start, today_ist),
    max_value=today_ist,
    label_visibility="collapsed",
)
# `date_input` with a tuple returns a 1-tuple mid-edit, before the second click.
if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = (
        date_range[0] if isinstance(date_range, (tuple, list)) else date_range
    )

with st.sidebar.expander("Narrow to a time of day"):
    start_time = st.time_input("From", value=time(0, 0), step=timedelta(minutes=15))
    end_time = st.time_input("To", value=time(23, 59), step=timedelta(minutes=15))

date_from, date_to = ist_bounds(start_date, end_date, start_time, end_time)

date_field_label = st.sidebar.radio(
    "Apply the range to",
    ["First detected", "Unfollowed at", "Last seen"],
    horizontal=False,
)
date_field = {
    "First detected": "first_detected_at",
    "Unfollowed at": "unfollowed_at",
    "Last seen": "last_seen_at",
}[date_field_label]

apply_dates = st.sidebar.checkbox("Apply the date filter", value=False)

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
bounds = (date_from, date_to) if apply_dates else (None, None)

with session_scope() as session:
    metrics: DashboardMetrics = get_dashboard_metrics(session, target_id)
    followers_frame = query_followers(
        session,
        target_id,
        search=search,
        sort_mode=sort_mode,
        status=status_filter,
        date_from=bounds[0],
        date_to=bounds[1],
        date_field=date_field,  # type: ignore[arg-type]
    )
    events_frame = query_events(
        session, target_id, search=search, date_from=bounds[0], date_to=bounds[1]
    )
    growth_frame = growth_timeseries(session, target_id, date_from=bounds[0], date_to=bounds[1])
    activity_frame = daily_activity(session, target_id, days=30)
    hours_frame = peak_follow_hours(session, target_id, date_from=bounds[0], date_to=bounds[1])
    snapshots_frame = snapshot_history(session, target_id)

# ---------------------------------------------------------------------------
# Header + metric cards
# ---------------------------------------------------------------------------
st.title(f"@{target_username}")
caption = f"Last snapshot: {metrics.last_snapshot_label}"
if metrics.last_snapshot_at:
    caption += f"  ·  {fmt_dual(metrics.last_snapshot_at)}"
caption += f"  ·  {metrics.snapshot_count} snapshots on record"
st.caption(caption)

card1, card2, card3, card4, card5 = st.columns(5)
card1.metric("Total followers", f"{metrics.total_followers:,}")
card2.metric(
    "Net change · 24h",
    f"{metrics.net_24h:+,}",
    delta=f"{metrics.net_24h:+,}",
    delta_color="normal" if metrics.net_24h else "off",
)
card3.metric("Gained · 24h", f"{metrics.gained_24h:,}")
card4.metric("Lost · 24h", f"{metrics.lost_24h:,}")
card5.metric(
    "Peak follow hour · IST",
    metrics.peak_follow_hour_label,
    help="The hour of day, in IST, when this profile most often gains followers.",
)

# A capture well below the profile's own claimed count means pagination is
# being truncated — surfacing it prevents reading a short scrape as churn.
completeness = metrics.capture_completeness
if completeness is not None and completeness < 0.95:
    st.warning(
        f"Captured {metrics.total_followers:,} of the {metrics.reported_follower_count:,} "
        f"followers this profile reports ({completeness:.0%}). The last run was probably "
        "truncated by the per-run cap or a rate limit — treat recent unfollow counts with care."
    )
if metrics.last_snapshot_status in {"PARTIAL", "FAILED"}:
    st.info(
        f"The most recent snapshot finished as **{metrics.last_snapshot_status}**. "
        "See the Scrape Health tab for the reason."
    )

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_followers, tab_timeline, tab_trends, tab_health = st.tabs(
    ["Followers", "Timeline", "Trends", "Scrape Health"]
)

# --- Followers -------------------------------------------------------------
with tab_followers:
    left, right = st.columns([3, 1])
    left.subheader(f"{len(followers_frame):,} followers match")
    filters_on = bool(search) or status_filter != "All" or apply_dates
    if filters_on:
        left.caption(
            f"Filters — search: {search or '—'} · status: {status_filter} · "
            f"dates: {'on' if apply_dates else 'off'} · sort: {sort_mode}"
        )

    if followers_frame.empty:
        st.info("No followers match the current filters.")
    else:
        display = followers_frame.assign(
            profile=followers_frame["profile_url"],
        )[
            [
                "username",
                "full_name",
                "status",
                "first_detected_ist",
                "first_detected_utc",
                "first_detected_ago",
                "unfollowed_ist",
                "snapshots_present",
                "times_followed",
                "is_verified",
                "is_private",
                "profile",
            ]
        ]
        st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            height=460,
            column_config={
                "username": st.column_config.TextColumn("Username", width="medium"),
                "full_name": st.column_config.TextColumn("Name", width="medium"),
                "status": st.column_config.TextColumn("State", width="small"),
                "first_detected_ist": st.column_config.DatetimeColumn(
                    "First detected (IST)", format="DD MMM YYYY, HH:mm"
                ),
                "first_detected_utc": st.column_config.DatetimeColumn(
                    "First detected (UTC)", format="DD MMM YYYY, HH:mm"
                ),
                "first_detected_ago": st.column_config.TextColumn("Age", width="small"),
                "unfollowed_ist": st.column_config.DatetimeColumn(
                    "Unfollowed (IST)", format="DD MMM YYYY, HH:mm"
                ),
                "snapshots_present": st.column_config.NumberColumn("Snapshots", width="small"),
                "times_followed": st.column_config.NumberColumn("Follow cycles", width="small"),
                "is_verified": st.column_config.CheckboxColumn("Verified", width="small"),
                "is_private": st.column_config.CheckboxColumn("Private", width="small"),
                "profile": st.column_config.LinkColumn("Profile", display_text="open"),
            },
        )

    st.divider()
    st.markdown("**Export the filtered follower log**")
    export1, export2, _ = st.columns([1, 1, 3])
    export1.download_button(
        "Download CSV",
        data=to_csv_bytes(followers_frame),
        file_name=export_filename(target_username, "followers", "csv"),
        mime="text/csv",
        width="stretch",
        disabled=followers_frame.empty,
    )
    export2.download_button(
        "Download JSON",
        data=to_json_bytes(
            followers_frame, meta={"target": target_username, "kind": "followers"}
        ),
        file_name=export_filename(target_username, "followers", "json"),
        mime="application/json",
        width="stretch",
        disabled=followers_frame.empty,
    )

# --- Timeline --------------------------------------------------------------
with tab_timeline:
    st.subheader("Chronological event log")
    st.caption(
        "Append-only record of every follow and unfollow, newest first. "
        "Timestamps are the moment a snapshot *detected* the change."
    )
    if events_frame.empty:
        st.info("No events recorded for the current filters.")
    else:
        st.dataframe(
            events_frame,
            width="stretch",
            hide_index=True,
            height=460,
            column_config={
                "event": st.column_config.TextColumn("Event", width="small"),
                "username": st.column_config.TextColumn("Username", width="medium"),
                "timestamp_ist": st.column_config.DatetimeColumn(
                    "When (IST)", format="DD MMM YYYY, HH:mm:ss"
                ),
                "timestamp_utc": st.column_config.DatetimeColumn(
                    "When (UTC)", format="DD MMM YYYY, HH:mm:ss"
                ),
                "when": st.column_config.TextColumn("Relative", width="small"),
                "snapshot_id": st.column_config.NumberColumn("Snapshot", width="small"),
            },
        )
        st.divider()
        ev1, ev2, _ = st.columns([1, 1, 3])
        ev1.download_button(
            "Download CSV",
            data=to_csv_bytes(events_frame),
            file_name=export_filename(target_username, "events", "csv"),
            mime="text/csv",
            width="stretch",
        )
        ev2.download_button(
            "Download JSON",
            data=to_json_bytes(events_frame, meta={"target": target_username, "kind": "events"}),
            file_name=export_filename(target_username, "events", "json"),
            mime="application/json",
            width="stretch",
        )

# --- Trends ----------------------------------------------------------------
with tab_trends:
    st.subheader("Follower total over time")
    if growth_frame.empty:
        st.info("No snapshots in this range yet.")
    else:
        st.altair_chart(growth_chart(growth_frame, colors), width="stretch")

    st.subheader("Daily gains and losses")
    st.caption("Last 30 days, bucketed by IST calendar day.")
    if activity_frame.empty:
        st.info("No follow or unfollow events in the last 30 days.")
    else:
        st.altair_chart(activity_chart(activity_frame, colors), width="stretch")

    st.subheader("When do people follow?")
    if hours_frame["follows"].sum() == 0:
        st.info("Not enough follow events yet to find a pattern.")
    else:
        st.altair_chart(peak_hours_chart(hours_frame, colors), width="stretch")
        st.caption(
            f"Busiest window: **{metrics.peak_follow_window}** "
            f"({metrics.peak_follow_hour_count} new followers detected). "
            "Resolution is limited by the snapshot interval — currently "
            f"{settings.scrape_interval_minutes} minutes."
        )

# --- Scrape health ---------------------------------------------------------
with tab_health:
    st.subheader("Snapshot history")
    st.caption(
        "PARTIAL means the run was truncated or lost an implausible share of the "
        "follower set — the diff engine deliberately skips unfollow detection on "
        "those runs rather than record a false mass-unfollow."
    )
    if snapshots_frame.empty:
        st.info("No snapshots recorded yet.")
    else:
        healthy = (snapshots_frame["status"] == "SUCCESS").sum()
        h1, h2, h3 = st.columns(3)
        h1.metric("Runs recorded", f"{len(snapshots_frame):,}")
        h2.metric("Successful", f"{healthy:,}")
        h3.metric(
            "Median duration",
            f"{snapshots_frame['duration_seconds'].median():.1f}s",
        )
        st.dataframe(
            snapshots_frame,
            width="stretch",
            hide_index=True,
            height=380,
            column_config={
                "snapshot_id": st.column_config.NumberColumn("ID", width="small"),
                "timestamp_ist": st.column_config.DatetimeColumn(
                    "When (IST)", format="DD MMM YYYY, HH:mm"
                ),
                "timestamp_utc": st.column_config.DatetimeColumn(
                    "When (UTC)", format="DD MMM YYYY, HH:mm"
                ),
                "status": st.column_config.TextColumn("Status", width="small"),
                "total_followers": st.column_config.NumberColumn("Total"),
                "captured_followers": st.column_config.NumberColumn("Captured"),
                "reported_followers": st.column_config.NumberColumn("Reported"),
                "new_followers": st.column_config.NumberColumn("Gained", width="small"),
                "lost_followers": st.column_config.NumberColumn("Lost", width="small"),
                "duration_seconds": st.column_config.NumberColumn("Seconds", width="small"),
                "error_message": st.column_config.TextColumn("Error", width="large"),
            },
        )

st.divider()
st.caption(
    "Timestamps are stored in UTC and rendered in both UTC and IST. "
    "Detection time is the snapshot that first saw a change, not the instant the "
    "button was pressed — Instagram does not expose that to third parties."
)
