"""Unit tests for app.services.qr.lookup.QRLookupService.

Sprint 4 Task 3 extension: lookup now returns the combined ``QRLookupResponse``
with the bound device fetched from NetBox for BOUND QRs. NetBox is faked with
respx (via a stubbed ``DeviceService``); the QR + batch repos are faked
in-process.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.domain.qr import QR, QRBatch, QRStatus
from app.netbox.errors import NetBoxClientError, NetBoxNotFound, NetBoxServerError
from app.services.device import DeviceService
from app.services.qr.lookup import QRLookupService

_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
_QR_ID = "DCQR-FREEKLM2"
_BOUND_QR_ID = "DCQR-BNDKLM23"
_DEVICE_ID = 99
_BATCH_ID = UUID("33333333-3333-3333-3333-333333333333")


# ---------- fakes ----------


class _FakeQRCodeRepo:
    def __init__(self) -> None:
        self.by_id: dict[str, QR] = {}

    async def get_by_id(self, qr_id: str) -> QR | None:
        return self.by_id.get(qr_id)


class _FakeQRBatchRepo:
    def __init__(self) -> None:
        self.by_id: dict[UUID, QRBatch] = {}

    async def get_by_id(self, batch_id: UUID) -> QRBatch | None:
        return self.by_id.get(batch_id)


class _StubDeviceService:
    def __init__(
        self,
        *,
        raw: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._raw = raw
        self._raises = raises
        self.calls: list[int] = []

    async def get_device_raw(self, device_id: int) -> dict[str, Any]:
        self.calls.append(device_id)
        if self._raises is not None:
            raise self._raises
        assert self._raw is not None
        return self._raw


# ---------- helpers ----------


def _batch(batch_id: UUID = _BATCH_ID) -> QRBatch:
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


def _free_qr(qr_id: str = _QR_ID) -> QR:
    return QR(
        id=qr_id,
        batch_id=_BATCH_ID,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _bound_qr(qr_id: str = _BOUND_QR_ID, device_id: int = _DEVICE_ID) -> QR:
    return QR(
        id=qr_id,
        batch_id=_BATCH_ID,
        status=QRStatus.BOUND,
        bound_to_device_id=device_id,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )


def _retired_qr(qr_id: str = _QR_ID) -> QR:
    return QR(
        id=qr_id,
        batch_id=_BATCH_ID,
        status=QRStatus.RETIRED,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=_NOW,
        retired_reason="damaged",
    )


def _device(qr_id_in_netbox: str | None = None) -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "core switch",
        "custom_fields": {"asset_tag": "A-9", "qr_id": qr_id_in_netbox},
        "last_updated": "2026-05-24T08:00:00.000000Z",
        "device_type": {
            "id": 11,
            "display": "C9300-48U",
            "manufacturer": {"id": 21, "name": "Cisco"},
            "u_height": 1,
        },
        "role": {"id": 31, "name": "Access Switch"},
        "primary_ip4": {"id": 41, "address": "192.0.2.10/24"},
        "primary_ip6": None,
    }


def _build_service(
    *,
    qr_repo: _FakeQRCodeRepo | None = None,
    batch_repo: _FakeQRBatchRepo | None = None,
    device_service: _StubDeviceService | None = None,
) -> tuple[QRLookupService, _FakeQRCodeRepo, _FakeQRBatchRepo, _StubDeviceService]:
    qr_repo = qr_repo or _FakeQRCodeRepo()
    batch_repo = batch_repo or _FakeQRBatchRepo()
    device_service = device_service or _StubDeviceService(raw=_device())
    service = QRLookupService(
        cast(QRCodeRepository, qr_repo),
        cast(QRBatchRepository, batch_repo),
        cast(DeviceService, device_service),
    )
    return service, qr_repo, batch_repo, device_service


# ---------- tests ----------


async def test_lookup_returns_none_when_qr_not_registered() -> None:
    service, _qr, _b, device = _build_service()
    assert await service.get_by_id("DCQR-MISSINGXY") is None
    assert device.calls == []  # never touched NetBox


async def test_lookup_free_qr_returns_response_with_device_null() -> None:
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    qr_repo.by_id[_QR_ID] = _free_qr()
    batch_repo.by_id[_BATCH_ID] = _batch()

    service, _q, _b, device = _build_service(qr_repo=qr_repo, batch_repo=batch_repo)
    result = await service.get_by_id(_QR_ID)

    assert result is not None
    assert result.qr.id == _QR_ID
    assert result.qr.status is QRStatus.FREE
    assert result.qr.batch.intended_site_id == 5
    assert result.device is None
    assert result.device_error is None
    assert device.calls == []  # no device fetch for FREE


async def test_lookup_retired_qr_returns_response_with_device_null() -> None:
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    qr_repo.by_id[_QR_ID] = _retired_qr()
    batch_repo.by_id[_BATCH_ID] = _batch()

    service, _q, _b, device = _build_service(qr_repo=qr_repo, batch_repo=batch_repo)
    result = await service.get_by_id(_QR_ID)

    assert result is not None
    assert result.qr.status is QRStatus.RETIRED
    assert result.device is None
    assert result.device_error is None
    assert device.calls == []


async def test_lookup_bound_qr_fetches_device_and_returns_populated_response() -> None:
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    qr_repo.by_id[_BOUND_QR_ID] = _bound_qr()
    batch_repo.by_id[_BATCH_ID] = _batch()
    stub_ds = _StubDeviceService(raw=_device())

    service, _q, _b, device = _build_service(
        qr_repo=qr_repo, batch_repo=batch_repo, device_service=stub_ds
    )
    result = await service.get_by_id(_BOUND_QR_ID)

    assert result is not None
    assert result.qr.status is QRStatus.BOUND
    assert result.device is not None
    assert result.device.id == _DEVICE_ID
    assert result.device.name == "sw-01"
    assert result.device.device_type is not None
    assert result.device.device_type.name == "C9300-48U"
    assert result.device.primary_ip4 == "192.0.2.10/24"
    assert result.device_error is None
    assert device.calls == [_DEVICE_ID]


async def test_lookup_bound_qr_qr_id_in_device_comes_from_app_db_not_netbox() -> None:
    """Decision H: even when NetBox custom_fields.qr_id says otherwise,
    the device.qr_id in the response is the app DB token."""
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    qr_repo.by_id[_BOUND_QR_ID] = _bound_qr()
    batch_repo.by_id[_BATCH_ID] = _batch()
    # NetBox returns a STALE / different qr_id — must be ignored.
    stub_ds = _StubDeviceService(raw=_device(qr_id_in_netbox="DCQR-STALEAAA"))

    service, _q, _b, _d = _build_service(
        qr_repo=qr_repo, batch_repo=batch_repo, device_service=stub_ds
    )
    result = await service.get_by_id(_BOUND_QR_ID)

    assert result is not None
    assert result.device is not None
    assert result.device.qr_id == _BOUND_QR_ID  # app DB wins


async def test_lookup_bound_qr_returns_device_unavailable_on_netbox_not_found() -> None:
    """NetBox says the bound device is gone — QR lookup must still succeed (decision D)."""
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    qr_repo.by_id[_BOUND_QR_ID] = _bound_qr()
    batch_repo.by_id[_BATCH_ID] = _batch()
    stub_ds = _StubDeviceService(raises=NetBoxNotFound("device 99 gone"))

    service, _q, _b, _d = _build_service(
        qr_repo=qr_repo, batch_repo=batch_repo, device_service=stub_ds
    )
    result = await service.get_by_id(_BOUND_QR_ID)

    assert result is not None
    assert result.qr.status is QRStatus.BOUND  # QR data still populated
    assert result.device is None
    assert result.device_error == "device_unavailable"


async def test_lookup_bound_qr_returns_device_unavailable_on_netbox_server_error() -> None:
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    qr_repo.by_id[_BOUND_QR_ID] = _bound_qr()
    batch_repo.by_id[_BATCH_ID] = _batch()
    stub_ds = _StubDeviceService(raises=NetBoxServerError("netbox down"))

    service, _q, _b, _d = _build_service(
        qr_repo=qr_repo, batch_repo=batch_repo, device_service=stub_ds
    )
    result = await service.get_by_id(_BOUND_QR_ID)

    assert result is not None
    assert result.device is None
    assert result.device_error == "device_unavailable"


async def test_lookup_bound_qr_returns_device_unavailable_on_any_netbox_client_error() -> None:
    """Base NetBoxClientError (and any future subclass) → soft-fail."""
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    qr_repo.by_id[_BOUND_QR_ID] = _bound_qr()
    batch_repo.by_id[_BATCH_ID] = _batch()
    stub_ds = _StubDeviceService(raises=NetBoxClientError("transport failure"))

    service, _q, _b, _d = _build_service(
        qr_repo=qr_repo, batch_repo=batch_repo, device_service=stub_ds
    )
    result = await service.get_by_id(_BOUND_QR_ID)

    assert result is not None
    assert result.device_error == "device_unavailable"


async def test_lookup_bound_qr_with_null_device_id_raises_runtime_error() -> None:
    """Defensive: BOUND with bound_to_device_id=None is forbidden by the
    qr_state_consistency CHECK constraint and ``QR.__post_init__``. The
    runtime guard fires under ``python -O`` (where assert is stripped)."""
    qr_repo = _FakeQRCodeRepo()
    batch_repo = _FakeQRBatchRepo()
    broken = _bound_qr()
    object.__setattr__(broken, "bound_to_device_id", None)
    qr_repo.by_id[_BOUND_QR_ID] = broken
    batch_repo.by_id[_BATCH_ID] = _batch()

    service, _q, _b, device = _build_service(qr_repo=qr_repo, batch_repo=batch_repo)
    with pytest.raises(RuntimeError, match="bound_to_device_id"):
        await service.get_by_id(_BOUND_QR_ID)
    assert device.calls == []  # never reached the fetch


def test_qr_lookup_response_exists_at_module_root() -> None:
    """Smoke test: QRLookupResponse is exported from app.services.qr.lookup."""
    from app.services.qr.lookup import QRLookupResponse

    resp = QRLookupResponse(qr=_q_info(), device=None, device_error=None)
    assert resp.qr.id == _QR_ID
    assert resp.device is None


def _q_info() -> Any:
    """Tiny helper for the smoke test."""
    from app.services.qr.lookup import to_qr_info

    return to_qr_info(_free_qr(), _batch())
