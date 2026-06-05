"""Integration tests for POST /api/v1/devices/{id}/decommission — Sprint 5 Task 4.

Endpoint + service + Postgres + respx-mocked NetBox. Pins:
- Unbound device: 1 audit row (``device.decommission``/``success``).
- Bound device: 2 audit rows (``qr.retire``/``success``, ``device.decommission``/``success``),
  shared ``request_id``; bound QR transitions BOUND → RETIRED with historical
  ``bound_to_device_id`` preserved.
- Stale version on the device PATCH: 409 + audit row records the conflict, QR
  untouched.
- ``dcinv-mobile-user`` is rejected with 403 (decision G).
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
_DEVICE_ID = 5
_QR_ID = "DCQR-FREEKLM2"
_DEVICE_PATH = f"/api/dcim/devices/{_DEVICE_ID}/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_VERSION = "2026-05-21T08:00:00.000000Z"
_POST_RETIRE_VERSION = "2026-05-21T09:00:00.000000Z"
_FINAL_VERSION = "2026-05-21T10:00:00.000000Z"
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


def _device(
    version: str = _VERSION,
    *,
    qr_id: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": status, "label": status.title()},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "core switch",
        "asset_tag": "A-9",
        "custom_fields": {"qr_id": qr_id},
        "last_updated": version,
    }


def _netbox_base() -> str:
    return str(get_settings().netbox_url).rstrip("/")


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


# ========== unbound device — one PATCH, one audit row ==========


async def test_decommission_unbound_device_persists_decommissioning_status_and_audit(
    client: httpx.AsyncClient,
) -> None:
    _as_admin()
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        # Step C re-read (no bound QR → uses caller-provided version).
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(_VERSION, qr_id=None))
        # Step C PATCH to decommissioning.
        router.patch(f"{base}{_DEVICE_PATH}").respond(
            json=_device(_FINAL_VERSION, qr_id=None, status="decommissioning")
        )
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        resp = await client.post(
            f"/api/v1/devices/{_DEVICE_ID}/decommission",
            json={"version": _VERSION, "reason": "end of life"},
        )
        # Sprint 7 Task 4: reason flows through to the NetBox journal comment.
        journal_calls = [c for c in router.calls if c.request.url.path == _JOURNAL_PATH]
        assert len(journal_calls) == 1
        import json as _json

        journal_body = _json.loads(journal_calls[0].request.content)
        assert "Reason: end of life" in journal_body["comments"]

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"]["value"] == "decommissioning"
    assert body["version"] == _FINAL_VERSION

    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(
                text(
                    "SELECT result::text, operation, entity_type, entity_id"
                    " FROM audit_log ORDER BY timestamp"
                )
            )
        ).all()
    # No bound QR → one audit row only (device.decommission/success).
    assert len(audits) == 1
    assert audits[0].result == "success"
    assert audits[0].operation == "device.decommission"
    assert audits[0].entity_type == "device"
    assert audits[0].entity_id == str(_DEVICE_ID)


async def test_decommission_unbound_returns_422_when_netbox_rejects_with_400(
    client: httpx.AsyncClient,
) -> None:
    """Sprint 7 Task 5: end-to-end NBV on the unbound-device path → structured 422."""
    _as_admin()
    base = _netbox_base()
    netbox_body = {"status": ["Invalid status transition."]}

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(_VERSION, qr_id=None))
        router.patch(f"{base}{_DEVICE_PATH}").respond(status_code=400, json=netbox_body)

        resp = await client.post(
            f"/api/v1/devices/{_DEVICE_ID}/decommission",
            json={"version": _VERSION, "reason": "end of life"},
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body

    # FAILURE audit row landed from patch_with_attribution.
    async with get_sessionmaker()() as session:
        audits = (await session.execute(text("SELECT result::text FROM audit_log"))).all()
    assert len(audits) == 1
    assert audits[0].result == "failure"


# ========== bound device — retire + decommission, two audit rows ==========


async def test_decommission_bound_device_retires_qr_and_decommissions_in_one_flow(
    client: httpx.AsyncClient,
) -> None:
    _as_admin()
    await _seed_bound_qr(_QR_ID, _DEVICE_ID)
    base = _netbox_base()

    # Two re-read responses (Step B then Step C). Two PATCH responses
    # (qr_id clear, then status change). respx routes are FIFO via .respond(),
    # so use .mock(side_effect=...) for sequence.
    with respx.mock(assert_all_called=True) as router:
        get_route = router.get(f"{base}{_DEVICE_PATH}")
        get_route.side_effect = [
            httpx.Response(200, json=_device(_VERSION, qr_id=_QR_ID)),
            httpx.Response(200, json=_device(_POST_RETIRE_VERSION, qr_id=None)),
        ]
        patch_route = router.patch(f"{base}{_DEVICE_PATH}")
        patch_route.side_effect = [
            # Step B PATCH (qr.retire → clears qr_id, bumps last_updated)
            httpx.Response(200, json=_device(_POST_RETIRE_VERSION, qr_id=None)),
            # Step C PATCH (status → decommissioning)
            httpx.Response(
                200,
                json=_device(_FINAL_VERSION, qr_id=None, status="decommissioning"),
            ),
        ]
        router.post(f"{base}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})

        resp = await client.post(
            f"/api/v1/devices/{_DEVICE_ID}/decommission",
            json={"version": _VERSION},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"]["value"] == "decommissioning"

    # qr_codes row: BOUND → RETIRED, historical bound_to_device_id preserved.
    async with get_sessionmaker()() as session:
        qr_row = (
            await session.execute(
                text("SELECT status::text, bound_to_device_id FROM qr_codes WHERE id = :id"),
                {"id": _QR_ID},
            )
        ).one()
    assert qr_row.status == "retired"
    assert qr_row.bound_to_device_id == _DEVICE_ID

    # Two audit rows: qr.retire then device.decommission, sharing request_id.
    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(
                text(
                    "SELECT result::text, operation, entity_type, entity_id,"
                    " request_id::text AS request_id"
                    " FROM audit_log ORDER BY timestamp"
                )
            )
        ).all()
    assert len(audits) == 2
    assert audits[0].operation == "qr.retire"
    assert audits[0].result == "success"
    assert audits[0].entity_id == _QR_ID
    assert audits[1].operation == "device.decommission"
    assert audits[1].result == "success"
    assert audits[1].entity_id == str(_DEVICE_ID)
    assert audits[0].request_id == audits[1].request_id


# ========== stale-version conflict on unbound device ==========


async def test_decommission_returns_409_when_device_version_stale(
    client: httpx.AsyncClient,
) -> None:
    _as_admin()
    base = _netbox_base()

    with respx.mock(assert_all_called=True) as router:
        # Step C re-read shows a newer version → conflict, no PATCH.
        router.get(f"{base}{_DEVICE_PATH}").respond(json=_device(_POST_RETIRE_VERSION))

        resp = await client.post(
            f"/api/v1/devices/{_DEVICE_ID}/decommission",
            json={"version": _VERSION},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _POST_RETIRE_VERSION

    async with get_sessionmaker()() as session:
        audits = (
            await session.execute(text("SELECT result::text, operation FROM audit_log"))
        ).all()
    assert len(audits) == 1
    assert audits[0].result == "conflict"
    assert audits[0].operation == "device.decommission"


# ========== role gating ==========


async def test_decommission_endpoint_requires_admin_role(
    client: httpx.AsyncClient,
) -> None:
    """Decision G: decommission is dcinv-admin only — dcinv-mobile-user gets 403."""
    _as_mobile_user()

    resp = await client.post(
        f"/api/v1/devices/{_DEVICE_ID}/decommission",
        json={"version": _VERSION},
    )

    assert resp.status_code == 403
    # No NetBox calls, no audit rows.
    async with get_sessionmaker()() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM audit_log"))).scalar_one()
    assert count == 0
