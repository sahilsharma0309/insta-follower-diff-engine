# insta-follower-diff-engine

Track a public Instagram profile's follower list over time: who joined, who
left, and exactly when. Periodic snapshots are diffed with set arithmetic,
every transition is written to an append-only event log with UTC **and** IST
timestamps, and a Streamlit dashboard makes the history searchable,
filterable and exportable.

```
scraper.py  ──▶  tracker.py  ──▶  SQLite / Postgres  ──▶  app.py
 fetch the      diff vs. the      targets · followers      search, filter,
 follower list  stored set        events · snapshots       chart, export
```

**It runs out of the box with no credentials.** The default `demo` backend
generates synthetic data, so you can seed three weeks of history and explore
the full dashboard in two commands.

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python cli.py seed-demo            # 3 weeks of synthetic history
streamlit run app.py               # dashboard at http://localhost:8501
```

To track a real profile, see [Live scraping](#live-scraping) below.

---

## Scope and responsible use

Read this before pointing the tool at anyone.

- **Automated collection is against Instagram's Terms of Use**, regardless of
  how politely it is paced. That is a real risk to the account you log in
  with: rate limits, checkpoints, and permanent disablement. Use a throwaway
  account, never your main one.
- Follower lists of public accounts are visible to any logged-in user, but
  *bulk collection with timestamps* is different in kind from browsing. It
  builds a dated record of real people's behaviour.
- Follower handles are **personal data**. If you are doing anything beyond
  personal use on your own account, obtain consent and check what GDPR / the
  DPDP Act require of you.
- Track profiles you own or have permission to track. Monitoring individuals
  who have not consented — and confession-page audiences in particular, where
  the whole point is that the association is sensitive — is the use case this
  tool should not be put to.

The `demo` backend exists so the code can be developed, tested and
demonstrated without any of the above applying.

---

## Project structure

| File | Role |
|---|---|
| `config.py` | Every setting, read from env / `.env`. Nothing secret in source. |
| `models.py` | SQLAlchemy 2.0 schema: `targets`, `followers`, `follower_events`, `snapshots`. |
| `db.py` | Engine + `session_scope()` transaction helper. |
| `scraper.py` | Three interchangeable backends, rate limiting, retries, session persistence. |
| `tracker.py` | The diff engine: set difference → events, with the safety guards. |
| `analytics.py` | Every dashboard query. Filtering happens in SQL, not pandas. |
| `app.py` | The Streamlit dashboard. |
| `scheduler.py` | APScheduler loop for periodic snapshots. |
| `cli.py` | `init-db`, `snapshot`, `export`, `events`, `add-target`, … |
| `seed_demo.py` | Backdated synthetic history generator. |
| `tests/` | 101 tests — `pytest` |

---

## How the diff engine works

Each run compares the freshly-scraped set against the stored one:

```python
new      = current - previous     # → FOLLOW event, first_detected_at
lost     = previous - current     # → UNFOLLOW event, unfollowed_at
retained = current & previous     # → bump the persistence counters
```

The arithmetic is the easy part. What makes it trustworthy is **refusing to
apply it to bad input**, because `follower_events` is append-only — a missed
unfollow is corrected by the next healthy run, but a false mass-unfollow
poisons the history permanently. Two guards sit in front of the unfollow
branch:

1. **Incomplete fetch.** If pagination was truncated (rate limit, per-run cap,
   mid-stream error), accounts we never reached look identical to accounts
   that left. Truncated runs process *follows only* and are marked `PARTIAL`.
2. **Implausible collapse.** If a run would remove more than
   `IG_MAX_UNFOLLOW_RATIO` (default 50%) of the known set **and** at least
   `IG_MIN_UNFOLLOWS_FOR_GUARD` (default 10) accounts, it is treated as a short
   read. Both conditions are required — on a 3-follower page, losing one is
   33% and perfectly normal.

Follows are always safe to record: *seeing* an account is positive evidence,
whereas *not seeing* one is not.

A follower who leaves and returns keeps their original row and
`first_detected_at`; `times_followed` counts the cycles.

---

## Database schema

| Table | Contents |
|---|---|
| `targets` | `id`, `username`, `full_name`, `created_at`, plus profile metadata |
| `followers` | `id`, `target_id`, `instagram_username`, `user_id`, `profile_pic_url`, `is_private`, `is_verified`, `first_detected_at`, `unfollowed_at`, `currently_following`, `times_followed`, `snapshots_present` |
| `follower_events` | `id`, `follower_id`, `target_id`, `event_type` (FOLLOW/UNFOLLOW), `timestamp` |
| `snapshots` | `id`, `target_id`, `total_followers`, `timestamp`, `status`, counts, duration |

`followers` is a materialised current-state table rather than per-snapshot
membership: the diff becomes one indexed query instead of a scan over all
history, and it answers "when was this account first seen?" in O(1).

All timestamps are stored as aware UTC (a `UTCDateTime` type decorator makes
SQLite behave like Postgres here) and rendered in both UTC and IST on read.

Default storage is SQLite under `runtime/`. For Postgres, set one variable —
no code changes:

```bash
IG_DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/tracker
```

---

## Live scraping

Choose a backend with `IG_SCRAPER_BACKEND`:

| Backend | Needs | Notes |
|---|---|---|
| `demo` *(default)* | nothing | Synthetic. Offline. What the tests run against. |
| `playwright` | `pip install playwright && playwright install chromium` | Headless Chromium. Logs in once, persists cookies, then reads Instagram's own paginated followers endpoint. |
| `instaloader` | `pip install instaloader` | Wraps the `instaloader` library with our rate limiter. |

```bash
cp .env.example .env         # then fill in IG_USERNAME / IG_PASSWORD
python cli.py add-target some_public_page
python cli.py snapshot --backend playwright
```

The password is needed **once**, to mint a session. After that the cookies in
`runtime/sessions/` are reused (mode `0600`, gitignored) and you can blank the
password again. Hitting a 2FA or "suspicious login" challenge? Re-run with
`IG_HEADLESS=false`, solve it by hand, and the session is saved.

### Staying unblocked

The defaults are deliberately slow, and the reason is reliability rather than
etiquette: a scraper that backs off outlives a fast one.

- Randomised **2.5–6 s** gap between requests (jittered — perfectly regular
  timing is a stronger bot signal than volume).
- Hard ceiling of **180 requests/hour**; the limiter blocks past it.
- Exponential backoff with jitter on 429/5xx/network errors, honouring
  `Retry-After` when the server sends one. Auth failures and 404s are *not*
  retried.
- Targets are scraped sequentially — parallelism would multiply the request
  rate against a single login.
- Proxy support via `IG_PROXY_SERVER` (routes both navigation and API calls).

---

## Scheduling

```bash
python scheduler.py                       # uses IG_SCRAPE_INTERVAL
python scheduler.py --interval 30         # or 15min/hourly/daily/<minutes>
```

`max_instances=1` and `coalesce=True` mean a long run never stacks on the next
fire, and `IG_SCHEDULE_JITTER` (default ±180 s) keeps runs off exact clock
boundaries. The job never raises — a transient failure is logged and the next
tick continues.

Prefer cron? The CLI is a clean entry point:

```cron
17 * * * * cd /path/to/repo && .venv/bin/python cli.py snapshot >> runtime/cron.log 2>&1
```

The snapshot interval sets your detection resolution: hourly snapshots
timestamp a follow to within an hour. Instagram does not expose the actual
moment someone pressed Follow to third parties, so no tool can do better than
its own polling rate — the dashboard says so rather than implying false
precision.

---

## Dashboard

`streamlit run app.py`

- **Metric cards** — total followers, net change / gained / lost in 24 h, peak
  follow hour (IST).
- **Live search** by username or display name.
- **Chronological sort** — Latest Added, Oldest Tracked, Recently Unfollowed,
  Longest Retained, A–Z.
- **Date & time range** picked in IST, applied to first-detected, unfollowed-at
  or last-seen.
- **Status filter** — Currently Following / New / Retained / Unfollowed.
- **Charts** — follower total over time, daily gains vs losses, follows by hour
  of day.
- **Export** — CSV or JSON of the *filtered* view, timestamps in both zones.
- **Scrape Health** tab — per-run status, duration and errors, so a `PARTIAL`
  run is visible rather than silently skewing the numbers.

---

## CLI

```bash
python cli.py init-db
python cli.py seed-demo [names...] [--days 21] [--keep]
python cli.py add-target campus_confessions
python cli.py remove-target campus_confessions [--purge]
python cli.py list-targets
python cli.py snapshot [--target X] [--backend playwright] [-v]
python cli.py events --target X --days 7 --limit 50
python cli.py export --target X --kind followers --format csv
python cli.py schedule [--interval hourly]
python cli.py config          # resolved settings, secrets masked
```

---

## Configuration

Every variable is documented in `.env.example`; all have working defaults.
The ones you are most likely to touch:

| Variable | Default | Purpose |
|---|---|---|
| `IG_TARGETS` | — | Comma-separated handles to track |
| `IG_SCRAPER_BACKEND` | `demo` | `demo` / `playwright` / `instaloader` |
| `IG_USERNAME`, `IG_PASSWORD` | — | Login for the live backends |
| `IG_DATABASE_URL` | SQLite in `runtime/` | Point at Postgres to switch |
| `IG_SCRAPE_INTERVAL` | `hourly` | `15min`…`weekly`, or minutes |
| `IG_MIN/MAX_REQUEST_DELAY` | `2.5` / `6.0` | Randomised inter-request gap |
| `IG_MAX_REQUESTS_PER_HOUR` | `180` | Hard hourly ceiling |
| `IG_MAX_FOLLOWERS_PER_RUN` | `0` (unlimited) | Cap; a capped run is `PARTIAL` |
| `IG_PROXY_SERVER` | — | e.g. `http://host:8000` |
| `IG_MAX_UNFOLLOW_RATIO` | `0.5` | Implausible-collapse threshold |

---

## Tests

```bash
pytest
```

101 tests, no network, ~2 s. They cover the diff arithmetic, both safety
guards, the returning-follower path, rate-limiter and backoff behaviour (on a
fake clock), every dashboard query and filter, the UTC/IST conversions, and
export round-trips.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No targets configured` | Set `IG_TARGETS=...` or run `python cli.py add-target <name>` |
| Snapshots all `PARTIAL` | Capture is short of the reported count — check `IG_MAX_FOLLOWERS_PER_RUN` and the Scrape Health tab. |
| `HTTP 401/403`, session invalid | Delete `runtime/sessions/*.storage.json` and log in again. |
| Login challenge loop | `IG_HEADLESS=false`, solve once by hand; the session is saved. |
| Dashboard shows no data | Mismatched `IG_DATA_DIR` — the CLI and Streamlit must resolve the same one. |
| `database is locked` | Concurrent SQLite writers. WAL is enabled; for real concurrency use Postgres. |

---

## License

MIT — see [LICENSE](LICENSE).
