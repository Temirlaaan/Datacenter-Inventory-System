"""Sprint 6 Task 4 step (c/d): cross-cutting tests for the active-shift gate.

Two concerns covered here:

1. **Gate (decision G)** — each of the six write endpoints returns a structured
   ``409 NO_ACTIVE_SHIFT`` body when the JWT-identified user has no active
   shift. Asserted against the live ASGI app so dep registration, exception
   handler, and route wiring are all exercised together.

2. **End-to-end smoke (decision D, step d)** — a full request flow:
   ``POST /sessions/start`` (creates shift) → ``PATCH /devices/{id}`` (writes
   audit) → assert ``audit_log.session_id == shift_sessions.id`` →
   ``POST /sessions/end``. Pins the cross-component contract that
   ``audit_log.session_id`` now points to the shift's UUID, not the JWT sid.

These tests deliberately do NOT seed an active shift in the autouse fixture —
each test owns its shift state explicitly so the gate and the smoke flow can
each set up the world they need.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
import structlog
from sqlalchemy import text

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.netbox.client import get_netbox_client

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_SUB = "11111111-1111-1111-1111-111111111111"
_DEVICE_ID = 5
_DEVICE_PATH = f"/api/dcim/devices/{_DEVICE_ID}/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_VERSION = "2026-05-30T08:00:00.000000Z"
_NEW_VERSION = "2026-05-30T09:00:00.000000Z"


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
async def _truncate() -> AsyncGenerator[None, None]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    if get_netbox_client.cache_info().currsize > 0:
        get_netbox_client.cache_clear()
    structlog.contextvars.clear_contextvars()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "TRUNCATE qr_codes, qr_batches, audit_log,"
                " idempotency_keys, shift_sessions CASCADE"
            )
        )
        await session.commit()
    await get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    if get_netbox_client.cache_info().currsize > 0:
        get_netbox_client.cache_clear()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _as_user(*roles: str) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=tuple(roles),
        session_id=None,
    )


def _device(version: str = _VERSION, **overrides: Any) -> dict[str, Any]:
    device = {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "core switch",
        "asset_tag": "A-9",
        "custom_fields": {},
        "last_updated": version,
    }
    device.update(overrides)
    return device


def _netbox_base() -> str:
    return str(get_settings().netbox_url).rstrip("/")


# ---------- 409 NO_ACTIVE_SHIFT per write endpoint ----------


async def test_patch_device_returns_409_no_active_shift(client: httpx.AsyncClient) -> None:
    _as_user("dcinv-mobile-user")

    resp = await client.patch(
        f"/api/v1/devices/{_DEVICE_ID}",
        json={"name": "sw-01-new"},
        headers={"If-Unmodified-Since": _VERSION},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_post_create_device_returns_409_no_active_shift(client: httpx.AsyncClient) -> None:
    _as_user("dcinv-mobile-user")

    resp = await client.post(
        "/api/v1/devices/",
        json={
            "name": "sw-99",
            "device_type": 11,
            "role": 31,
            "site": 1,
            "status": "active",
        },
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_post_add_comment_returns_409_no_active_shift(client: httpx.AsyncClient) -> None:
    _as_user("dcinv-mobile-user")

    resp = await client.post(
        f"/api/v1/devices/{_DEVICE_ID}/comments",
        json={"comment": "spotted a loose cable"},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_post_decommission_returns_409_no_active_shift(client: httpx.AsyncClient) -> None:
    _as_user("dcinv-admin")

    resp = await client.post(
        f"/api/v1/devices/{_DEVICE_ID}/decommission",
        json={"version": _VERSION},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_post_qr_bind_returns_409_no_active_shift(client: httpx.AsyncClient) -> None:
    _as_user("dcinv-mobile-user")

    resp = await client.post(
        "/api/v1/qr/DCQR-ABCDEFGH/bind",
        json={"device_id": _DEVICE_ID, "version": _VERSION},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_post_qr_retire_returns_409_no_active_shift(client: httpx.AsyncClient) -> None:
    _as_user("dcinv-admin")

    resp = await client.post(
        "/api/v1/qr/DCQR-ABCDEFGH/retire",
        json={},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


# ---------- end-to-end smoke (step d) ----------


async def test_start_then_device_patch_then_end_records_shift_id_in_audit(
    client: httpx.AsyncClient,
) -> None:
    """Cross-component pin for decision D: a full request flow proves that
    ``audit_log.session_id`` carries the started shift's UUID, not the JWT
    sid.

    Flow:
      1. POST /sessions/start  → creates shift, returns its UUID
      2. PATCH /devices/{id}   → audit row written
      3. SELECT audit_log      → session_id == shift.id
      4. POST /sessions/end    → shift closed
    """
    _as_user("dcinv-mobile-user")
    base = _netbox_base()

    # 1) Start a shift.
    start_resp = await client.post("/api/v1/sessions/start", json={"tablet_id": "tablet-e2e"})
    assert start_resp.status_code == 200
    shift_id = UUID(start_resp.json()["session"]["id"])

    # 2) Drive a write that produces an audit row.
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device())
        router.patch(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION, name="sw-01-new"))
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        patch_resp = await client.patch(
            f"/api/v1/devices/{_DEVICE_ID}",
            json={"name": "sw-01-new"},
            headers={"If-Unmodified-Since": _VERSION},
        )
    assert patch_resp.status_code == 200

    # 3) Audit row must carry the shift's UUID, NOT the JWT sid (which is None
    # on this test user).
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT session_id, operation FROM audit_log ORDER BY id DESC LIMIT 1")
            )
        ).one()
    assert row.operation == "device.update"
    assert row.session_id == shift_id

    # 4) End the shift cleanly.
    end_resp = await client.post("/api/v1/sessions/end", json={"end_reason": "manual"})
    assert end_resp.status_code == 200
    assert end_resp.json()["session"]["end_reason"] == "manual"
