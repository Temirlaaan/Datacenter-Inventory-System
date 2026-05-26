"""Fixtures for the QR registry endpoint tests.

These tests run against a real test Postgres, so they carry the ``integration``
marker and the same env-var skip-gate as ``tests/integration``.

Two test styles share these fixtures:
- Handler-call tests ``await`` the endpoint functions directly on the test's
  own event loop (``session`` fixture). This exercises every handler line and
  coverage traces it reliably.
- Integration tests drive the full ASGI stack via ``httpx.AsyncClient``
  (``client`` fixture) to prove routing, auth gating, and validation wiring.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.netbox.client import get_netbox_client

_BACKEND_DIR = Path(__file__).resolve().parents[4]
_REQUIRED_ENV = ("NETBOX_URL", "NETBOX_SERVICE_TOKEN", "KEYCLOAK_BASE_URL", "DATABASE_URL")
_USER_KEYCLOAK_ID = "11111111-1111-1111-1111-111111111111"


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
    """Skip the module unless the DB env is present; otherwise round-trip the schema."""
    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    if missing:
        pytest.skip(f"integration env vars missing: {', '.join(missing)}")
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


async def _reset_netbox_client() -> None:
    """Close and clear the process-wide NetBox client.

    The lru-cached singleton's httpx.AsyncClient binds to the first event loop
    that uses it. Without this reset, a test that touches ``get_netbox_client``
    (e.g. ``test_devices.py::test_get_device_service_builds_a_device_service``)
    leaks the client to the next test — and the root ``clean_env`` fixture
    then drains it via ``asyncio.run(...)``, which leaves no current event
    loop and breaks pytest-asyncio's loop setup for the following async test.
    """
    if get_netbox_client.cache_info().currsize > 0:
        # Suppress in case the client was created on a now-closed loop —
        # mirrors the root `clean_env`'s defensive aclose.
        with contextlib.suppress(Exception):
            await get_netbox_client().aclose()
        get_netbox_client.cache_clear()


@pytest.fixture(autouse=True)
async def _truncate() -> AsyncGenerator[None, None]:
    # Clear get_settings too — a prior unit test (e.g. test_lifecycle.py) may
    # have cached Settings with a junk DATABASE_URL via monkeypatched env vars.
    # Same call as tests/integration/test_devices.py's conftest.
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    await _reset_netbox_client()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(
            text("TRUNCATE qr_codes, qr_batches, audit_log, idempotency_keys CASCADE")
        )
        await session.commit()
    await get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    await _reset_netbox_client()


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """A fresh AsyncSession for handler-call tests."""
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client bound to the app — for full-stack integration tests."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def make_user(*roles: str, email: str | None = "alice@example.com") -> AuthUser:
    """Build a canned AuthUser for handler-call tests."""
    return AuthUser(sub=_USER_KEYCLOAK_ID, email=email, roles=tuple(roles), session_id=None)


@pytest.fixture
def as_user() -> Callable[..., AuthUser]:
    """Override get_current_user with a canned AuthUser — for integration tests."""

    def _set(*roles: str, email: str | None = "alice@example.com") -> AuthUser:
        user = make_user(*roles, email=email)
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    return _set
