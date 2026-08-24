"""
db.py — one process-wide engine, and a session context manager.

Every entry point (CLI, scheduler, Streamlit app, tests) goes through
`session_scope()` so transaction handling lives in exactly one place:
commit on success, roll back on any exception, always close.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import Settings, get_settings
from models import create_db_engine, create_session_factory, init_db

log = logging.getLogger(__name__)

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None
_ENGINE_URL: str | None = None


def get_engine(settings: Settings | None = None, *, echo: bool = False) -> Engine:
    """Return the shared Engine, creating it on first use.

    The engine is rebuilt if the configured URL changes — which happens in the
    test suite, where each test points at its own in-memory database.
    """
    global _ENGINE, _SESSION_FACTORY, _ENGINE_URL
    settings = settings or get_settings()
    if _ENGINE is None or _ENGINE_URL != settings.database_url:
        settings.ensure_directories()
        _ENGINE = create_db_engine(settings.database_url, echo=echo)
        _SESSION_FACTORY = create_session_factory(_ENGINE)
        _ENGINE_URL = settings.database_url
        log.debug("Database engine created for %s", _ENGINE.url.render_as_string())
    return _ENGINE


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    get_engine(settings)
    assert _SESSION_FACTORY is not None  # set by get_engine
    return _SESSION_FACTORY


def bootstrap(settings: Settings | None = None) -> Engine:
    """Create the runtime directories and the schema. Idempotent."""
    settings = settings or get_settings()
    engine = get_engine(settings)
    init_db(engine)
    return engine


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional scope around a series of operations.

    Usage::

        with session_scope() as session:
            session.add(Target(username="example"))
    """
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop the cached engine. Used by tests and by `--database-url` overrides."""
    global _ENGINE, _SESSION_FACTORY, _ENGINE_URL
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None
    _ENGINE_URL = None


__all__ = [
    "bootstrap",
    "get_engine",
    "get_session_factory",
    "reset_engine",
    "session_scope",
]
