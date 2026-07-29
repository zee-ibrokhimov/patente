"""Engine and session construction.

SQLite in WAL mode is genuinely fine for thousands of users, but only with the
right pragmas, and they must be set per connection rather than once per database:

  journal_mode=WAL   readers do not block the writer, which is what makes a
                     single-file database workable under concurrent bot traffic
  foreign_keys=ON    SQLite ignores foreign keys unless asked. Without this the
                     ON DELETE CASCADE behind /delete silently does nothing.
  busy_timeout       wait for a competing writer instead of raising "database is
                     locked" at the caller
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.config import settings


def sync_url(url: str | None = None) -> str:
    """Async URL -> sync URL. Migrations and the content pipeline run sync."""
    return (url or settings.database_url).replace("+aiosqlite", "")


def _apply_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def register_pragmas(engine: Engine) -> Engine:
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _apply_pragmas)
    return engine


def make_sync_engine(url: str | None = None, **kw) -> Engine:
    return register_pragmas(create_engine(sync_url(url), **kw))


def make_async_engine(url: str | None = None, **kw):
    engine = create_async_engine(url or settings.database_url, **kw)
    register_pragmas(engine.sync_engine)
    return engine


# Lazily built so importing a model never opens a database file.
_async_engine = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_engine, _async_session_factory
    if _async_session_factory is None:
        _async_engine = make_async_engine()
        _async_session_factory = async_sessionmaker(_async_engine, expire_on_commit=False)
    return _async_session_factory


def sync_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(make_sync_engine(url), expire_on_commit=False)
