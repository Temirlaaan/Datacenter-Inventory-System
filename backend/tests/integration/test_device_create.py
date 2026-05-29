"""Integration tests for POST /api/v1/devices/ — Sprint 5 Task 2.

NetBox is faked with respx; Postgres is real. Confirms the full create path
lands the right audit row in the DB and produces the right HTTP response,
including Correction 2's NetBox-4xx-to-422 translation.
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

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_SUB = "11111111-1111-1111-1111-111111111111"
_CREATE_PATH = "/api/dcim/devices/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_NEW_VERSION = "2026-05-28T10:00:00.000000Z"


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
    structlog.contextvars.clear_contextvars()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE audit_log"))
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
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-mobile-user",),
        session_id=None,
    )


def _create_body() -> dict[str, Any]:
    return {
        "device_type_id": 11,
        "role_id": 31,
        "site_id": 1,
        "status": "active",
        "name": "sw-99",
    }


def _created_dict() -> dict[str, Any]:
    """A NetBox device POST response — every key to_device_data reads."""
    return {
        "id": 99,
        "name": "sw-99",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": None,
        "position": None,
        "serial": "",
        "comments": "",
        "custom_fields": {"asset_tag": None},
        "last_updated": _NEW_VERSION,
    }


def _netbox_base() -> str:
    return str(get_settings().netbox_url).rstrip("/")


async def test_create_device_happy_path_persists_success_audit_row(
    client: httpx.AsyncClient,
) -> None:
    """Real Postgres + respx happy path: POST → 201, audit row landed with
    entity_id derived from the created device's id, before_json={},
    after_json carrying the created object."""
    _as_mobile_user()
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{base}{_CREATE_PATH}").respond(status_code=201, json=_created_dict())
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        resp = await client.post("/api/v1/devices/", json=_create_body())

    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["id"] == 99
    assert body["data"]["name"] == "sw-99"
    assert body["version"] == _NEW_VERSION

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT result::text, operation, entity_type, entity_id,"
                    " before_json, after_json FROM audit_log"
                )
            )
        ).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.result == "success"
    assert row.operation == "device.create"
    assert row.entity_type == "device"
    assert row.entity_id == "99"  # derived from created["id"] per post_with_attribution
    assert row.before_json == {}
    assert row.after_json["object"]["id"] == 99


async def test_create_device_netbox_validation_error_returns_422_and_failure_audit(
    client: httpx.AsyncClient,
) -> None:
    """Correction 2 end-to-end: NetBox 400 → structured 422 response AND
    a FAILURE audit row lands (with entity_id="unknown" since the create
    never produced an id)."""
    _as_mobile_user()
    base = _netbox_base()
    netbox_body = {"name": ["device with this name already exists."]}

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{base}{_CREATE_PATH}").respond(status_code=400, json=netbox_body)

        resp = await client.post("/api/v1/devices/", json=_create_body())

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text("SELECT result::text, operation, entity_id, after_json FROM audit_log")
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].result == "failure"
    assert rows[0].operation == "device.create"
    # The placeholder when entity_id is unknown (caller passed None, POST failed)
    assert rows[0].entity_id == "unknown"
    # The attempted payload is echoed in after_json for forensic debugging
    assert rows[0].after_json["payload"]["name"] == "sw-99"
