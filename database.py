"""
database.py — Database engine, session factory, and helper utilities.

Provides:
    init_db()          — create all tables (idempotent)
    get_session()      — context manager yielding a scoped Session
    get_async_session()— async context manager for use inside Discord cogs
    engine             — the SQLAlchemy Engine (sync)
    SessionLocal       — sessionmaker bound to engine
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator, Generator

from loguru import logger
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from models import Base


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _sync_url(url: str) -> str:
    """
    Return a synchronous database URL.

    Converts async driver prefixes back to their sync equivalents so the
    same DATABASE_URL env var works for both sync and async engines.

        postgresql+asyncpg://... → postgresql+psycopg2://...
        sqlite+aiosqlite://...  → sqlite://...
    """
    url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    url = url.replace("sqlite+aiosqlite://", "sqlite:///")
    return url


def _async_url(url: str) -> str:
    """
    Return an async-compatible database URL.

        sqlite:///...           → sqlite+aiosqlite:///...
        postgresql://...        → postgresql+asyncpg://...
        postgresql+psycopg2://  → postgresql+asyncpg://...
    """
    if url.startswith("sqlite:///") and "aiosqlite" not in url:
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    return url


# ---------------------------------------------------------------------------
# Read configuration
# ---------------------------------------------------------------------------

_DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/capitol_gains.db")

SYNC_DATABASE_URL: str = _sync_url(_DATABASE_URL)
ASYNC_DATABASE_URL: str = _async_url(_DATABASE_URL)

_is_sqlite: bool = SYNC_DATABASE_URL.startswith("sqlite")

# ---------------------------------------------------------------------------
# Sync engine & session
# ---------------------------------------------------------------------------

_engine_kwargs: dict = {
    "echo": os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG",
}

if _is_sqlite:
    # SQLite requires check_same_thread=False when used across threads
    # (APScheduler jobs run in thread-pool workers).
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    _engine_kwargs["pool_pre_ping"] = True
else:
    # Connection pool tuning for Postgres
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(SYNC_DATABASE_URL, **_engine_kwargs)

# Enable WAL mode for SQLite so reads don't block writes
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# ---------------------------------------------------------------------------
# Async engine & session
# ---------------------------------------------------------------------------

_async_engine_kwargs: dict = {
    "echo": os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG",
}

if not _is_sqlite:
    _async_engine_kwargs["pool_size"] = 5
    _async_engine_kwargs["max_overflow"] = 10
    _async_engine_kwargs["pool_pre_ping"] = True

async_engine = create_async_engine(ASYNC_DATABASE_URL, **_async_engine_kwargs)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Create all tables defined in models.py (idempotent — safe to call on
    every startup).  Does NOT run Alembic migrations; use Alembic for
    schema changes in production.
    """
    logger.info("Initialising database at {}", SYNC_DATABASE_URL)
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialised — all tables present.")


async def async_init_db() -> None:
    """Async variant of init_db() for use in async startup hooks."""
    logger.info("Async-initialising database at {}", ASYNC_DATABASE_URL)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database async-initialised — all tables present.")


# ---------------------------------------------------------------------------
# Sync session context manager
# ---------------------------------------------------------------------------

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Yield a transactional database session.

    Usage::

        with get_session() as session:
            session.add(trade)
            # commits automatically on exit; rolls back on exception

    The session is committed on clean exit and rolled back on any
    exception, then always closed.
    """
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Async session context manager
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an async transactional database session.

    Usage::

        async with get_async_session() as session:
            result = await session.execute(select(Trade))
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_db_connection() -> bool:
    """
    Return True if the database is reachable, False otherwise.
    Used by the bot startup routine and health-check endpoints.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.debug("Database connection OK.")
        return True
    except Exception as exc:
        logger.error("Database connection failed: {}", exc)
        return False


async def async_check_db_connection() -> bool:
    """Async variant of check_db_connection()."""
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.debug("Async database connection OK.")
        return True
    except Exception as exc:
        logger.error("Async database connection failed: {}", exc)
        return False


# ---------------------------------------------------------------------------
# Convenience: dispose engines on shutdown
# ---------------------------------------------------------------------------

def dispose_engines() -> None:
    """
    Close all pooled connections.  Call this in the bot's on_close handler
    or in a finally block to ensure clean shutdown.
    """
    engine.dispose()
    logger.info("Sync database engine disposed.")


async def async_dispose_engines() -> None:
    """Async variant — disposes the async engine connection pool."""
    await async_engine.dispose()
    logger.info("Async database engine disposed.")
