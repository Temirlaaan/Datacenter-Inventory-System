"""Integration test: _check_db against a real Postgres — covers the thin
session-opening wrapper that unit tests stub out."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_check_db_returns_ok_against_live_postgres() -> None:
    """Clear the engine/sessionmaker cache so we get a fresh engine bound to this
    test's event loop — see tests/integration/test_db.py for the same pattern."""
    from app.api.v1.health import _check_db
    from app.db.session import get_engine, get_sessionmaker

    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    result = await _check_db()
    assert result == {"status": "ok"}
