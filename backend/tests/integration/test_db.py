"""Integration tests for app.db.session — requires a running Postgres."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.session import get_engine, get_sessionmaker

pytestmark = pytest.mark.integration


async def test_async_session_can_select_one() -> None:
    """Round-trip a SELECT 1 through the async session — proves the engine + driver work."""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    async with get_sessionmaker()() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


def test_alembic_upgrade_head_succeeds() -> None:
    """Smoke test: `alembic upgrade head` runs cleanly against the test DB.

    Subprocess (not Alembic's Python API) because the container entrypoint
    in Sprint 1 Task 7 will shell out the same way — same code path.
    """
    backend_dir = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=backend_dir,
        timeout=30,
    )
    assert (
        result.returncode == 0
    ), f"alembic upgrade head failed: stdout={result.stdout!r} stderr={result.stderr!r}"
