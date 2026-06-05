"""Integration tests for PATCH /api/v1/devices/{id} — endpoint + service + Postgres.

NetBox is faked with respx; Postgres is real. Confirms the full update path
lands the right audit row in the DB and produces the right HTTP response.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import structlog
from sqlalchemy import text

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_SUB = "11111111-1111-1111-1111-111111111111"
_DEVICE_PATH = "/api/dcim/devices/5/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_VERSION = "2026-05-18T10:00:00.000000Z"
_NEW_VERSION = "2026-05-18T11:30:00.000000Z"


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
    # Clear get_settings too — a prior unit test (e.g. test_client.py) may have
    # cached Settings with a junk DATABASE_URL.
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    structlog.contextvars.clear_contextvars()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE audit_log, shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _as_mobile_user() -> None:
    """Override `get_current_user` with a mobile-role user for the next request."""
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-mobile-user",),
        session_id=None,
    )


def _device(version: str = _VERSION, **overrides: Any) -> dict[str, Any]:
    device = {
        "id": 5,
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


async def test_patch_device_writes_success_audit_row(client: httpx.AsyncClient) -> None:
    _as_mobile_user()
    base = _netbox_base()
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device())
        router.patch(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION, name="sw-01-new"))
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        resp = await client.patch(
            "/api/v1/devices/5",
            json={"name": "sw-01-new"},
            headers={"If-Unmodified-Since": _VERSION},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["name"] == "sw-01-new"
    assert body["version"] == _NEW_VERSION

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text("SELECT result::text, operation, entity_type, entity_id FROM audit_log")
            )
        ).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.result == "success"
    assert row.operation == "device.update"
    assert row.entity_type == "device"
    assert row.entity_id == "5"


async def test_patch_device_returns_409_and_writes_conflict_audit_row(
    client: httpx.AsyncClient,
) -> None:
    _as_mobile_user()
    base = _netbox_base()
    with respx.mock(assert_all_called=True) as router:
        # Re-read returns a newer version — no PATCH route registered, so any
        # erroneous PATCH would fail respx's assert_all_called check.
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))

        resp = await client.patch(
            "/api/v1/devices/5",
            json={"name": "sw-01-new"},
            headers={"If-Unmodified-Since": _VERSION},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION
    assert body["error"]["current_state"]["id"] == 5

    async with get_sessionmaker()() as session:
        rows = (await session.execute(text("SELECT result::text, operation FROM audit_log"))).all()
    assert len(rows) == 1
    assert rows[0].result == "conflict"
    assert rows[0].operation == "device.update"


async def test_patch_device_returns_422_when_netbox_rejects_with_400(
    client: httpx.AsyncClient,
) -> None:
    """Sprint 7 Task 5: end-to-end NetBox 400 → structured 422 with the
    NetBox body surfaced in ``netbox_detail``. Audit row records FAILURE."""
    _as_mobile_user()
    base = _netbox_base()
    netbox_body = {"status": ["Invalid value for status."]}
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device())
        router.patch(f"{base}{_DEVICE_PATH}").respond(status_code=400, json=netbox_body)

        resp = await client.patch(
            "/api/v1/devices/5",
            json={"name": "sw-01-new"},
            headers={"If-Unmodified-Since": _VERSION},
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body

    async with get_sessionmaker()() as session:
        rows = (await session.execute(text("SELECT result::text, operation FROM audit_log"))).all()
    assert len(rows) == 1
    assert rows[0].result == "failure"
    assert rows[0].operation == "device.update"
