"""End-to-end integration tests for rate-limit middleware (Sprint 8a Task 3).

Drives the full ASGI stack with a real JWT in the Authorization header so the
middleware extracts the ``sub`` claim and counts requests against the per-
user bucket. Endpoint logic is bypassed via dependency override; what matters
is whether the middleware returns 429 BEFORE the route handler runs.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import httpx
import pytest
from jose import jwt
from sqlalchemy import text

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.middleware.rate_limit import reset_rate_limit_buckets
from app.netbox.client import get_netbox_client
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_KEYCLOAK_ID = "11111111-1111-1111-1111-111111111111"
_SECRET = "test-secret-not-used-for-verification"


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


def _bearer_for(sub: str = _USER_KEYCLOAK_ID) -> str:
    """A valid-shape JWT with ``sub`` — the middleware only reads ``sub``."""
    token: str = jwt.encode({"sub": sub, "exp": 9999999999}, _SECRET, algorithm="HS256")
    return token


async def _reset_netbox_client() -> None:
    """Close + clear the cached NetBox client so a stale event-loop binding
    from a prior test doesn't leak. Same pattern as
    ``tests/unit/api/v1/conftest.py:_reset_netbox_client``."""
    if get_netbox_client.cache_info().currsize > 0:
        with contextlib.suppress(Exception):
            await get_netbox_client().aclose()
        get_netbox_client.cache_clear()


@pytest.fixture(autouse=True)
async def _setup() -> AsyncGenerator[None, None]:
    """Seed an active shift for the default user + reset buckets per test."""
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_rate_limit_buckets()
    await _reset_netbox_client()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_rate_limit_buckets()
    await _reset_netbox_client()


@pytest.fixture
async def client_with_mobile_user() -> AsyncGenerator[httpx.AsyncClient, None]:
    """AsyncClient that override get_current_user → mobile user (matches the
    JWT we mint below), and exits the rate-limit middleware path naturally."""
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


async def test_429_after_exhausting_read_budget(
    client_with_mobile_user: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hammer a READ endpoint past the budget → 429 + Retry-After + structured body."""
    monkeypatch.setenv("RATE_LIMIT_READ_PER_MINUTE", "3")
    get_settings.cache_clear()

    headers = {"Authorization": f"Bearer {_bearer_for()}"}

    # First 3 reads succeed (the endpoint may return a NetBox error, but the
    # rate-limit middleware lets them through).
    for _ in range(3):
        resp = await client_with_mobile_user.get("/api/v1/meta/sites", headers=headers)
        assert resp.status_code != 429, resp.text

    # 4th read → 429 from the middleware.
    resp = await client_with_mobile_user.get("/api/v1/meta/sites", headers=headers)
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["retry_after_seconds"] > 0
    # Retry-After header matches the body's retry_after_seconds.
    assert int(resp.headers["Retry-After"]) == body["error"]["retry_after_seconds"]


async def test_read_budget_separate_from_write_budget(
    client_with_mobile_user: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who maxed out READ can still WRITE (separate buckets)."""
    monkeypatch.setenv("RATE_LIMIT_READ_PER_MINUTE", "2")
    monkeypatch.setenv("RATE_LIMIT_WRITE_PER_MINUTE", "5")
    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {_bearer_for()}"}

    # Exhaust read budget.
    for _ in range(2):
        resp = await client_with_mobile_user.get("/api/v1/meta/sites", headers=headers)
        assert resp.status_code != 429

    # 3rd read → 429
    resp = await client_with_mobile_user.get("/api/v1/meta/sites", headers=headers)
    assert resp.status_code == 429

    # WRITE bucket is independent — POST /api/v1/sessions/start lands as WRITE.
    # We expect it to NOT be 429 (the rate-limit middleware lets it through).
    # The actual response may be 200/422/whatever — what matters is "not 429".
    resp = await client_with_mobile_user.post(
        "/api/v1/sessions/start", json={"tablet_id": "t-01"}, headers=headers
    )
    assert resp.status_code != 429, resp.text


async def test_rate_limit_disabled_lets_all_requests_through(
    client_with_mobile_user: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RATE_LIMIT_ENABLED=false short-circuits the middleware entirely."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_READ_PER_MINUTE", "1")
    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {_bearer_for()}"}

    # 10 reads back-to-back with a budget of 1 — but disabled → none 429.
    for _ in range(10):
        resp = await client_with_mobile_user.get("/api/v1/meta/sites", headers=headers)
        assert resp.status_code != 429


async def test_unauthenticated_requests_bypass_rate_limit_then_fail_at_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Authorization header → middleware lets the request through (no
    rate-limit key). The downstream auth dep returns 401."""
    monkeypatch.setenv("RATE_LIMIT_READ_PER_MINUTE", "1")
    get_settings.cache_clear()
    app.dependency_overrides.clear()  # let auth actually run

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # 5 unauthenticated reads — each gets 401/403 from auth, NOT 429.
        for _ in range(5):
            resp = await c.get("/api/v1/meta/sites")
            assert resp.status_code != 429
            # Auth rejects without a Bearer token.
            assert resp.status_code in (401, 403)


async def test_admin_path_classified_as_admin_consuming_separate_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /api/v1/admin/audit`` consumes the ADMIN bucket, not READ.
    Exhausting the READ budget must not affect the ADMIN budget."""
    monkeypatch.setenv("RATE_LIMIT_READ_PER_MINUTE", "1")
    monkeypatch.setenv("RATE_LIMIT_ADMIN_PER_MINUTE", "5")
    get_settings.cache_clear()

    admin = AuthUser(
        sub=_USER_KEYCLOAK_ID,
        email="admin@example.com",
        roles=("dcinv-admin",),
        session_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {_bearer_for()}"}

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # Exhaust READ via /api/v1/meta/sites (1 hit → bucket full).
            resp = await c.get("/api/v1/meta/sites", headers=headers)
            assert resp.status_code != 429
            # Next READ → 429
            resp = await c.get("/api/v1/meta/sites", headers=headers)
            assert resp.status_code == 429

            # ADMIN path: NOT rate-limited (separate bucket); 5 hits all
            # pass the rate-limit middleware. Note: /admin/audit requires
            # an active shift so the response may be 409 NO_ACTIVE_SHIFT —
            # what matters is it's not 429.
            for _ in range(3):
                resp = await c.get("/api/v1/admin/audit", headers=headers)
                assert resp.status_code != 429
    finally:
        app.dependency_overrides.clear()
