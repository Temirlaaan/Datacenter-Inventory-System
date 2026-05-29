"""Integration tests for POST /api/v1/devices/{id}/comments — Sprint 5 Task 3.

NetBox is faked with respx; Postgres is real. Confirms the journal-only POST
lands the right audit row and produces the right HTTP response.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

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
_DEVICE_ID = 5
_JOURNAL_PATH = "/api/extras/journal-entries/"


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


def _netbox_base() -> str:
    return str(get_settings().netbox_url).rstrip("/")


async def test_add_comment_happy_path_persists_success_audit_row(
    client: httpx.AsyncClient,
) -> None:
    """End-to-end: POST → 201 {"id": ...}, audit row landed with
    operation="device.add_comment", entity_id=str(device_id),
    after_json.object carrying the created journal entry."""
    _as_mobile_user()
    base = _netbox_base()
    journal_entry = {
        "id": 42,
        "assigned_object_id": _DEVICE_ID,
        "comments": "Replaced PSU 1",
    }

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json=journal_entry)

        resp = await client.post(
            f"/api/v1/devices/{_DEVICE_ID}/comments",
            json={"comment": "Replaced PSU 1"},
        )

    assert resp.status_code == 201
    assert resp.json() == {"id": 42}

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
    assert row.operation == "device.add_comment"
    assert row.entity_type == "device"
    # entity_id is the device_id (Task 3 plan — caller-provided to
    # post_with_attribution, not derived from the created journal id)
    assert row.entity_id == str(_DEVICE_ID)
    # before_json is empty (no pre-state for a POST)
    assert row.before_json == {}
    # after_json carries the created journal entry
    assert row.after_json["object"]["id"] == 42


async def test_add_comment_writes_no_secondary_journal_entry(
    client: httpx.AsyncClient,
) -> None:
    """The POST IS the journal entry (attach_journal=False) — only ONE
    netbox POST should fire. The respx assert_all_called=True flag means
    any unmatched journal POST would fail the test."""
    _as_mobile_user()
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        # Only one route — if post_with_attribution attempted a SECOND journal
        # post, respx would raise "unmocked request".
        route = router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 42})

        await client.post(
            f"/api/v1/devices/{_DEVICE_ID}/comments",
            json={"comment": "test"},
        )

    assert route.call_count == 1  # exactly one POST


async def test_add_comment_netbox_4xx_writes_failure_audit_row_with_device_entity_id(
    client: httpx.AsyncClient,
) -> None:
    """NetBox rejects with 400 → FAILURE audit row lands with entity_id=
    str(device_id) (caller-provided, not "unknown"); response 502 via
    global handler (no specialised 422 for add-comment per Task 3 plan)."""
    _as_mobile_user()
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{base}{_JOURNAL_PATH}").respond(
            status_code=400, json={"comments": ["bad request"]}
        )

        resp = await client.post(
            f"/api/v1/devices/{_DEVICE_ID}/comments",
            json={"comment": "test"},
        )

    # NetBoxValidationError IS-A NetBoxClientError → global handler → 502.
    # Add-comment doesn't have specialised 422 translation (Sprint 6 candidate).
    assert resp.status_code == 502

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text("SELECT result::text, operation, entity_id, after_json" " FROM audit_log")
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].result == "failure"
    assert rows[0].operation == "device.add_comment"
    # Caller-provided entity_id survives the FAILURE path (not "unknown")
    assert rows[0].entity_id == str(_DEVICE_ID)
