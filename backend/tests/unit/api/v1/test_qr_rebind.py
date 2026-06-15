"""Endpoint tests for POST /api/v1/qr/{qr_id}/rebind (docs/backend-tz-qr-rebind.md).

Handler logic by direct ``await``; ``AsyncClient`` proves routing, role-gating,
and request validation. The saga orchestration is stubbed — its units are
exhaustively covered in ``tests/unit/services/qr/test_lifecycle.py``.

Integration-marked: the happy path fetches the QR's batch from Postgres to
compose the response (the ``conftest`` schema fixture provides a clean DB).
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
    QRRebindRequest,
    get_lifecycle_service,
    rebind_qr,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.main import app
from app.netbox.errors import NetBoxNotFound
from app.services.netbox_write import WriteConflictError
from app.services.qr.lifecycle import (
    DeviceAlreadyBoundError,
    QRLifecycleService,
    QRNotFoundError,
    QRRebindInconsistencyError,
    QRRebindRolledBackError,
    QRStateConflictError,
    SameDeviceError,
)
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_QR_ID = "DCQR-BOUNDKLM"
_OLD_DEVICE_ID = 99
_NEW_DEVICE_ID = 200
_VERSION = "2026-06-15T08:00:00.000000Z"
_NEW_VERSION = "2026-06-15T09:00:00.000000Z"


def _device_dict(device_id: int = _NEW_DEVICE_ID, version: str = _NEW_VERSION) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": "sw-02",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 40,
        "serial": "XYZ789",
        "comments": "swapped switch",
        "asset_tag": "A-10",
        "custom_fields": {"qr_id": _QR_ID},
        "last_updated": version,
    }


def _bound_qr(batch_id: UUID, *, device_id: int = _NEW_DEVICE_ID) -> QR:
    return QR(
        id=_QR_ID,
        batch_id=batch_id,
        status=QRStatus.BOUND,
        bound_to_device_id=device_id,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )


def _seed_bound_qr_at_old_device(batch_id: UUID) -> QR:
    return QR(
        id=_QR_ID,
        batch_id=batch_id,
        status=QRStatus.BOUND,
        bound_to_device_id=_OLD_DEVICE_ID,
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
    """Stand-in for ``QRLifecycleService.rebind`` — canned result or raise."""

    def __init__(
        self,
        *,
        rebound: QR | None = None,
        device: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._rebound = rebound
        self._device = device
        self._error = error

    async def rebind(
        self,
        *,
        qr_id: str,
        new_device_id: int,
        expected_version: str,
        reason: str,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any]]:
        if self._error is not None:
            raise self._error
        assert self._rebound is not None
        assert self._device is not None
        return self._rebound, self._device


def _noop_sessionmaker() -> object:
    raise AssertionError("sessionmaker should not be called when idempotency_key is None")


def _req() -> QRRebindRequest:
    return QRRebindRequest(device_id=_NEW_DEVICE_ID, version=_VERSION, reason="swap")


async def _call(stub: _StubLifecycleService, session: AsyncSession) -> JSONResponse:
    return await rebind_qr(
        qr_id=_QR_ID,
        request=_req(),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
        sessionmaker=cast(object, _noop_sessionmaker),  # type: ignore[arg-type]
        idempotency_key=None,
    )


# ---------- handler logic (direct await) ----------


async def test_rebind_handler_returns_combined_response_on_happy_path(
    session: AsyncSession,
) -> None:
    batch_id = uuid4()
    await _seed(_seed_bound_qr_at_old_device(batch_id))
    stub = _StubLifecycleService(
        rebound=_bound_qr(batch_id),
        device=_device_dict(),
    )

    result = await _call(stub, session)

    assert isinstance(result, JSONResponse)
    assert result.status_code == 200
    body = json.loads(bytes(result.body))
    assert body["qr"]["id"] == _QR_ID
    assert body["qr"]["status"] == "bound"
    assert body["qr"]["bound_to_device_id"] == _NEW_DEVICE_ID
    assert body["device"]["id"] == _NEW_DEVICE_ID
    assert body["device"]["qr_id"] == _QR_ID


@pytest.mark.parametrize(
    "error, expected_status, expected_code",
    [
        (QRNotFoundError(_QR_ID), 404, "QR_NOT_FOUND"),
        (QRStateConflictError(QRStatus.FREE), 409, "QR_NOT_BOUND"),
        (QRStateConflictError(QRStatus.RETIRED), 409, "QR_NOT_BOUND"),
        (SameDeviceError(_NEW_DEVICE_ID), 409, "SAME_DEVICE"),
        (NetBoxNotFound("device 200 -> 404"), 404, "DEVICE_NOT_FOUND"),
        (DeviceAlreadyBoundError(_NEW_DEVICE_ID, "DCQR-OTHER1"), 409, "DEVICE_ALREADY_BOUND"),
        (
            QRRebindRolledBackError(_QR_ID, _OLD_DEVICE_ID, _NEW_DEVICE_ID),
            500,
            "QR_REBIND_ROLLED_BACK",
        ),
        (
            QRRebindInconsistencyError(_QR_ID, _OLD_DEVICE_ID, _NEW_DEVICE_ID),
            500,
            "QR_REBIND_INCONSISTENCY",
        ),
    ],
)
async def test_rebind_handler_maps_errors_to_status_and_code(
    session: AsyncSession,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    result = await _call(_StubLifecycleService(error=error), session)

    assert isinstance(result, JSONResponse)
    assert result.status_code == expected_status
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == expected_code


async def test_rebind_handler_returns_409_device_conflict_with_current_state(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(
        error=WriteConflictError(
            current_object=_device_dict(version=_NEW_VERSION), current_version=_NEW_VERSION
        )
    )

    result = await _call(stub, session)

    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION
    assert body["error"]["current_state"]["id"] == _NEW_DEVICE_ID


async def test_rebind_handler_device_already_bound_surfaces_existing_token(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(
        error=DeviceAlreadyBoundError(_NEW_DEVICE_ID, "DCQR-OTHER1")
    )

    result = await _call(stub, session)

    body = json.loads(bytes(result.body))
    assert body["error"]["existing_qr_id"] == "DCQR-OTHER1"


# ---------- routing / role / validation (AsyncClient) ----------


async def test_post_rebind_endpoint_returns_200_on_happy_path(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    batch_id = uuid4()
    await _seed(_seed_bound_qr_at_old_device(batch_id))
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        rebound=_bound_qr(batch_id),
        device=_device_dict(),
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/rebind",
        json={"device_id": _NEW_DEVICE_ID, "version": _VERSION, "reason": "swap"},
    )

    assert resp.status_code == 200
    assert resp.json()["qr"]["bound_to_device_id"] == _NEW_DEVICE_ID


async def test_post_rebind_endpoint_returns_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")  # admin lacks the mobile-user role
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/rebind",
        json={"device_id": _NEW_DEVICE_ID, "version": _VERSION, "reason": "swap"},
    )

    assert resp.status_code == 403


async def test_post_rebind_endpoint_returns_422_for_missing_reason(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """``reason`` is mandatory — the rebind moves a label between devices."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/rebind",
        json={"device_id": _NEW_DEVICE_ID, "version": _VERSION},
    )

    assert resp.status_code == 422


async def test_post_rebind_endpoint_returns_422_for_empty_reason(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/rebind",
        json={"device_id": _NEW_DEVICE_ID, "version": _VERSION, "reason": ""},
    )

    assert resp.status_code == 422


async def test_post_rebind_endpoint_returns_422_for_extra_body_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/rebind",
        json={"device_id": _NEW_DEVICE_ID, "version": _VERSION, "reason": "x", "rogue": 1},
    )

    assert resp.status_code == 422
