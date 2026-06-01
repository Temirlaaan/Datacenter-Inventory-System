"""Integration tests for POST /api/v1/qr/{qr_id}/retire — endpoint + service + Postgres.

Mirrors ``test_qr_bind.py``. Covers:
- FREE→RETIRED: pure DB transition, ZERO NetBox calls, atomic SUCCESS audit row
- BOUND→RETIRED: full three-record write, qr_codes transitions, device's
  custom_fields.qr_id cleared (verified through respx)
- Stale-version conflict on BOUND→RETIRED leaves the QR BOUND
- Role gating: dcinv-admin only (decision I) — dcinv-mobile-user → 403
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
import structlog
from sqlalchemy import text

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.main import app
from app.netbox.client import get_netbox_client
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_SUB = "11111111-1111-1111-1111-111111111111"
_QR_ID = "DCQR-FREEKLM2"
_DEVICE_ID = 5
_DEVICE_PATH = f"/api/dcim/devices/{_DEVICE_ID}/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_VERSION = "2026-05-21T08:00:00.000000Z"
_NEW_VERSION = "2026-05-21T09:00:00.000000Z"
_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


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
    get_netbox_client.cache_clear()
    structlog.contextvars.clear_contextvars()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
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
    get_netbox_client.cache_clear()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _as_admin() -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        session_id=None,
    )


def _as_mobile_user() -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-mobile-user",),
        session_id=None,
    )


def _device(version: str = _VERSION, *, qr_id: str | None = None) -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "core switch",
        "custom_fields": {"asset_tag": "A-9", "qr_id": qr_id},
        "last_updated": version,
    }


def _netbox_base() -> str:
    return str(get_settings().netbox_url).rstrip("/")


async def _seed_free_qr(qr_id: str) -> UUID:
    bid = uuid4()
    batch = QRBatch(
        id=bid,
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID(_USER_SUB),
        count=1,
        intended_site_id=1,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    qr = QR(
        id=qr_id,
        batch_id=bid,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()
    return bid


async def _seed_bound_qr(qr_id: str, device_id: int) -> UUID:
    bid = uuid4()
    batch = QRBatch(
        id=bid,
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID(_USER_SUB),
        count=1,
        intended_site_id=1,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    qr = QR(
        id=qr_id,
        batch_id=bid,
        status=QRStatus.BOUND,
        bound_to_device_id=device_id,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()
    return bid


# ========== FREE → RETIRED ==========


async def test_retire_free_persists_retired_state_with_zero_netbox_calls(
    client: httpx.AsyncClient,
) -> None:
    _as_admin()
    await _seed_free_qr(_QR_ID)

    # respx with no routes — any NetBox call would 404 + respx would yelp.
    with respx.mock(assert_all_called=False) as router:
        resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={})

    assert router.calls.call_count == 0
    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["status"] == "retired"

    # qr_codes row transitioned.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status::text, bound_to_device_id FROM qr_codes WHERE id = :id"),
                {"id": _QR_ID},
            )
        ).one()
    assert row.status == "retired"
    assert row.bound_to_device_id is None

    # Atomic SUCCESS audit row landed.
    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(
                text("SELECT result::text, operation, entity_type, entity_id FROM audit_log")
            )
        ).all()
    assert len(audits) == 1
    assert audits[0].result == "success"
    assert audits[0].operation == "qr.retire"
    assert audits[0].entity_type == "qr"
    assert audits[0].entity_id == _QR_ID


# ========== BOUND → RETIRED ==========


async def test_retire_bound_persists_retired_state_and_writes_success_audit_row(
    client: httpx.AsyncClient,
) -> None:
    _as_admin()
    await _seed_bound_qr(_QR_ID, _DEVICE_ID)
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        # Step B re-read shows the bound device.
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(qr_id=_QR_ID))
        # Step B PATCH clears qr_id.
        router.patch(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION, qr_id=None))
        # patch_with_attribution's journal POST.
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={"version": _VERSION})

    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["status"] == "retired"

    # qr_codes row transitioned to RETIRED. Note: ``bound_to_device_id`` is
    # *preserved* per Sprint 2's domain design ("Historical bound_* fields are
    # preserved on a BOUND → RETIRED transition so audit/forensics can trace
    # prior ownership"). The qr_one_per_device partial unique index only
    # constrains BOUND rows, so this is safe.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status::text, bound_to_device_id FROM qr_codes WHERE id = :id"),
                {"id": _QR_ID},
            )
        ).one()
    assert row.status == "retired"
    assert row.bound_to_device_id == _DEVICE_ID

    # SUCCESS audit row from patch_with_attribution.
    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(
                text(
                    "SELECT result::text, operation, entity_type, entity_id"
                    " FROM audit_log ORDER BY timestamp"
                )
            )
        ).all()
    assert len(audits) == 1
    assert audits[0].result == "success"
    assert audits[0].operation == "qr.retire"
    assert audits[0].entity_id == _QR_ID


async def test_retire_bound_returns_409_when_device_version_stale_and_leaves_qr_bound(
    client: httpx.AsyncClient,
) -> None:
    _as_admin()
    await _seed_bound_qr(_QR_ID, _DEVICE_ID)
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        # Re-read returns a newer version — no PATCH should fire.
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION, qr_id=_QR_ID))

        resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={"version": _VERSION})

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "DEVICE_CONFLICT"

    # qr_codes row unchanged.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status::text, bound_to_device_id FROM qr_codes WHERE id = :id"),
                {"id": _QR_ID},
            )
        ).one()
    assert row.status == "bound"
    assert row.bound_to_device_id == _DEVICE_ID

    # CONFLICT audit row landed.
    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(text("SELECT result::text, operation FROM audit_log"))
        ).all()
    assert len(audits) == 1
    assert audits[0].result == "conflict"
    assert audits[0].operation == "qr.retire"


# ========== Role gating ==========


async def test_retire_endpoint_requires_admin_role_rejects_mobile_user(
    client: httpx.AsyncClient,
) -> None:
    """Decision I: retire is dcinv-admin only — dcinv-mobile-user gets 403."""
    _as_mobile_user()
    await _seed_free_qr(_QR_ID)

    resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={})

    assert resp.status_code == 403
