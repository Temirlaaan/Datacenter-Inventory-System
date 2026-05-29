"""Endpoint tests for POST /api/v1/qr/{qr_id}/bind.

Strategy mirrors ``test_devices.py`` for the device update endpoint: handler
logic by direct ``await``; ``AsyncClient`` proves routing, role-gating, and
request validation. The lifecycle orchestration is stubbed — its own units are
exhaustively tested in ``tests/unit/services/qr/test_lifecycle.py``.

A real test Postgres is needed because the bind endpoint fetches the QR's
batch from the DB to compose the response. The ``_truncate`` fixture from
``conftest.py`` provides a clean schema per test.
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
    QRBindRequest,
    bind_qr,
    get_lifecycle_service,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.main import app
from app.services.netbox_write import WriteConflictError
from app.services.qr.lifecycle import (
    QRAlreadyBoundError,
    QRBindInconsistencyError,
    QRBindRolledBackError,
    QRLifecycleService,
    QRNotFoundError,
    QRStateConflictError,
)
from app.services.qr.lookup import QRLookupResponse
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
_QR_ID = "DCQR-FREEKLM2"
_DEVICE_ID = 99
_VERSION = "2026-05-21T08:00:00.000000Z"
_NEW_VERSION = "2026-05-21T09:00:00.000000Z"


def _device_dict(version: str = _NEW_VERSION, *, qr_id: str | None = _QR_ID) -> dict[str, Any]:
    """A raw NetBox device payload — every key ``to_device_data`` reads is present."""
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


def _bound_qr(qr_id: str = _QR_ID, batch_id: UUID | None = None) -> QR:
    return QR(
        id=qr_id,
        batch_id=batch_id or uuid4(),
        status=QRStatus.BOUND,
        bound_to_device_id=_DEVICE_ID,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=None,
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


def _batch(batch_id: UUID, *, intended_site_id: int | None = 5) -> QRBatch:
    return QRBatch(
        id=batch_id,
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=1,
        intended_site_id=intended_site_id,
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
    """Stand-in for ``QRLifecycleService.bind`` — returns a canned result or raises."""

    def __init__(
        self,
        *,
        bound: QR | None = None,
        device: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._bound = bound
        self._device = device
        self._error = error

    async def bind(
        self,
        *,
        qr_id: str,
        device_id: int,
        expected_version: str,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any]]:
        if self._error is not None:
            raise self._error
        assert self._bound is not None
        assert self._device is not None
        return self._bound, self._device


# ---------- handler logic (direct await) ----------


async def test_bind_qr_handler_returns_combined_response_on_happy_path(
    session: AsyncSession,
) -> None:
    batch_id = uuid4()
    await _seed_batch_and_qr(_free_qr(_QR_ID, batch_id))
    stub = _StubLifecycleService(
        bound=_bound_qr(batch_id=batch_id),
        device=_device_dict(),
    )

    result = await bind_qr(
        qr_id=_QR_ID,
        request=QRBindRequest(device_id=_DEVICE_ID, version=_VERSION),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
    )

    assert isinstance(result, QRLookupResponse)
    assert result.qr.id == _QR_ID
    assert result.qr.status is QRStatus.BOUND
    assert result.qr.bound_to_device_id == _DEVICE_ID
    assert result.qr.batch.intended_site_id == 5
    assert result.device is not None
    assert result.device.id == _DEVICE_ID
    assert result.device.name == "sw-01"
    assert result.device_error is None


async def test_bind_qr_handler_returns_404_when_qr_not_found(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRNotFoundError(_QR_ID))

    result = await bind_qr(
        qr_id=_QR_ID,
        request=QRBindRequest(device_id=_DEVICE_ID, version=_VERSION),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_NOT_FOUND"


async def test_bind_qr_handler_returns_409_on_qr_state_conflict(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRStateConflictError(QRStatus.BOUND))

    result = await bind_qr(
        qr_id=_QR_ID,
        request=QRBindRequest(device_id=_DEVICE_ID, version=_VERSION),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_STATE_CONFLICT"
    assert body["error"]["current_status"] == "bound"


async def test_bind_qr_handler_returns_409_on_device_version_conflict(
    session: AsyncSession,
) -> None:
    current = _device_dict(_NEW_VERSION)
    stub = _StubLifecycleService(
        error=WriteConflictError(current_object=current, current_version=_NEW_VERSION)
    )

    result = await bind_qr(
        qr_id=_QR_ID,
        request=QRBindRequest(device_id=_DEVICE_ID, version=_VERSION),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION
    assert body["error"]["current_state"]["id"] == _DEVICE_ID


async def test_bind_qr_handler_returns_409_on_qr_already_bound(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRAlreadyBoundError(_QR_ID, _DEVICE_ID))

    result = await bind_qr(
        qr_id=_QR_ID,
        request=QRBindRequest(device_id=_DEVICE_ID, version=_VERSION),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_ALREADY_BOUND"


async def test_bind_qr_handler_returns_500_on_rolled_back(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRBindRolledBackError(_QR_ID, _DEVICE_ID))

    result = await bind_qr(
        qr_id=_QR_ID,
        request=QRBindRequest(device_id=_DEVICE_ID, version=_VERSION),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_BIND_ROLLED_BACK"


async def test_bind_qr_handler_returns_500_on_inconsistency(
    session: AsyncSession,
) -> None:
    stub = _StubLifecycleService(error=QRBindInconsistencyError(_QR_ID, _DEVICE_ID))

    result = await bind_qr(
        qr_id=_QR_ID,
        request=QRBindRequest(device_id=_DEVICE_ID, version=_VERSION),
        user=make_user("dcinv-mobile-user"),
        lifecycle=cast(QRLifecycleService, stub),
        session=session,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "QR_BIND_INCONSISTENCY"


async def test_get_lifecycle_service_builds_a_qr_lifecycle_service(
    session: AsyncSession,
) -> None:
    assert isinstance(get_lifecycle_service(session=session), QRLifecycleService)


# ---------- routing / role / validation (AsyncClient) ----------


async def test_post_bind_endpoint_returns_200_on_happy_path(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    batch_id = uuid4()
    await _seed_batch_and_qr(_free_qr(_QR_ID, batch_id))
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        bound=_bound_qr(batch_id=batch_id),
        device=_device_dict(),
    )

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
    # H1 fix: device.qr_id is the freshly-bound token (decision H — app DB
    # is the source of truth, not NetBox's custom_fields.qr_id).
    assert body["device"]["qr_id"] == _QR_ID
    # device_error is None → dropped by response_model_exclude_none
    assert "device_error" not in body


async def test_post_bind_endpoint_returns_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")  # has admin, missing mobile
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/bind",
        json={"device_id": _DEVICE_ID, "version": _VERSION},
    )

    assert resp.status_code == 403


async def test_post_bind_endpoint_returns_422_for_missing_device_id(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(f"/api/v1/qr/{_QR_ID}/bind", json={"version": _VERSION})

    assert resp.status_code == 422


async def test_post_bind_endpoint_returns_422_for_missing_version(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(f"/api/v1/qr/{_QR_ID}/bind", json={"device_id": _DEVICE_ID})

    assert resp.status_code == 422


async def test_post_bind_endpoint_returns_422_for_extra_body_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """``extra='forbid'`` on QRBindRequest must reject unknown keys."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/bind",
        json={"device_id": _DEVICE_ID, "version": _VERSION, "rogue": True},
    )

    assert resp.status_code == 422


async def test_post_bind_endpoint_404_when_qr_not_registered(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService(
        error=QRNotFoundError(_QR_ID)
    )

    resp = await client.post(
        f"/api/v1/qr/{_QR_ID}/bind",
        json={"device_id": _DEVICE_ID, "version": _VERSION},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "QR_NOT_FOUND"
