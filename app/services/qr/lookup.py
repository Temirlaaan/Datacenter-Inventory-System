"""QR lookup. ToR §4.3 (QR scan flow).

``QRLookupService.get_by_id`` resolves a scanned QR id to its current state plus
the batch metadata (intended site/location/rack). The Sprint 4 combined
``QRLookupResponse`` carries the bound device alongside the QR — populated by
Task 1's bind endpoint from ``patch_with_attribution``'s return value, and (in
Task 3) by extending this service to fetch the bound device for GET.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.domain.qr import QR, QRBatch, QRStatus
from app.netbox.errors import NetBoxClientError
from app.services.device import DeviceData, DeviceService, to_device_data


class QRLookupBatchInfo(BaseModel):
    """Intended placement carried by the QR's batch."""

    intended_site_id: int | None
    intended_location_id: int | None
    intended_rack_id: int | None


class QRInfo(BaseModel):
    """The QR portion of the combined ``QRLookupResponse``.

    Fields outside the QR's current state are absent from the wire format via
    ``response_model_exclude_none=True`` on the endpoint (e.g. a FREE QR omits
    ``bound_to_device_id``).
    """

    id: str
    status: QRStatus
    batch: QRLookupBatchInfo
    bound_to_device_id: int | None = None
    bound_at: datetime | None = None
    retired_at: datetime | None = None
    retired_reason: str | None = None


class QRLookupResponse(BaseModel):
    """Combined QR + bound-device response (Sprint 4 decision E, ToR §4.3.3).

    Used by Task 1's bind endpoint (device always populated from
    ``patch_with_attribution``'s return) and Task 3's GET endpoint (device
    fetched from NetBox for BOUND QRs, ``device=None`` for FREE/RETIRED).
    ``device_error`` is a categorical string (``"device_unavailable"`` for
    now) — never a free-form message.
    """

    qr: QRInfo
    device: DeviceData | None = None
    device_error: str | None = None


def to_qr_info(qr: QR, batch: QRBatch) -> QRInfo:
    """Project a domain ``QR`` + its ``QRBatch`` onto the wire-format ``QRInfo``."""
    return QRInfo(
        id=qr.id,
        status=qr.status,
        batch=QRLookupBatchInfo(
            intended_site_id=batch.intended_site_id,
            intended_location_id=batch.intended_location_id,
            intended_rack_id=batch.intended_rack_id,
        ),
        bound_to_device_id=qr.bound_to_device_id,
        bound_at=qr.bound_at,
        retired_at=qr.retired_at,
        retired_reason=qr.retired_reason,
    )


class QRLookupService:
    """Lookup a QR code by its id, returning the combined QR+device response.

    Sprint 4 Task 3: for BOUND QRs, fetches the bound NetBox device via
    ``DeviceService.get_device_raw`` and folds it into the response. FREE and
    RETIRED QRs return ``device=None``. NetBox failures on the device fetch
    are swallowed and surfaced as ``device_error="device_unavailable"`` so a
    NetBox outage doesn't break QR lookup (decision D — the QR lives in the
    app DB and must always be readable).
    """

    def __init__(
        self,
        qr_code_repo: QRCodeRepository,
        qr_batch_repo: QRBatchRepository,
        device_service: DeviceService,
    ) -> None:
        self._qr_code_repo = qr_code_repo
        self._qr_batch_repo = qr_batch_repo
        self._device_service = device_service

    async def get_by_id(self, qr_id: str) -> QRLookupResponse | None:
        """Return the combined QR+device response, or ``None`` if QR not registered.

        Note: Sprint 2 shipped a simpler ``QRLookupResult`` from this method; the
        Sprint 4 Task 3 extension returns the combined ``QRLookupResponse``
        instead. Mobile clients must adapt — captured in the Sprint 4 work-log.
        """
        qr = await self._qr_code_repo.get_by_id(qr_id)
        if qr is None:
            return None
        batch = await self._qr_batch_repo.get_by_id(qr.batch_id)
        # The qr_codes.batch_id FK guarantees the batch row exists.
        assert batch is not None
        qr_info = to_qr_info(qr, batch)

        if qr.status is not QRStatus.BOUND:
            # FREE or RETIRED — no device to fetch.
            return QRLookupResponse(qr=qr_info, device=None, device_error=None)

        # BOUND: fetch the device, soft-fail on NetBox errors.
        if qr.bound_to_device_id is None:
            # NOTE 1 / Correction 1: RuntimeError over assert (survives `python -O`).
            # The qr_state_consistency CHECK constraint guarantees BOUND rows have a
            # non-null bound_to_device_id, so this branch is unreachable under
            # correct DB state. Same pattern as QRLifecycleService.retire().
            raise RuntimeError(
                f"BOUND QR {qr.id} has no bound_to_device_id; "
                "domain invariant violated (qr_state_consistency CHECK "
                "should prevent this)"
            )
        try:
            device_raw = await self._device_service.get_device_raw(qr.bound_to_device_id)
        except NetBoxClientError:
            # Catches NetBoxNotFound, NetBoxServerError, NetBoxTimeout — anything
            # the client raises. Soft fail per decision D.
            return QRLookupResponse(qr=qr_info, device=None, device_error="device_unavailable")
        device_data = to_device_data(device_raw, qr_id=qr.id)
        return QRLookupResponse(qr=qr_info, device=device_data, device_error=None)
