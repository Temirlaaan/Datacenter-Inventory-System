"""Integration tests for app.main.lifespan — auto-end job wiring (Sprint 7 Task 1).

Exercises the lifespan against a freshly constructed FastAPI app rather than
the module-level ``app`` singleton, so each test gets clean ``app.state``
without leaking the loop task across tests.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.config import Settings, get_settings
from app.main import lifespan
from app.services.auto_end_job import AutoEndJobStatus

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=_BACKEND_DIR,
        timeout=30,
    )
    assert (
        result.returncode == 0
    ), f"alembic {args!r} failed: stdout={result.stdout!r} stderr={result.stderr!r}"


@pytest.fixture(scope="module", autouse=True)
def _clean_schema() -> Generator[None, None, None]:
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


def _patched_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Override get_settings() to return Settings with the requested overrides."""

    base = {
        "netbox_url": "https://netbox.example.com",
        "netbox_service_token": "test-token",
        "keycloak_base_url": "https://sso.example.com",
        "database_url": "postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test",
        **overrides,
    }

    @lru_cache
    def _stub() -> Settings:
        return Settings(**base)  # type: ignore[arg-type]

    monkeypatch.setattr("app.main.get_settings", _stub)


async def test_lifespan_creates_auto_end_job_status_with_enabled_true_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    _patched_settings(monkeypatch)
    app = FastAPI()

    async with lifespan(app):
        assert isinstance(app.state.auto_end_job_status, AutoEndJobStatus)
        assert app.state.auto_end_job_status.enabled is True
        assert app.state.auto_end_job_task is not None
        # Background task is running (not yet completed).
        assert not app.state.auto_end_job_task.done()


async def test_lifespan_with_auto_end_disabled_skips_task_but_attaches_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    _patched_settings(monkeypatch, shift_auto_end_enabled=False)
    app = FastAPI()

    async with lifespan(app):
        # Status is always attached so /health has a consistent shape.
        assert isinstance(app.state.auto_end_job_status, AutoEndJobStatus)
        assert app.state.auto_end_job_status.enabled is False
        # But the task is NOT scheduled.
        assert app.state.auto_end_job_task is None


async def test_lifespan_shutdown_drains_auto_end_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    # Very small interval/grace so the loop won't be stuck mid-sleep for long.
    _patched_settings(
        monkeypatch,
        shift_auto_end_interval_seconds=1,
        shift_auto_end_threshold_hours=12,
    )
    app = FastAPI()

    async with lifespan(app):
        task = app.state.auto_end_job_task
        assert task is not None
        # Give the loop a moment to enter its grace-period sleep.
        await asyncio.sleep(0.05)

    # After the lifespan exits, the task is done (drained, not stuck).
    assert task.done()


async def test_lifespan_shutdown_cancels_auto_end_task_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the auto-end task refuses to drain within the shutdown timeout,
    the lifespan logs a warning and ``.cancel()`` the task — it MUST NOT
    block process shutdown forever.

    Forced by patching the shutdown timeout to zero so ``asyncio.wait_for``
    raises ``TimeoutError`` immediately, hitting the except branch.
    """
    get_settings.cache_clear()
    _patched_settings(
        monkeypatch,
        shift_auto_end_interval_seconds=1,
        shift_auto_end_threshold_hours=12,
    )
    # Force the shutdown drain to time out immediately.
    monkeypatch.setattr("app.main._AUTO_END_SHUTDOWN_TIMEOUT_SECONDS", 0.0)
    app = FastAPI()

    async with lifespan(app):
        task = app.state.auto_end_job_task
        assert task is not None
        # Don't wait for grace — exit immediately so wait_for(0.0) times out.

    # Task was cancelled by the timeout branch.
    assert task.cancelled() or task.done()
