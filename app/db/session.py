"""Async SQLAlchemy engine + session factory + FastAPI dependency.

Lazy `lru_cache` factories so module import does not require Settings to already be
loaded. Tests can swap engines by clearing the caches via `clean_env`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Create the process-wide async engine on first call; cached thereafter."""
    settings = get_settings()
    return create_async_engine(
        str(settings.database_url),
        # pool_pre_ping issues a cheap SELECT 1 before checkout — prevents stale-
        # connection 500s after Postgres restarts. ~1 round-trip per checkout.
        pool_pre_ping=True,
        echo=False,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the cached engine."""
    return async_sessionmaker(
        get_engine(),
        # expire_on_commit=False is the standard async pattern: otherwise attribute
        # access after commit triggers a sync DB call, which crashes in async context.
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, rolls back on exception, always closes."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
