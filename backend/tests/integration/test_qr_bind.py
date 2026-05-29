"""Integration tests for POST /api/v1/qr/{qr_id}/bind — endpoint + service + Postgres.

NetBox is faked with respx; Postgres is real. Confirms the full bind path:
- FREE→BOUND lands in qr_codes
- The three-record write (NetBox PATCH + journal POST + audit_log) actually
  reaches the DB with the right rows
- The optimistic-concurrency conflict path leaves qr_codes unchanged
- The qr_one_per_device partial unique index triggers the compensation flow
  and produces a 409 ``QR_ALREADY_BOUND`` response plus a compensation
  audit row with ``failure_stage="db_commit"``.
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

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_SUB = "11111111-1111-1111-1111-111111111111"
_QR_ID = "DCQR-FREEKLM2"
_OTHER_QR_ID = "DCQR-FREEKLM3"
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
    get_netbox_client.cache_clear()
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


async def _seed_free_qr(qr_id: str, batch_id: UUID | None = None) -> UUID:
    bid = batch_id or uuid4()
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


async def _seed_bound_qr(qr_id: str, device_id: int, batch_id: UUID | None = None) -> UUID:
    bid = batch_id or uuid4()
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


# ========== Happy path ==========


async def test_bind_persists_bound_state_and_writes_success_audit_row(
    client: httpx.AsyncClient,
) -> None:
    _as_mobile_user()
    await _seed_free_qr(_QR_ID)
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(qr_id=None))
        router.patch(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION, qr_id=_QR_ID))
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        resp = await client.post(
            f"/api/v1/qr/{_QR_ID}/bind",
            json={"device_id": _DEVICE_ID, "version": _VERSION},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["id"] == _QR_ID
    assert body["qr"]["status"] == "bound"
    assert body["qr"]["bound_to_device_id"] == _DEVICE_ID
    assert body["device"]["id"] == _DEVICE_ID

    # qr_codes row transitioned to BOUND.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status::text, bound_to_device_id FROM qr_codes WHERE id = :id"),
                {"id": _QR_ID},
            )
        ).one()
    assert row.status == "bound"
    assert row.bound_to_device_id == _DEVICE_ID

    # SUCCESS audit row landed (written inside patch_with_attribution).
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
    assert audits[0].operation == "qr.bind"
    assert audits[0].entity_type == "qr"
    assert audits[0].entity_id == _QR_ID


# ========== Optimistic concurrency conflict ==========


async def test_bind_returns_409_when_device_version_stale_and_leaves_qr_free(
    client: httpx.AsyncClient,
) -> None:
    _as_mobile_user()
    await _seed_free_qr(_QR_ID)
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        # Re-read returns a newer version — no PATCH should fire.
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))

        resp = await client.post(
            f"/api/v1/qr/{_QR_ID}/bind",
            json={"device_id": _DEVICE_ID, "version": _VERSION},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION

    # qr_codes row is unchanged.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status::text FROM qr_codes WHERE id = :id"),
                {"id": _QR_ID},
            )
        ).one()
    assert row.status == "free"

    # CONFLICT audit row landed.
    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(text("SELECT result::text, operation FROM audit_log"))
        ).all()
    assert len(audits) == 1
    assert audits[0].result == "conflict"
    assert audits[0].operation == "qr.bind"


# ========== qr_one_per_device race ==========


async def test_bind_qr_one_per_device_race_returns_409_and_writes_compensation_audit(
    client: httpx.AsyncClient,
) -> None:
    """Device 5 is already BOUND to QR-A; binding QR-B raises QRAlreadyBoundError.

    The real ``qr_one_per_device`` partial unique index fires on the UPDATE,
    compensation runs (clears NetBox qr_id), and a compensation audit row
    lands with ``failure_stage="db_commit"`` and
    ``compensation_outcome="cleared"``.
    """
    _as_mobile_user()
    # QR-A is already bound to device 5.
    await _seed_bound_qr("DCQR-FREEKLM4", _DEVICE_ID)
    # QR-B is free; the bind attempt below races on device 5.
    await _seed_free_qr(_OTHER_QR_ID)
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        # GET is called TWICE: first by Step B (pre-PATCH re-read returns
        # qr_id=None) and again by compensation (post-PATCH state, qr_id=QR-B).
        # side_effect gives different responses per call.
        router.get(f"{base}{_DEVICE_PATH}").mock(
            side_effect=[
                httpx.Response(200, json=_device(qr_id=None)),
                httpx.Response(200, json=_device(_NEW_VERSION, qr_id=_OTHER_QR_ID)),
            ]
        )
        # PATCH is also called twice: Step B sets qr_id=QR-B; compensation
        # clears it back to null.
        router.patch(f"{base}{_DEVICE_PATH}").mock(
            side_effect=[
                httpx.Response(200, json=_device(_NEW_VERSION, qr_id=_OTHER_QR_ID)),
                httpx.Response(200, json=_device(_NEW_VERSION, qr_id=None)),
            ]
        )
        # patch_with_attribution's journal POST (regular flow, succeeds).
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        resp = await client.post(
            f"/api/v1/qr/{_OTHER_QR_ID}/bind",
            json={"device_id": _DEVICE_ID, "version": _VERSION},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QR_ALREADY_BOUND"

    # QR-B stays FREE — its UPDATE was rolled back by the IntegrityError.
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT status::text FROM qr_codes WHERE id = :id"),
                {"id": _OTHER_QR_ID},
            )
        ).one()
    assert row.status == "free"

    # Audit rows: one SUCCESS from patch_with_attribution (the NetBox PATCH did
    # land), and one FAILURE compensation row from the lifecycle service.
    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(
                text(
                    "SELECT result::text, operation, entity_type, entity_id, after_json"
                    " FROM audit_log ORDER BY timestamp"
                )
            )
        ).all()
    results = [a.result for a in audits]
    assert "success" in results, f"expected SUCCESS from patch_with_attribution, got {results}"
    assert "failure" in results, f"expected FAILURE compensation row, got {results}"
    failure_row = next(a for a in audits if a.result == "failure")
    assert failure_row.operation == "qr.bind"
    assert failure_row.entity_type == "qr"
    assert failure_row.entity_id == _OTHER_QR_ID
    assert failure_row.after_json["failure_stage"] == "db_commit"
    assert failure_row.after_json["compensation_outcome"] == "cleared"


# ========== Role gating end-to-end ==========


async def test_bind_endpoint_requires_mobile_role(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),  # no mobile role
        session_id=None,
    )
    await _seed_free_qr(_QR_ID)

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/bind",
        json={"device_id": _DEVICE_ID, "version": _VERSION},
    )

    assert resp.status_code == 403
