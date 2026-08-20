"""Database engine, session factory and the FastAPI session dependency.

The engine is owned by a `Database` instance created during application startup and stored
on `app.state`, rather than a module-level global. That keeps tests free to build an engine
against a throwaway database without monkey-patching module state.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

logger = logging.getLogger(__name__)


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = 5,
        connect_timeout_seconds: int = 10,
    ) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            # A connect attempt to an unreachable host does not always get a prompt refusal;
            # without this the first request would hang rather than fail.
            connect_args={"connect_timeout": connect_timeout_seconds},
            # Managed Postgres (Neon/Render) drops idle connections; without pre-ping the
            # first request after an idle period fails with a stale-connection error.
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=pool_size * 2,
            pool_recycle=1800,
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        return self._sessionmaker()

    async def ping(self) -> None:
        """Raise if the database is unreachable."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self._engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session.

    Commits are *not* automatic: booking and leave-cancellation depend on precise
    transaction boundaries, so services open and commit their own transactions explicitly.
    This dependency only guarantees the session is rolled back and closed.
    """
    database: Database = request.app.state.database
    session = database.session()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
