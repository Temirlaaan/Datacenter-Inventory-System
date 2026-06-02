"""End-to-end integration test for the NetBox circuit breaker (Sprint 8a Task 2).

Drives the full ASGI stack: hit a write endpoint enough times to trip the
circuit, then assert the next call returns 503 NETBOX_CIRCUIT_OPEN with the
structured body + Retry-After header — instead of the existing 502 path that
NetBoxClientError takes when the circuit is closed.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.netbox.client import get_netbox_client
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
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
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


@pytest.fixture(autouse=True)
async def _truncate_and_seed_shift(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None, None]:
    # Trip the circuit fast — 1 failure opens it.
    monkeypatch.setenv("NETBOX_CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("NETBOX_CIRCUIT_RECOVERY_TIMEOUT_SECONDS", "30")
    # Strip the client's retry backoff so tests don't sit on real wall time.
    from app.netbox import client as client_module

    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    # Reset circuit each test so failure counts don't leak.
    client_module.reset_netbox_circuit()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE audit_log, shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    client_module.reset_netbox_circuit()


@pytest.fixture
async def mobile_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    user = AuthUser(
        sub=_USER_KEYCLOAK_ID,
        email="alice@example.com",
        roles=("dcinv-mobile-user",),
        session_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_repeated_netbox_5xx_opens_circuit_and_subsequent_call_returns_503(
    mobile_client: httpx.AsyncClient,
) -> None:
    """End-to-end: trip the circuit via 5xx, then assert next call gets 503
    NETBOX_CIRCUIT_OPEN + structured body + Retry-After header (NOT 502)."""
    # Reset the cached NetBox client so it picks up the test settings.
    # Otherwise it might have been cached from another module.
    if get_netbox_client.cache_info().currsize > 0:
        await get_netbox_client().aclose()
        get_netbox_client.cache_clear()

    base = str(get_settings().netbox_url).rstrip("/")
    device_path = "/api/dcim/devices/5/"

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{base}{device_path}").respond(status_code=503, text="upstream down")

        # Call 1: NetBox returns 503 → retries exhaust → NetBoxServerError.
        # With FAILURE_THRESHOLD=1, this trips the circuit OPEN.
        first = await mobile_client.patch(
            "/api/v1/devices/5",
            json={"name": "sw-01-new"},
            headers={"If-Unmodified-Since": "2026-05-21T08:00:00.000000Z"},
        )
        assert first.status_code == 502  # Sprint 3's NetBoxClientError handler

        # Call 2: circuit is OPEN → fast-fail with 503 NETBOX_CIRCUIT_OPEN.
        second = await mobile_client.patch(
            "/api/v1/devices/5",
            json={"name": "sw-01-new"},
            headers={"If-Unmodified-Since": "2026-05-21T08:00:00.000000Z"},
        )

    assert second.status_code == 503
    body = second.json()
    assert body["error"]["code"] == "NETBOX_CIRCUIT_OPEN"
    assert body["error"]["retry_after_seconds"] == 30
    assert second.headers["Retry-After"] == "30"
