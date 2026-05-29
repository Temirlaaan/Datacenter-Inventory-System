"""Integration tests for NetBoxWriteService — audit rows landing in Postgres.

NetBox is faked with respx; Postgres is real. These confirm the ``audit_result``
enum binding and the JSONB ``before_json``/``after_json`` columns round-trip for
every outcome — which the respx-only unit tests cannot verify.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
import respx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.config import get_settings
from app.db.repositories import AuditLogRepository
from app.db.session import get_engine, get_sessionmaker
from app.netbox.client import NetBoxClient
from app.netbox.errors import NetBoxServerError
from app.services.netbox_write import NetBoxWriteService, WriteConflictError

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_SUB = "11111111-1111-1111-1111-111111111111"
_SESSION_ID = "22222222-2222-2222-2222-222222222222"
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
    # get_settings is cleared too: a prior unit test (e.g. test_client.py) may have
    # cached Settings with a junk DATABASE_URL, which would point the engine at an
    # unresolvable host. Clearing it here makes this file order-independent.
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
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip retry sleeps so the re-read-failure test isn't gated on real wall time."""
    from app.netbox import client as client_module

    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


def _user(session_id: str | None = None) -> AuthUser:
    return AuthUser(
        sub=_USER_SUB, email="alice@example.com", roles=("dcinv-admin",), session_id=session_id
    )


def _device(version: str = _VERSION, **overrides: Any) -> dict[str, Any]:
    device = {
        "id": 5,
        "name": "sw-01",
        "status": {"value": "active"},
        "last_updated": version,
    }
    device.update(overrides)
    return device


def _netbox_base() -> str:
    return str(get_settings().netbox_url).rstrip("/")


async def _patch(
    client: NetBoxClient,
    session: AsyncSession,
    *,
    expected_version: str = _VERSION,
    user: AuthUser | None = None,
) -> dict[str, Any]:
    service = NetBoxWriteService(client, session, AuditLogRepository(session))
    return await service.patch_with_attribution(
        netbox_path=_DEVICE_PATH,
        netbox_object_type="dcim.device",
        netbox_object_id=5,
        entity_type="device",
        operation="device.update",
        expected_version=expected_version,
        changes={"name": "sw-01-new"},
        user=user or _user(),
    )


async def test_patch_with_attribution_writes_success_audit_row() -> None:
    base = _netbox_base()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{base}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{base}{_DEVICE_PATH}").respond(
                json=_device(_NEW_VERSION, name="sw-01-new")
            )
            router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            async with get_sessionmaker()() as session:
                result = await _patch(client, session, user=_user(_SESSION_ID))

    assert result["name"] == "sw-01-new"
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT result::text, operation, entity_type, entity_id,"
                    " before_json, after_json, session_id::text FROM audit_log"
                )
            )
        ).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.result == "success"
    assert row.operation == "device.update"
    assert row.entity_type == "device"
    assert row.entity_id == "5"
    assert row.before_json == {"object": _device(), "expected_version": _VERSION}
    assert row.after_json["observed_version"] == _VERSION
    assert row.session_id == _SESSION_ID


async def test_patch_with_attribution_writes_conflict_audit_row() -> None:
    base = _netbox_base()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            # Re-read returns a newer version than the client expected — no PATCH route.
            router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            async with get_sessionmaker()() as session:
                with pytest.raises(WriteConflictError):
                    await _patch(client, session, expected_version=_VERSION)

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text("SELECT result::text, before_json, after_json FROM audit_log")
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].result == "conflict"
    assert rows[0].before_json == {"expected_version": _VERSION}
    assert rows[0].after_json["observed_version"] == _NEW_VERSION


async def test_patch_with_attribution_writes_failure_audit_row(fast_backoff: None) -> None:
    base = _netbox_base()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{base}{_DEVICE_PATH}").respond(status_code=500)
            async with get_sessionmaker()() as session:
                with pytest.raises(NetBoxServerError):
                    await _patch(client, session)

    async with get_sessionmaker()() as session:
        rows = (await session.execute(text("SELECT result::text FROM audit_log"))).all()

    assert len(rows) == 1
    assert rows[0].result == "failure"


async def test_patch_with_attribution_audit_request_id_matches_contextvar() -> None:
    base = _netbox_base()
    bound_id = "8400e7f2-aaaa-bbbb-cccc-1234567890ab"
    structlog.contextvars.bind_contextvars(request_id=bound_id)
    try:
        async with NetBoxClient.from_settings() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(f"{base}{_DEVICE_PATH}").respond(json=_device())
                router.patch(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
                router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
                async with get_sessionmaker()() as session:
                    await _patch(client, session)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")

    async with get_sessionmaker()() as session:
        rid = (await session.execute(text("SELECT request_id::text FROM audit_log"))).scalar_one()
    assert rid == bound_id


# ============================================================================
# post_with_attribution (Sprint 5 Task 1)
# ============================================================================

_CREATE_PATH = "/api/dcim/devices/"


async def test_post_with_attribution_writes_success_audit_row() -> None:
    """Real Postgres + respx happy path: the create lands a SUCCESS audit row
    with entity_id derived from the response, before_json={}, after_json
    carrying the created device."""
    base = _netbox_base()
    created = {
        "id": 99,
        "name": "sw-99",
        "status": {"value": "active"},
        "last_updated": _NEW_VERSION,
    }
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{base}{_CREATE_PATH}").respond(status_code=201, json=created)
            router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            async with get_sessionmaker()() as session:
                service = NetBoxWriteService(client, session, AuditLogRepository(session))
                result = await service.post_with_attribution(
                    netbox_path=_CREATE_PATH,
                    netbox_object_type="dcim.device",
                    netbox_object_id=None,  # device-create case: derive from response
                    entity_type="device",
                    entity_id=None,  # → resolved to "99" from created["id"]
                    operation="device.create",
                    payload={
                        "name": "sw-99",
                        "device_type": 11,
                        "role": 31,
                        "site": 1,
                        "status": "active",
                    },
                    user=_user(session_id=_SESSION_ID),
                    attach_journal=True,
                )

    assert result == created

    # Verify the audit row landed with all columns populated correctly.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT result::text, operation, entity_type, entity_id,"
                    " before_json, after_json, session_id::text"
                    " FROM audit_log"
                )
            )
        ).one()
    assert row.result == "success"
    assert row.operation == "device.create"
    assert row.entity_type == "device"
    assert row.entity_id == "99"  # derived from created["id"]
    assert row.before_json == {}
    assert row.after_json == {"object": created}
    assert row.session_id == _SESSION_ID
