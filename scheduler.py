"""
scheduler.py — periodic snapshots via APScheduler.

Run it as a long-lived foreground process::

    python scheduler.py

Design notes
------------
* **One job, all targets, sequential.** Targets are scraped one after another
  inside a single job rather than as parallel jobs. All requests come from one
  login, so concurrency would multiply the request rate against that single
  account — the exact pattern that gets a session flagged.
* **`max_instances=1` and `coalesce=True`.** If a run overruns the interval
  (large profile, aggressive backoff), the next fire is skipped rather than
  stacked on top. Two overlapping scrapes would double the request rate at the
  worst possible moment.
* **Jitter.** Firing at exactly :00 every hour is a machine signature. The
  default ±180 s spread costs nothing and looks like a human opening the app.
* **The job never raises.** An exception escaping the job would kill the
  scheduler thread; failures are logged and the next tick carries on.

For deployment, either keep this under a process supervisor (systemd,
`supervisord`, `docker restart: always`), or skip it entirely and drive
`cli.py snapshot` from cron — see the README.
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import Settings, get_settings
from db import bootstrap, session_scope
from scraper import ScraperError, build_scraper
from timeutils import fmt_dual, utcnow
from tracker import run_all_targets

log = logging.getLogger(__name__)


def run_snapshot_job(settings: Settings | None = None) -> None:
    """One scheduled pass over every active target.

    Deliberately catches everything: an escaping exception would take the
    scheduler down with it, turning a transient network blip into silent
    permanent downtime.
    """
    settings = settings or get_settings()
    started = utcnow()
    log.info("--- Snapshot job started at %s ---", fmt_dual(started))

    try:
        with build_scraper(settings) as scraper, session_scope(settings) as session:
            outcomes = run_all_targets(session, scraper, settings=settings)
        for outcome in outcomes:
            log.info("  %s", outcome.summary())
        if not outcomes:
            log.warning("  Nothing to do — no active targets configured.")
    except ScraperError as exc:
        # Backend-level failure (bad session, missing dependency): the next
        # tick will retry, and the message says what to fix.
        log.error("Snapshot job aborted: %s", exc)
    except Exception:
        log.exception("Unhandled error in snapshot job")
    finally:
        elapsed = (utcnow() - started).total_seconds()
        log.info("--- Snapshot job finished in %.1f s ---", elapsed)


def build_scheduler(settings: Settings | None = None) -> BlockingScheduler:
    """Wire up the scheduler without starting it (so tests can inspect it)."""
    settings = settings or get_settings()
    scheduler = BlockingScheduler(timezone="UTC")

    trigger = IntervalTrigger(
        minutes=settings.scrape_interval_minutes,
        jitter=settings.jitter_seconds or None,
    )
    scheduler.add_job(
        run_snapshot_job,
        trigger=trigger,
        args=[settings],
        id="follower_snapshot",
        name="Instagram follower snapshot",
        max_instances=1,   # never overlap two scrapes
        coalesce=True,     # a missed fire runs once, not N times
        misfire_grace_time=int(settings.scrape_interval_minutes * 60 * 0.5),
        replace_existing=True,
    )
    return scheduler


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run periodic follower snapshots.")
    parser.add_argument(
        "--interval",
        help="Override IG_SCRAPE_INTERVAL (hourly, daily, or a number of minutes).",
    )
    parser.add_argument("--backend", help="Override IG_SCRAPER_BACKEND for this run.")
    parser.add_argument(
        "--no-run-on-start",
        action="store_true",
        help="Wait for the first interval instead of snapshotting immediately.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.interval or args.backend:
        from dataclasses import replace

        from config import parse_interval

        settings = replace(
            settings,
            scrape_interval_minutes=(
                parse_interval(args.interval)
                if args.interval
                else settings.scrape_interval_minutes
            ),
            scraper_backend=args.backend or settings.scraper_backend,
        )

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    bootstrap(settings)

    log.info(
        "Scheduler starting: backend=%s interval=%d min jitter=%ds",
        settings.scraper_backend,
        settings.scrape_interval_minutes,
        settings.jitter_seconds,
    )

    if settings.run_on_start and not args.no_run_on_start:
        log.info("Running an immediate snapshot before entering the schedule.")
        run_snapshot_job(settings)

    scheduler = build_scheduler(settings)

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("Signal %s received — finishing the current job then exiting.", signum)
        scheduler.shutdown(wait=True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
