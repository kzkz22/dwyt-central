"""Database engines and sessions.

user_session(user_id): the app role, with `dwyt.user_id` set for this transaction only, so
row-level security limits every query to that user's rows. A query without the setting
fails instead of returning everyone's data.

system_session(): the table owner (not subject to row-level security). Only for sign-in,
the runner protocol, the shared result store and admin pages.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def app_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)


@lru_cache(maxsize=1)
def system_engine() -> Engine:
    return create_engine(get_settings().database_system_url, pool_pre_ping=True, pool_size=5, max_overflow=10)


@contextmanager
def _session(engine: Engine, user_id: int | None = None) -> Iterator[Session]:
    s = Session(engine, expire_on_commit=False)
    try:
        if user_id is not None:
            s.execute(text("SELECT set_config('dwyt.user_id', :u, true)"), {"u": str(int(user_id))})
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def user_session(user_id: int):
    """A transaction as this user (row-level security applies)."""
    return _session(app_engine(), user_id)


def system_session():
    """A transaction as the table owner (no row-level security): use sparingly."""
    return _session(system_engine())


def reset() -> None:
    """Forget the engines (tests switch databases)."""
    for fn in (app_engine, system_engine):
        if fn.cache_info().currsize:
            fn().dispose()
        fn.cache_clear()
