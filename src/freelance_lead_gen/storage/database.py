"""SQLAlchemy async engine and session management.

Provides a :func:`get_session` context manager for async database access
and lifecycle helpers (:func:`init_db`, :func:`close_db`) that handle
engine creation, disposal, and pragma configuration (WAL mode, foreign keys).
"""

from __future__ import annotations as _annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from freelance_lead_gen.config.settings import get_settings

# ── Singleton engine (module-level) ──────────────────────────────────────────

_engine: AsyncEngine | None = None
"""Module-level async engine.  Managed by :func:`init_db` / :func:`close_db`."""

_session_factory: async_sessionmaker[AsyncSession] | None = None
"""Module-level session factory.  Created alongside the engine."""


# ── Declarative Base ─────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models.

    Every ORM model in the project should inherit from this class::

        from freelance_lead_gen.storage.database import Base

        class Opportunity(Base):
            __tablename__ = "opportunities"
            ...
    """

    __abstract__ = True

    # Default repr for all models (overridable per-model).
    def __repr__(self) -> str:
        cols = ", ".join(
            f"{c.name}={getattr(self, c.name, '<?>')!r}"
            for c in self.__table__.columns  # type: ignore[union-attr]
        )
        return f"{self.__class__.__name__}({cols})"


# ── Engine lifecycle ─────────────────────────────────────────────────────────


async def init_db() -> AsyncEngine:
    """Initialise the database engine and session factory.

    This function:
    1. Creates the async SQLAlchemy engine (with aiosqlite).
    2. Configures connection-pool listeners for WAL mode + foreign keys.
    3. Creates the async session factory.
    4. Stores both at module level.

    Call once at application startup::

        await init_db()

    Returns
    -------
    AsyncEngine
        The initialised engine instance.
    """
    global _engine, _session_factory  # noqa: PLW0603

    settings = get_settings()

    _engine = create_async_engine(
        settings.database.database_url,
        echo=settings.database.echo,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.pool_overflow,
        connect_args={"check_same_thread": False},
    )

    # Attach listeners that run on each connection checkout.
    event.listen(_engine.sync_engine, "connect", _on_connect)

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    # Verify the engine works.
    async with _engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    logger = __import__("structlog").get_logger(__name__)
    logger.info(
        "database.initialised",
        url=str(settings.database.database_url).replace(".db", "/*.db"),
        echo=settings.database.echo,
    )

    return _engine


async def close_db() -> None:
    """Dispose of the database engine and release resources.

    Call at application shutdown::

        await close_db()
    """
    global _engine, _session_factory  # noqa: PLW0603

    if _engine is not None:
        await _engine.dispose()
        logger = __import__("structlog").get_logger(__name__)
        logger.info("database.disposed")

    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    """Return the current engine, raising if not yet initialised."""
    if _engine is None:
        msg = "Database engine not initialised — call init_db() first"
        raise RuntimeError(msg)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the current session factory, raising if not yet initialised."""
    if _session_factory is None:
        msg = "Session factory not initialised — call init_db() first"
        raise RuntimeError(msg)
    return _session_factory


# ── Session context manager ──────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide an async database session as a context manager.

    The session is automatically committed on success and rolled back on
    exception.  Use with ``async with``::

        async with get_session() as session:
            result = await session.execute(...)
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ── Connection pragmas ───────────────────────────────────────────────────────


def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ANN401
    """Configure SQLite pragmas on each new connection.

    - Enables WAL mode for concurrent read/write performance.
    - Enforces foreign key constraints.
    - Sets a busy timeout so concurrent access does not fail immediately.

    This runs for every checkout from the connection pool.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA cache_size=-8000;")  # 8 MB cache
        cursor.execute("PRAGMA temp_store=MEMORY;")
    finally:
        cursor.close()
