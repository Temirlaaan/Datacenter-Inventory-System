"""Endpoint tests for POST /api/v1/qr/{qr_id}/unbind (docs/backend-tz-qr-unbind.md).

Handler logic by direct ``await``; ``AsyncClient`` proves routing, role-gating,
and request validation. The saga is stubbed — covered in
``tests/unit/services/qr/test_lifecycle.py``.
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

from app.api.v1.qr import QRUnbindRequest, get_lifecycle_service, unbind_qr
from app.auth.dependencies import AuthUser
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.main import app
from app.services.netbox_write import WriteConflictError
from app.services.qr.lifecycle import (
    QRLifecycleService,
    QRNotFoundError,
    QRStateConflictError,
    QRUnbindInconsistencyError,
    QRUnbindRolledBackError,
)
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)
_QR_ID = "DCQR-BOUNDKLM"
_DEVICE_ID = 99
_VERSION = "2026-06-16T08:00:00.000000Z"


def _device_dict() -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "",
        "asset_tag": "A-9",
        "custom_fields": {"qr_id": None},
        "last_updated": _VERSION,
    }


def _free_qr(batch_id: UUID) -> QR:
    return QR(
        id=_QR_ID,
        batch_id=batch_id,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _bound_qr_row(batch_id: UUID) -> QR:
    return QR(
        id=_QR_ID,
        batch_id=batch_id,
        status=QRStatus.BOUND,
        bound_to_device_id=_DEVICE_ID,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
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


async def _seed(qr: QR) -> None:
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(_batch(qr.batch_id))
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()


class _StubLifecycleService:
    def __init__(self, *, freed: QR | None = None, error: Exception | None = None) -> None:
        self._freed = freed
        self._error = error

    async def unbind(
        self, *, qr_id: str, expected_version: str, reason: str, user: AuthUser
    ) -> QR:
        if self._error is not None:
            raise self._error
        assert self._freed is not None
        return self._freed


def _noop_sessionmaker() -> object:
    raise AssertionError("sessionmaker should not be called when idempotency_key is None")


async def _call(stub: _StubLifecycleService, session: AsyncSession) -> JSONResponse:
    return await unbind_qr(
        qr_id=_QR_ID,
        request=QRUnbindRequest(version=_VERSION, reason="device removed"),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None,
    )


# ---------- handler logic (direct await) ----------


async def test_unbind_handler_returns_free_qr_on_happy_path(session: AsyncSession) -> None:
    batch_id = uuid4()
    await _seed(_bound_qr_row(batch_id))
    result = await _call(_StubLifecycleService(freed=_free_qr(batch_id)), session)

    assert isinstance(result, JSONResponse)
    assert result.status_code == 200
    body = json.loads(bytes(result.body))
    assert body["qr"]["id"] == _QR_ID
    assert body["qr"]["status"] == "free"
    # No device in the unbind response — the binding is gone.
    assert "device" not in body


@pytest.mark.parametrize(
    "error, expected_status, expected_code",
    [
        (QRNotFoundError(_QR_ID), 404, "QR_NOT_FOUND"),
        (QRStateConflictError(QRStatus.FREE), 409, "QR_NOT_BOUND"),
        (QRStateConflictError(QRStatus.RETIRED), 409, "QR_NOT_BOUND"),
        (QRUnbindRolledBackError(_QR_ID, _DEVICE_ID), 500, "QR_UNBIND_ROLLED_BACK"),
        (QRUnbindInconsistencyError(_QR_ID, _DEVICE_ID), 500, "QR_UNBIND_INCONSISTENCY"),
    ],
)
async def test_unbind_handler_maps_errors(
    session: AsyncSession, error: Exception, expected_status: int, expected_code: str
) -> None:
    result = await _call(_StubLifecycleService(error=error), session)
    assert result.status_code == expected_status
    assert json.loads(bytes(result.body))["error"]["code"] == expected_code


async def test_unbind_handler_returns_409_device_conflict(session: AsyncSession) -> None:
    stub = _StubLifecycleService(
        error=WriteConflictError(current_object=_device_dict(), current_version=_VERSION)
    )
    result = await _call(stub, session)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _VERSION


# ---------- routing / role / validation (AsyncClient) ----------


async def test_post_unbind_endpoint_200(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    batch_id = uuid4()
    await _seed(_bound_qr_row(batch_id))
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        freed=_free_qr(batch_id)
    )
    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/unbind",
        json={"version": _VERSION, "reason": "device removed"},
    )
    assert resp.status_code == 200
    assert resp.json()["qr"]["status"] == "free"


async def test_post_unbind_endpoint_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )
    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/unbind",
        json={"version": _VERSION, "reason": "x"},
    )
    assert resp.status_code == 403


async def test_post_unbind_endpoint_422_missing_reason(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )
    resp = await client.post(f"/api/v1/qr/{_QR_ID}/unbind", json={"version": _VERSION})
    assert resp.status_code == 422


async def test_post_unbind_endpoint_422_empty_reason(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )
    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/unbind",
        json={"version": _VERSION, "reason": ""},
    )
    assert resp.status_code == 422


async def test_post_unbind_endpoint_422_extra_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )
    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/unbind",
        json={"version": _VERSION, "reason": "x", "rogue": 1},
    )
    assert resp.status_code == 422
