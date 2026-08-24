"""
cli.py — command-line entry point for everything that is not the dashboard.

    python cli.py init-db                     # create the schema
    python cli.py seed-demo                   # 3 weeks of synthetic history
    python cli.py add-target campus_confessions
    python cli.py list-targets
    python cli.py snapshot                    # scrape every active target once
    python cli.py snapshot --target foo --backend playwright
    python cli.py events --target foo --limit 20
    python cli.py export --target foo --format csv --kind followers
    python cli.py schedule                    # hand off to scheduler.py

Every subcommand is a thin wrapper over the library modules — the CLI holds no
business logic of its own, so anything it can do is equally reachable from a
notebook or another script.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from config import SCRAPER_BACKENDS, ConfigError, Settings, get_settings, parse_interval
from db import bootstrap, session_scope
from timeutils import fmt_dual, utcnow

log = logging.getLogger("cli")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _settings_from_args(args: argparse.Namespace) -> Settings:
    """Apply per-invocation overrides on top of the environment."""
    settings = get_settings()
    overrides: dict[str, object] = {}
    if getattr(args, "backend", None):
        if args.backend not in SCRAPER_BACKENDS:
            raise ConfigError(
                f"--backend must be one of {sorted(SCRAPER_BACKENDS)}, got {args.backend!r}"
            )
        overrides["scraper_backend"] = args.backend
    if getattr(args, "database_url", None):
        overrides["database_url"] = args.database_url
    if getattr(args, "interval", None):
        overrides["scrape_interval_minutes"] = parse_interval(args.interval)
    if overrides:
        settings = replace(settings, **overrides)  # type: ignore[arg-type]
        if "database_url" in overrides:
            from db import reset_engine

            reset_engine()
    return settings


def _print_table(rows: list[dict[str, object]], columns: list[str]) -> None:
    """Minimal fixed-width table so the CLI needs no extra dependency."""
    if not rows:
        print("  (nothing to show)")
        return
    widths = {
        col: max(len(col), max(len(str(row.get(col, ""))) for row in rows)) for col in columns
    }
    header = "  ".join(col.ljust(widths[col]) for col in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    bootstrap(settings)
    print(f"Schema ready at {settings.database_url}")
    return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    from seed_demo import seed

    settings = _settings_from_args(args)
    bootstrap(settings)
    names = args.usernames or ["campus_confessions", "city_secrets_page"]
    seed(names, days=args.days, interval_hours=args.interval_hours, reset=not args.keep)
    print(f"Seeded {len(names)} target(s) with {args.days} days of history.")
    print("Next: streamlit run app.py")
    return 0


def cmd_add_target(args: argparse.Namespace) -> int:
    from tracker import get_or_create_target

    settings = _settings_from_args(args)
    bootstrap(settings)
    with session_scope(settings) as session:
        target = get_or_create_target(session, args.username)
        target.is_active = True
        print(f"Tracking @{target.username} (id={target.id})")
    return 0


def cmd_remove_target(args: argparse.Namespace) -> int:
    from tracker import get_target

    settings = _settings_from_args(args)
    bootstrap(settings)
    with session_scope(settings) as session:
        target = get_target(session, args.username)
        if target is None:
            print(f"No such target: @{args.username}")
            return 1
        if args.purge:
            session.delete(target)  # cascades to followers/events/snapshots
            print(f"Deleted @{args.username} and all its history.")
        else:
            target.is_active = False
            print(f"Paused @{args.username}. History kept; use --purge to delete it.")
    return 0


def cmd_list_targets(args: argparse.Namespace) -> int:
    from analytics import get_dashboard_metrics
    from tracker import list_targets

    settings = _settings_from_args(args)
    bootstrap(settings)
    with session_scope(settings) as session:
        targets = list_targets(session)
        rows = []
        for target in targets:
            metrics = get_dashboard_metrics(session, target.id)
            rows.append(
                {
                    "username": f"@{target.username}",
                    "active": "yes" if target.is_active else "paused",
                    "followers": metrics.total_followers,
                    "net_24h": f"{metrics.net_24h:+d}",
                    "snapshots": metrics.snapshot_count,
                    "last_run": metrics.last_snapshot_label,
                }
            )
    _print_table(
        rows, ["username", "active", "followers", "net_24h", "snapshots", "last_run"]
    )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from scraper import ScraperError, build_scraper
    from tracker import run_all_targets

    settings = _settings_from_args(args)
    bootstrap(settings)
    print(f"Backend: {settings.scraper_backend}   Started: {fmt_dual(utcnow())}")

    try:
        with build_scraper(settings) as scraper, session_scope(settings) as session:
            outcomes = run_all_targets(
                session, scraper, usernames=args.target or None, settings=settings
            )
    except ScraperError as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
        return 1

    if not outcomes:
        print("No targets configured. Add one with: python cli.py add-target <username>")
        return 1

    for outcome in outcomes:
        print(f"  {outcome.summary()}")
        if args.verbose:
            for username in outcome.new_followers[:20]:
                print(f"      + {username}")
            for username in outcome.lost_followers[:20]:
                print(f"      - {username}")
    return 0 if all(o.status.value != "FAILED" for o in outcomes) else 1


def cmd_events(args: argparse.Namespace) -> int:
    from analytics import query_events
    from tracker import get_target

    settings = _settings_from_args(args)
    bootstrap(settings)
    with session_scope(settings) as session:
        target = get_target(session, args.target)
        if target is None:
            print(f"No such target: @{args.target}")
            return 1
        since = utcnow() - timedelta(days=args.days) if args.days else None
        frame = query_events(session, target.id, date_from=since, limit=args.limit)

    if frame.empty:
        print("  (no events)")
        return 0
    rows = [
        {
            "event": row.event,
            "username": f"@{row.username}",
            "utc": row.timestamp_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "ist": row.timestamp_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "when": row.when,
        }
        for row in frame.itertuples()
    ]
    _print_table(rows, ["event", "username", "utc", "ist", "when"])
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from analytics import (
        export_filename,
        query_events,
        query_followers,
        to_csv_bytes,
        to_json_bytes,
    )
    from tracker import get_target

    settings = _settings_from_args(args)
    bootstrap(settings)
    settings.ensure_directories()

    with session_scope(settings) as session:
        target = get_target(session, args.target)
        if target is None:
            print(f"No such target: @{args.target}")
            return 1
        since = utcnow() - timedelta(days=args.days) if args.days else None
        if args.kind == "followers":
            frame = query_followers(session, target.id, date_from=since, sort_mode="Latest Added")
        else:
            frame = query_events(session, target.id, date_from=since, limit=None)
        meta = {"target": target.username, "kind": args.kind}

    payload = to_csv_bytes(frame) if args.format == "csv" else to_json_bytes(frame, meta=meta)
    destination = (
        Path(args.output)
        if args.output
        else settings.export_dir / export_filename(args.target, args.kind, args.format)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    print(f"Wrote {len(frame)} rows to {destination}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    import scheduler

    forwarded: list[str] = []
    if args.interval:
        forwarded += ["--interval", args.interval]
    if args.backend:
        forwarded += ["--backend", args.backend]
    if args.no_run_on_start:
        forwarded.append("--no-run-on-start")
    return scheduler.main(forwarded)


def cmd_config(args: argparse.Namespace) -> int:
    """Print the resolved configuration, with secrets masked."""
    settings = _settings_from_args(args)
    print("Resolved configuration")
    print("=" * 60)
    for key, value in sorted(vars(settings).items()):
        if key == "extra":
            continue
        if any(word in key for word in ("password", "secret")):
            value = "***set***" if value else "(unset)"
        if key == "targets":
            value = ", ".join(t.username for t in settings.targets) or "(none)"
        print(f"  {key:<28} {value}")
    print("=" * 60)
    print(f"  session cookie file          {settings.session_state_path}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Instagram follower tracker — scrape, diff and export.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--database-url", help="Override IG_DATABASE_URL.")
    # Log verbosity is a global concern; `snapshot --verbose` separately
    # controls whether changed usernames are printed. Keeping them distinct
    # avoids argparse's subparser-overrides-parent-default trap.
    parser.add_argument("--debug", action="store_true", help="Debug-level logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the database schema.").set_defaults(
        func=cmd_init_db
    )

    seed = sub.add_parser("seed-demo", help="Generate synthetic history for the dashboard.")
    seed.add_argument("usernames", nargs="*", help="Handles to seed.")
    seed.add_argument("--days", type=int, default=21)
    seed.add_argument("--interval-hours", type=int, default=1)
    seed.add_argument("--keep", action="store_true", help="Append instead of resetting.")
    seed.set_defaults(func=cmd_seed_demo)

    add = sub.add_parser("add-target", help="Start tracking a profile.")
    add.add_argument("username")
    add.set_defaults(func=cmd_add_target)

    remove = sub.add_parser("remove-target", help="Pause or delete a tracked profile.")
    remove.add_argument("username")
    remove.add_argument("--purge", action="store_true", help="Delete all stored history too.")
    remove.set_defaults(func=cmd_remove_target)

    sub.add_parser("list-targets", help="Show tracked profiles and their headline numbers.").set_defaults(
        func=cmd_list_targets
    )

    snap = sub.add_parser("snapshot", help="Scrape targets once and record the diff.")
    snap.add_argument("--target", action="append", help="Repeatable; defaults to all active.")
    snap.add_argument("--backend", choices=sorted(SCRAPER_BACKENDS))
    snap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="List the usernames that joined and left, not just the counts.",
    )
    snap.set_defaults(func=cmd_snapshot)

    events = sub.add_parser("events", help="Print the recent follow/unfollow timeline.")
    events.add_argument("--target", required=True)
    events.add_argument("--days", type=int, default=7)
    events.add_argument("--limit", type=int, default=50)
    events.set_defaults(func=cmd_events)

    export = sub.add_parser("export", help="Write follower or event logs to CSV/JSON.")
    export.add_argument("--target", required=True)
    export.add_argument("--kind", choices=["followers", "events"], default="followers")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--days", type=int, default=0, help="0 = all history.")
    export.add_argument("--output", help="Destination path; defaults to the export dir.")
    export.set_defaults(func=cmd_export)

    schedule = sub.add_parser("schedule", help="Run periodic snapshots in the foreground.")
    schedule.add_argument("--interval", help="hourly, daily, or a number of minutes.")
    schedule.add_argument("--backend", choices=sorted(SCRAPER_BACKENDS))
    schedule.add_argument("--no-run-on-start", action="store_true")
    schedule.set_defaults(func=cmd_schedule)

    sub.add_parser("config", help="Print the resolved configuration.").set_defaults(
        func=cmd_config
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
