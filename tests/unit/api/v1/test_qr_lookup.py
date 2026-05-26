"""Tests for the GET /api/v1/qr/{qr_id} endpoint.

Sprint 4 Task 3: the response shape is now the combined ``QRLookupResponse``
``{qr, device, device_error}``. The previous flat ``QRInfo`` is preserved
as an embedded ``qr`` field. Tests assert against the new shape; BOUND-path
device fetch + soft-fail behaviour is covered at the service level
(``tests/unit/services/qr/test_lookup.py``) — here we verify wiring,
routing, role-gating, and the wire-format change.

Handler-call tests stub the lifecycle service via ``app.dependency_overrides``
on ``get_lookup_service`` (note 2 in Task 3 review). FREE/RETIRED responses
require no NetBox roundtrip and are seeded via the real Postgres test DB;
BOUND end-to-end coverage lives in the integration tests for Tasks 1/2
(bind/retire) which exercise the combined response after a successful write.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.api.v1.qr import get_lookup_service, lookup_qr
from app.auth.dependencies import AuthUser
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.main import app
from app.services.qr.lookup import (
    QRInfo,
    QRLookupBatchInfo,
    QRLookupResponse,
    QRLookupService,
)
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


async def _seed(qr: QR, *, intended_site_id: int | None = 5) -> None:
    """Insert a batch + the given QR directly (the generation service only makes
    FREE codes, so BOUND/RETIRED fixtures are seeded by hand)."""
    batch = QRBatch(
        id=qr.batch_id,
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=1,
        intended_site_id=intended_site_id,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()


def _free(qr_id: str) -> QR:
    return QR(
        id=qr_id,
        batch_id=uuid4(),
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _qr_info(
    qr_id: str = "DCQR-AAAAAAAA",
    *,
    status: QRStatus = QRStatus.FREE,
    bound_to_device_id: int | None = None,
    bound_at: datetime | None = None,
    retired_at: datetime | None = None,
    retired_reason: str | None = None,
) -> QRInfo:
    return QRInfo(
        id=qr_id,
        status=status,
        batch=QRLookupBatchInfo(
            intended_site_id=5,
            intended_location_id=None,
            intended_rack_id=None,
        ),
        bound_to_device_id=bound_to_device_id,
        bound_at=bound_at,
        retired_at=retired_at,
        retired_reason=retired_reason,
    )


class _StubLookupService:
    """Stand-in for ``QRLookupService.get_by_id`` — returns a canned response
    or ``None`` (for the 404 path)."""

    def __init__(self, *, result: QRLookupResponse | None) -> None:
        self._result = result
        self.calls: list[str] = []

    async def get_by_id(self, qr_id: str) -> QRLookupResponse | None:
        self.calls.append(qr_id)
        return self._result


# === lookup_qr handler ========================================================


async def test_lookup_qr_handler_raises_404_for_unknown_id() -> None:
    stub = _StubLookupService(result=None)
    with pytest.raises(HTTPException) as exc:
        await lookup_qr(
            "DCQR-ZZZZZZZZ",
            make_user("dcinv-mobile-user"),
            cast(QRLookupService, stub),
        )
    assert exc.value.status_code == 404


async def test_lookup_qr_handler_returns_response_for_known_id() -> None:
    stub = _StubLookupService(
        result=QRLookupResponse(qr=_qr_info("DCQR-AAAAAAAA"), device=None, device_error=None)
    )
    result = await lookup_qr(
        "DCQR-AAAAAAAA",
        make_user("dcinv-mobile-user"),
        cast(QRLookupService, stub),
    )

    assert isinstance(result, QRLookupResponse)
    assert result.qr.id == "DCQR-AAAAAAAA"
    assert result.qr.status is QRStatus.FREE
    assert result.qr.batch.intended_site_id == 5
    assert result.device is None
    assert result.device_error is None


# === full-stack integration: response shaping + role gating ===================


def _override_lookup_service(result: QRLookupResponse | None) -> None:
    app.dependency_overrides[get_lookup_service] = lambda: _StubLookupService(result=result)


async def test_lookup_endpoint_free_code_omits_device_and_bound_retired_fields(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """FREE QR: nested ``qr`` carries the QR data; ``device`` / ``device_error``
    are None → dropped by ``response_model_exclude_none``; the QR's
    bound/retired fields are similarly dropped."""
    as_user("dcinv-mobile-user")
    _override_lookup_service(
        QRLookupResponse(qr=_qr_info("DCQR-AAAAAAAA"), device=None, device_error=None)
    )

    resp = await client.get("/api/v1/qr/DCQR-AAAAAAAA")

    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["id"] == "DCQR-AAAAAAAA"
    assert body["qr"]["status"] == "free"
    assert body["qr"]["batch"]["intended_site_id"] == 5
    assert "bound_to_device_id" not in body["qr"]
    assert "bound_at" not in body["qr"]
    assert "retired_at" not in body["qr"]
    assert "retired_reason" not in body["qr"]
    # Top-level device + device_error are None → dropped
    assert "device" not in body
    assert "device_error" not in body


async def test_lookup_endpoint_bound_code_includes_device_block(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """BOUND QR with successful device fetch: ``device`` is populated; the
    QR's ``bound_to_device_id`` / ``bound_at`` survive on the ``qr`` block."""
    as_user("dcinv-mobile-user")
    from app.services.device import DeviceData, ObjectRef, StatusRef

    device = DeviceData(
        id=42,
        name="sw-42",
        status=StatusRef(value="active", label="Active"),
        site=ObjectRef(id=1, name="DC-1"),
        rack=ObjectRef(id=7, name="R-14"),
        position=10,
        serial="X9",
        asset_tag=None,
        comments="",
        qr_id="DCQR-BBBBBBBB",
    )
    _override_lookup_service(
        QRLookupResponse(
            qr=_qr_info(
                "DCQR-BBBBBBBB",
                status=QRStatus.BOUND,
                bound_to_device_id=42,
                bound_at=_NOW,
            ),
            device=device,
            device_error=None,
        )
    )

    resp = await client.get("/api/v1/qr/DCQR-BBBBBBBB")

    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["status"] == "bound"
    assert body["qr"]["bound_to_device_id"] == 42
    assert "bound_at" in body["qr"]
    assert body["device"]["id"] == 42
    assert body["device"]["qr_id"] == "DCQR-BBBBBBBB"
    assert "device_error" not in body


async def test_lookup_endpoint_bound_code_with_netbox_down_returns_soft_error(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """BOUND QR with failed device fetch: device=None + device_error populated.
    QR data still flows back so the mobile client can show the QR even without
    the linked device (decision D)."""
    as_user("dcinv-mobile-user")
    _override_lookup_service(
        QRLookupResponse(
            qr=_qr_info(
                "DCQR-BBBBBBBB",
                status=QRStatus.BOUND,
                bound_to_device_id=42,
                bound_at=_NOW,
            ),
            device=None,
            device_error="device_unavailable",
        )
    )

    resp = await client.get("/api/v1/qr/DCQR-BBBBBBBB")

    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["status"] == "bound"
    assert body["qr"]["bound_to_device_id"] == 42
    assert "device" not in body
    assert body["device_error"] == "device_unavailable"


async def test_lookup_endpoint_retired_code_includes_retirement_fields(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    _override_lookup_service(
        QRLookupResponse(
            qr=_qr_info(
                "DCQR-CCCCCCCC",
                status=QRStatus.RETIRED,
                retired_at=_NOW,
                retired_reason="damaged label",
            ),
            device=None,
            device_error=None,
        )
    )

    resp = await client.get("/api/v1/qr/DCQR-CCCCCCCC")

    assert resp.status_code == 200
    body = resp.json()
    assert body["qr"]["status"] == "retired"
    assert body["qr"]["retired_reason"] == "damaged label"
    assert "retired_at" in body["qr"]
    assert "bound_to_device_id" not in body["qr"]
    assert "bound_at" not in body["qr"]
    assert "device" not in body


async def test_lookup_endpoint_404_via_full_stack(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    _override_lookup_service(None)

    resp = await client.get("/api/v1/qr/DCQR-MISSINGXY")
    assert resp.status_code == 404
    # Sprint 2's HTTPException shape stays: {"detail": "..."} — Task 3 doesn't
    # unify error envelopes (Q4 in the review).
    assert resp.json() == {"detail": "QR not registered"}


async def test_lookup_endpoint_succeeds_for_admin_with_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    # Real Keycloak grants admins the mobile role too; modelled here as both.
    as_user("dcinv-admin", "dcinv-mobile-user")
    _override_lookup_service(
        QRLookupResponse(qr=_qr_info("DCQR-DDDDDDDD"), device=None, device_error=None)
    )

    resp = await client.get("/api/v1/qr/DCQR-DDDDDDDD")
    assert resp.status_code == 200


async def test_lookup_endpoint_without_mobile_role_returns_403(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")  # admin only, no mobile role
    _override_lookup_service(
        QRLookupResponse(qr=_qr_info("DCQR-AAAAAAAA"), device=None, device_error=None)
    )
    resp = await client.get("/api/v1/qr/DCQR-AAAAAAAA")
    assert resp.status_code == 403


def test_get_lookup_service_builds_a_qr_lookup_service(session: Any) -> None:
    """The FastAPI dependency factory wires the real service together."""
    assert isinstance(get_lookup_service(session=session), QRLookupService)


# === smoke: real DB still seeds correctly (legacy coverage) ===================


async def test_seed_helper_inserts_qr_and_batch_round_trip() -> None:
    """Regression: the _seed helper used by other tests still works after
    Sprint 4's lookup-service refactor."""
    await _seed(_free("DCQR-EEEEEEEE"), intended_site_id=9)
    async with get_sessionmaker()() as session:
        qr = await QRCodeRepository(session).get_by_id("DCQR-EEEEEEEE")
    assert qr is not None
    assert qr.status is QRStatus.FREE
