"""Unit tests for /health: per-check helpers in isolation, plus the aggregated route."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

NETBOX_URL = "https://netbox.example.com"
KEYCLOAK_BASE_URL = "https://sso.example.com"
JWKS_URL = f"{KEYCLOAK_BASE_URL}/realms/prod-v1/protocol/openid-connect/certs"


@pytest.fixture
def health_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", KEYCLOAK_BASE_URL)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test"
    )


# ---------- _check_db ----------


async def test_check_db_returns_ok_when_select_one_succeeds(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock get_session so this test doesn't require a real Postgres."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock()

    @asynccontextmanager
    async def fake_session_cm() -> AsyncIterator[AsyncMock]:
        yield fake_session

    from app.api.v1 import health as health_mod

    monkeypatch.setattr(health_mod, "_open_session", fake_session_cm)
    result = await health_mod._check_db()
    assert result == {"status": "ok"}


async def test_check_db_returns_unreachable_on_oserror(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def failing_session_cm() -> AsyncIterator[None]:
        raise OSError("connection refused")
        yield  # pragma: no cover — unreachable, satisfies generator contract

    from app.api.v1 import health as health_mod

    monkeypatch.setattr(health_mod, "_open_session", failing_session_cm)
    result = await health_mod._check_db()
    assert result == {"status": "unreachable", "detail": "connection_error"}


# ---------- _check_netbox ----------


async def test_check_netbox_returns_ok_on_200(clean_env: None, health_env: None) -> None:
    from app.api.v1.health import _check_netbox

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{NETBOX_URL}/api/status/").respond(json={"netbox-version": "4.1.0"})
        result = await _check_netbox()

    assert result["status"] == "ok"


async def test_check_netbox_returns_unhealthy_on_500(clean_env: None, health_env: None) -> None:
    from app.api.v1.health import _check_netbox

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{NETBOX_URL}/api/status/").respond(status_code=500)
        result = await _check_netbox()

    assert result == {"status": "unhealthy", "detail": "http_500"}


async def test_check_netbox_returns_timeout_category(clean_env: None, health_env: None) -> None:
    """Detail must be a generic category, not a raw exception message — leak hygiene."""
    from app.api.v1.health import _check_netbox

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{NETBOX_URL}/api/status/").mock(side_effect=httpx.ConnectTimeout("slow"))
        result = await _check_netbox()

    assert result == {"status": "unreachable", "detail": "timeout"}


def test_categorize_http_error_covers_known_subtypes() -> None:
    """One direct call per branch; cheaper than spinning respx for each."""
    from app.api.v1.health import _categorize_http_error

    assert _categorize_http_error(httpx.ReadTimeout("x")) == "timeout"
    assert _categorize_http_error(httpx.ConnectError("x")) == "connection_error"
    # ReadError is a NetworkError sibling of ConnectError — exercises the NetworkError
    # branch without overlapping with the more specific Connect/Timeout cases above.
    assert _categorize_http_error(httpx.ReadError("x")) == "network_error"
    # ProtocolError lives under TransportError but is NOT a NetworkError — falls through.
    assert _categorize_http_error(httpx.RemoteProtocolError("x")) == "transport_error"


async def test_check_netbox_returns_unreachable_on_connect_error(
    clean_env: None, health_env: None
) -> None:
    from app.api.v1.health import _check_netbox

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{NETBOX_URL}/api/status/").mock(side_effect=httpx.ConnectError("no route"))
        result = await _check_netbox()

    assert result == {"status": "unreachable", "detail": "connection_error"}


# ---------- _check_keycloak ----------


async def test_check_keycloak_returns_ok_when_jwks_returns_200(
    clean_env: None, health_env: None
) -> None:
    from app.api.v1.health import _check_keycloak

    with respx.mock(assert_all_called=True) as router:
        router.get(JWKS_URL).respond(json={"keys": []})
        result = await _check_keycloak()

    assert result["status"] == "ok"


async def test_check_keycloak_returns_unhealthy_on_500(clean_env: None, health_env: None) -> None:
    from app.api.v1.health import _check_keycloak

    with respx.mock(assert_all_called=True) as router:
        router.get(JWKS_URL).respond(status_code=500)
        result = await _check_keycloak()

    assert result == {"status": "unhealthy", "detail": "http_500"}


async def test_check_keycloak_returns_unreachable_on_connect_error(
    clean_env: None, health_env: None
) -> None:
    from app.api.v1.health import _check_keycloak

    with respx.mock(assert_all_called=True) as router:
        router.get(JWKS_URL).mock(side_effect=httpx.ConnectError("no route"))
        result = await _check_keycloak()

    assert result == {"status": "unreachable", "detail": "connection_error"}


# ---------- /health route ----------


def _stub_checks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db: dict[str, str] | None = None,
    netbox: dict[str, str] | None = None,
    keycloak: dict[str, str] | None = None,
) -> None:
    from app.api.v1 import health as health_mod

    async def fake_db() -> dict[str, str]:
        return db or {"status": "ok"}

    async def fake_netbox() -> dict[str, str]:
        return netbox or {"status": "ok"}

    async def fake_keycloak() -> dict[str, str]:
        return keycloak or {"status": "ok"}

    monkeypatch.setattr(health_mod, "_check_db", fake_db)
    monkeypatch.setattr(health_mod, "_check_netbox", fake_netbox)
    monkeypatch.setattr(health_mod, "_check_keycloak", fake_keycloak)


def test_health_returns_200_when_all_checks_pass(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_checks(monkeypatch)
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"]["status"] == "ok"
    assert body["checks"]["netbox"]["status"] == "ok"
    assert body["checks"]["keycloak"]["status"] == "ok"


def test_health_returns_503_when_db_down(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_checks(monkeypatch, db={"status": "unreachable", "detail": "db gone"})
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"]["status"] == "unreachable"


def test_health_returns_503_when_netbox_unreachable(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_checks(monkeypatch, netbox={"status": "unreachable", "detail": "no route"})
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["checks"]["netbox"]["status"] == "unreachable"


def test_health_returns_503_when_keycloak_down(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_checks(monkeypatch, keycloak={"status": "unhealthy", "detail": "500"})
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["checks"]["keycloak"]["status"] == "unhealthy"


def test_health_does_not_require_auth(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe must succeed with no Authorization header — orchestrators don't carry tokens."""
    _stub_checks(monkeypatch)
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/health")  # no headers

    assert resp.status_code == 200


def test_health_completes_within_budget_when_one_check_hangs(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: total endpoint latency ≤ 3s even when one downstream stalls."""
    from app.api.v1 import health as health_mod

    async def hanging_check() -> dict[str, str]:
        await asyncio.sleep(60)  # would exceed budget if not for per-check timeout
        return {"status": "ok"}

    async def fast_ok() -> dict[str, str]:
        return {"status": "ok"}

    monkeypatch.setattr(health_mod, "_check_db", fast_ok)
    monkeypatch.setattr(health_mod, "_check_netbox", hanging_check)
    monkeypatch.setattr(health_mod, "_check_keycloak", fast_ok)

    from app.main import app

    start = time.monotonic()
    with TestClient(app) as client:
        resp = client.get("/health")
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"health took {elapsed:.2f}s, must be < 3s"
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["netbox"]["status"] == "timeout"
    assert body["checks"]["db"]["status"] == "ok"
    assert body["checks"]["keycloak"]["status"] == "ok"


# ---------- auto_end_job sub-object (Sprint 7 Task 1) ------------------------


def _disable_auto_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent the lifespan from scheduling the background task in tests.

    The /health sub-object reads ``app.state.auto_end_job_status``, which is
    populated unconditionally by the lifespan. Disabling the task keeps each
    test deterministic — we mutate the status object directly below.
    """
    monkeypatch.setenv("SHIFT_AUTO_END_ENABLED", "false")


def test_health_includes_auto_end_job_sub_object_with_enabled_field(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_checks(monkeypatch)
    _disable_auto_end(monkeypatch)
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert "auto_end_job" in body
    assert body["auto_end_job"] == {
        "enabled": False,
        "last_iteration_at": None,
        "status": "healthy",
    }


def test_health_auto_end_job_reports_stale_when_last_iteration_at_is_old(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.auto_end_job import AutoEndJobStatus

    _stub_checks(monkeypatch)
    _disable_auto_end(monkeypatch)
    monkeypatch.setenv("SHIFT_AUTO_END_INTERVAL_SECONDS", "300")
    from app.main import app

    with TestClient(app) as client:
        # Mutate the status after the lifespan attached it. Mock the loop
        # having last run 16 minutes ago — older than 3 * 300s = 900s.
        app.state.auto_end_job_status = AutoEndJobStatus(
            enabled=True,
            last_iteration_at=datetime.now(UTC) - timedelta(minutes=16),
        )
        resp = client.get("/health")

    assert resp.status_code == 200  # decision 1: stale job does NOT flip overall to 503
    body = resp.json()
    assert body["status"] == "ok"
    assert body["auto_end_job"]["enabled"] is True
    assert body["auto_end_job"]["status"] == "stale"
    assert body["auto_end_job"]["last_iteration_at"] is not None


def test_health_auto_end_job_reports_healthy_with_recent_iteration(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.auto_end_job import AutoEndJobStatus

    _stub_checks(monkeypatch)
    _disable_auto_end(monkeypatch)
    monkeypatch.setenv("SHIFT_AUTO_END_INTERVAL_SECONDS", "300")
    from app.main import app

    with TestClient(app) as client:
        # Last iteration 1 minute ago — well within 3 * 300s.
        app.state.auto_end_job_status = AutoEndJobStatus(
            enabled=True,
            last_iteration_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        resp = client.get("/health")

    body = resp.json()
    assert body["auto_end_job"]["status"] == "healthy"


def test_health_stale_auto_end_job_does_not_make_overall_status_degraded(
    clean_env: None, health_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 1: the auto_end_job sub-object is informational. A stale loop
    does NOT cause /health to return 503 or set status='degraded'. Operators
    alert on the sub-field separately."""
    from datetime import UTC, datetime, timedelta

    from app.services.auto_end_job import AutoEndJobStatus

    _stub_checks(monkeypatch)  # all downstreams ok
    _disable_auto_end(monkeypatch)
    monkeypatch.setenv("SHIFT_AUTO_END_INTERVAL_SECONDS", "300")
    from app.main import app

    with TestClient(app) as client:
        # Pathologically stale loop.
        app.state.auto_end_job_status = AutoEndJobStatus(
            enabled=True,
            last_iteration_at=datetime.now(UTC) - timedelta(days=7),
        )
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["auto_end_job"]["status"] == "stale"
