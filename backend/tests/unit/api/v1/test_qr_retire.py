"""Endpoint tests for POST /api/v1/qr/{qr_id}/retire.

Mirrors ``test_qr_bind.py``: handler logic by direct ``await``, AsyncClient
for routing + role gating + body validation. Lifecycle orchestration is
stubbed; its own units live in ``tests/unit/services/qr/test_lifecycle.py``.

A real test Postgres is needed because the retire endpoint fetches the QR's
batch from the DB to compose the response (same shape as bind).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.qr import (
    QRRetireRequest,
    get_lifecycle_service,
    retire_qr,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.main import app
from app.netbox.errors import NetBoxValidationError
from app.services.netbox_write import WriteConflictError
from app.services.qr.lifecycle import (
    MissingVersionError,
    QRLifecycleService,
    QRNotFoundError,
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
)
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
_QR_ID = "DCQR-FREEKLM2"
_DEVICE_ID = 99
_VERSION = "2026-05-21T08:00:00.000000Z"
_NEW_VERSION = "2026-05-21T09:00:00.000000Z"


def _device_dict(version: str = _NEW_VERSION, *, qr_id: str | None = None) -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "core switch",
        "asset_tag": "A-9",
        "custom_fields": {"qr_id": qr_id},
        "last_updated": version,
    }


def _retired_qr(qr_id: str = _QR_ID, batch_id: UUID | None = None) -> QR:
    return QR(
        id=qr_id,
        batch_id=batch_id or uuid4(),
        status=QRStatus.RETIRED,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=_NOW,
        retired_reason=None,
    )


def _free_qr(qr_id: str, batch_id: UUID) -> QR:
    return QR(
        id=qr_id,
        batch_id=batch_id,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _batch(batch_id: UUID) -> QRBatch:
    return QRBatch(
        id=batch_id,
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=1,
        intended_site_id=5,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )


async def _seed_batch_and_qr(qr: QR) -> None:
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(_batch(qr.batch_id))
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()


class _StubLifecycleService:
    """Stand-in for ``QRLifecycleService.retire`` — returns a canned QR or raises."""

    def __init__(
        self,
        *,
        retired: QR | None = None,
        error: Exception | None = None,
    ) -> None:
        self._retired = retired
        self._error = error

    async def retire(
        self,
        *,
        qr_id: str,
        expected_version: str | None,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any] | None]:
        if self._error is not None:
            raise self._error
        assert self._retired is not None
        # Endpoint discards the second tuple element; ``None`` keeps the stub
        # honest about that contract.
        return self._retired, None


# ---------- handler logic (direct await) ----------


def _noop_sessionmaker() -> object:
    """Sentinel for direct-await tests where idempotency_key=None
    short-circuits the wrapper before sessionmaker is ever called."""
    raise AssertionError("sessionmaker should not be called when idempotency_key is None")

async def test_retire_qr_handler_returns_qr_on_happy_path(
    session: AsyncSession,
) -> None:
    batch_id = uuid4()
    await _seed_batch_and_qr(_free_qr(_QR_ID, batch_id))
    stub = _StubLifecycleService(retired=_retired_qr(batch_id=batch_id))

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=None),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    # Sprint 9 Task 0: handler always returns JSONResponse now.
    assert isinstance(result, JSONResponse)
    assert result.status_code == 200
    body = json.loads(bytes(result.body))
    assert body["qr"]["id"] == _QR_ID
    assert body["qr"]["status"] == "retired"
    assert body["qr"]["retired_at"] == _NOW.isoformat()
    assert body["qr"]["batch"]["intended_site_id"] == 5


async def test_retire_qr_handler_returns_404_when_qr_not_found(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRNotFoundError(_QR_ID))

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=None),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_NOT_FOUND"


async def test_retire_qr_handler_returns_409_on_qr_state_conflict(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRStateConflictError(QRStatus.RETIRED))

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=None),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_STATE_CONFLICT"
    assert body["error"]["current_status"] == "retired"


async def test_retire_qr_handler_returns_422_on_missing_version(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=MissingVersionError(_QR_ID))

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=None),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 422
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "VERSION_REQUIRED"


async def test_retire_qr_handler_returns_409_on_device_version_conflict(
    session: AsyncSession,
) -> None:
    current = _device_dict(_NEW_VERSION, qr_id=_QR_ID)
    stub = _StubLifecycleService(
        error=WriteConflictError(current_object=current, current_version=_NEW_VERSION)
    )

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION


async def test_retire_qr_handler_returns_500_on_rolled_back(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRRetireRolledBackError(_QR_ID, _DEVICE_ID))

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_RETIRE_ROLLED_BACK"


async def test_retire_qr_handler_returns_500_on_inconsistency(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRRetireInconsistencyError(_QR_ID, _DEVICE_ID))

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_RETIRE_INCONSISTENCY"


async def test_retire_qr_handler_translates_netbox_validation_error_to_422(
    session: AsyncSession,
) -> None:
    """Sprint 7 Task 5: rare in practice but covered for consistency — if
    NetBox rejects the device PATCH that clears custom_fields.qr_id on
    BOUND→RETIRED, 502 is misleading."""
    netbox_body = {"custom_fields": ["Invalid field write."]}
    stub = _StubLifecycleService(error=NetBoxValidationError(status_code=400, detail=netbox_body))

    result = await retire_qr(
        qr_id=_QR_ID,
        request=QRRetireRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 422
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body
    assert body["error"]["message"] == "NetBox rejected the retire"


# ---------- routing / role / validation (AsyncClient) ----------


async def test_post_retire_endpoint_returns_200_on_happy_path(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    batch_id = uuid4()
    await _seed_batch_and_qr(_free_qr(_QR_ID, batch_id))
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        retired=_retired_qr(batch_id=batch_id)
    )

    resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["id"] == _QR_ID
    assert body["qr"]["status"] == "retired"
    # device is not part of the retire response shape
    assert "device" not in body


async def test_post_retire_endpoint_returns_403_without_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    # Decision I: retire requires dcinv-admin, NOT dcinv-mobile-user.
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={})

    assert resp.status_code == 403


async def test_post_retire_endpoint_returns_422_for_extra_body_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/retire",
        json={"version": _VERSION, "rogue": True},
    )

    assert resp.status_code == 422


async def test_post_retire_endpoint_accepts_null_version_in_body(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Pydantic must accept ``version: null`` (FREE retire); the service
    decides whether the null is acceptable based on QR state."""
    as_user("dcinv-admin")
    batch_id = uuid4()
    await _seed_batch_and_qr(_free_qr(_QR_ID, batch_id))
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        retired=_retired_qr(batch_id=batch_id)
    )

    resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={"version": None})

    assert resp.status_code == 200


async def test_post_retire_endpoint_returns_404_when_qr_not_registered(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(f"/api/v1/qr/{_QR_ID}/retire", json={})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "QR_NOT_FOUND"
