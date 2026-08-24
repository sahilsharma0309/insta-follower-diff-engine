"""
Shared pytest fixtures.

Every test gets its own SQLite file inside `tmp_path` — a real file rather than
`:memory:`, because the engine hands out multiple connections and each one would
otherwise see a separate, empty in-memory database.
"""

from __future__ import annotations

import pytest

import config as config_module
import db as db_module
from models import Target
from scraper import FetchResult, FollowerRecord


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """A `Settings` pointing at a throwaway database, with caches reset."""
    monkeypatch.setenv("IG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IG_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("IG_SCRAPER_BACKEND", "demo")
    monkeypatch.setenv("IG_TARGETS", "demo_page")
    # Keep the tests instant: no politeness delays against a fake backend.
    monkeypatch.setenv("IG_MIN_REQUEST_DELAY", "0")
    monkeypatch.setenv("IG_MAX_REQUEST_DELAY", "0")

    resolved = config_module.get_settings(refresh=True)
    db_module.reset_engine()
    db_module.bootstrap(resolved)
    yield resolved
    db_module.reset_engine()
    config_module.get_settings(refresh=True)


@pytest.fixture
def session(settings):
    """An open session bound to the throwaway database."""
    factory = db_module.get_session_factory(settings)
    sess = factory()
    try:
        yield sess
        sess.commit()
    finally:
        sess.close()


@pytest.fixture
def target(session):
    row = Target(username="demo_page", full_name="Demo Page")
    session.add(row)
    session.flush()
    return row


def make_fetch(usernames, *, complete=True, reported=None, **kwargs) -> FetchResult:
    """Build a FetchResult from bare usernames — the tests' main input."""
    return FetchResult(
        records=[FollowerRecord(username=name) for name in usernames],
        complete=complete,
        reported_follower_count=reported,
        **kwargs,
    )


@pytest.fixture
def fetch_factory():
    return make_fetch
