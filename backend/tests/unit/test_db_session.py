"""Unit tests for app.db.session — caching factories, dep shape, rollback path."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "test-token")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


def test_get_engine_returns_same_instance_when_cached(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_required_env(monkeypatch)
    from app.db.session import get_engine

    e1 = get_engine()
    e2 = get_engine()
    assert e1 is e2


def test_get_sessionmaker_returns_same_instance_when_cached(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_required_env(monkeypatch)
    from app.db.session import get_sessionmaker

    s1 = get_sessionmaker()
    s2 = get_sessionmaker()
    assert s1 is s2


def test_get_engine_uses_database_url_from_settings(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://alice:pw@db.example.com/app")
    from app.db.session import get_engine

    engine = get_engine()
    # Engine.url is rendered without password by default — check host/db match.
    assert engine.url.host == "db.example.com"
    assert engine.url.database == "app"


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace get_sessionmaker so get_session yields a mock — no real DB needed."""
    fake = AsyncMock()
    fake.rollback = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__.return_value = fake
    cm.__aexit__.return_value = False  # do not suppress exceptions

    maker = MagicMock(return_value=cm)
    from app.db import session as session_module

    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: maker)
    return fake


async def _drain(gen: AsyncGenerator[Any, None]) -> Any:
    """Pull one value from an async generator and close it."""
    value = await gen.__anext__()
    await gen.aclose()
    return value


async def test_get_session_yields_a_session(fake_session: AsyncMock) -> None:
    from app.db.session import get_session

    session = await _drain(get_session())
    assert session is fake_session


async def test_get_session_rolls_back_on_exception(fake_session: AsyncMock) -> None:
    from app.db.session import get_session

    gen = get_session()
    await gen.__anext__()
    with pytest.raises(RuntimeError, match="boom"):
        await gen.athrow(RuntimeError("boom"))
    fake_session.rollback.assert_awaited_once()


async def test_get_session_does_not_roll_back_on_clean_exit(fake_session: AsyncMock) -> None:
    from app.db.session import get_session

    gen = get_session()
    await gen.__anext__()
    await gen.aclose()
    fake_session.rollback.assert_not_awaited()
