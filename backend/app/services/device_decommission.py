"""Device decommission service — Sprint 5 Task 4, ToR §4.3.5.

Sets a device's NetBox status to ``decommissioning``. If the device has a
bound QR, that QR is retired *first* (decision C — QR-first ordering keeps
the failure modes recoverable: a stuck-retired QR with the device still in
its old status is more recoverable than the reverse).

Three-branch compensation on a device-status PATCH failure that follows a
successful QR retire (re-bind the QR via ``QRLifecycleService.bind``):

- **Happy**: QR retired → device PATCHed → success.
- **Branch 2 — rolled back**: QR retired → device PATCH failed → re-bind
  succeeded → ``DeviceDecommissionRolledBackError`` (system consistent).
- **Branch 3 — inconsistency**: QR retired → device PATCH failed → re-bind
  failed → best-effort danger journal on the device →
  ``DeviceDecommissionInconsistencyError`` (manual cleanup required).

Correction 1 — the ``decommissioning`` slug here is the assumed NetBox
convention but isn't verified against the deployed NetBox; production
deploy gates on the verification entry in ``docs/parking-lot.md``.

Correction 4 — when ``QRLifecycleService.retire`` itself raises a
``QRRetireInconsistencyError`` (Sprint 4 Branch 3), decommission **must
not** proceed: the QR is in an undefined state on NetBox, so changing
the device status would compound the inconsistency. The service logs
critical and re-raises; the endpoint translates to a structured
``QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT`` 500.
"""

from __future__ import annotations

from typing import NoReturn

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.db.repositories.qr_code import QRCodeRepository
from app.netbox.client import NetBoxClient
from app.observability.request_id import current_request_id
from app.services.device import DeviceResponse, to_device_data
from app.services.netbox_write import NetBoxWriteService
from app.services.qr.lifecycle import QRLifecycleService, QRRetireInconsistencyError

logger = structlog.get_logger()

# Correction 1: assumed NetBox status slug. Production deploy gates on
# verification — see docs/parking-lot.md "Pending NetBox configuration".
# When verified, capture the actual value from
# OPTIONS /api/dcim/devices/ → actions.POST.status.choices[].value
# and update this constant if it differs.
_DECOMMISSIONING_STATUS = "decommissioning"

_JOURNAL_PATH = "/api/extras/journal-entries/"


class DeviceDecommissionRolledBackError(Exception):
    """Device status PATCH failed after QR retire; re-bind compensation restored consistency."""

    def __init__(self, device_id: int, qr_id: str) -> None:
        super().__init__(f"Decommission of device {device_id} rolled back (QR {qr_id} re-bound)")
        self.device_id = device_id
        self.qr_id = qr_id


class DeviceDecommissionInconsistencyError(Exception):
    """Device status PATCH AND re-bind compensation failed; manual cleanup required."""

    def __init__(self, device_id: int, qr_id: str) -> None:
        super().__init__(
            f"Decommission of device {device_id} left an inconsistency "
            f"with QR {qr_id} — manual cleanup required"
        )
        self.device_id = device_id
        self.qr_id = qr_id


class DeviceDecommissionService:
    """Coordinates QR retire + device status PATCH for the decommission flow."""

    def __init__(
        self,
        netbox_client: NetBoxClient,
        session: AsyncSession,
        qr_code_repo: QRCodeRepository,
        write_service: NetBoxWriteService,
        lifecycle_service: QRLifecycleService,
    ) -> None:
        self._netbox = netbox_client
        self._session = session
        self._qr_code_repo = qr_code_repo
        self._write_service = write_service
        self._lifecycle_service = lifecycle_service

    async def decommission(
        self,
        *,
        device_id: int,
        expected_version: str,
        reason: str | None,
        user: AuthUser,
    ) -> DeviceResponse:
        """Decommission ``device_id``. QR-first ordering with re-bind compensation.

        ``reason`` is accepted for forward-compatibility but currently only
        flows into the structured log binding; Sprint 6 candidate to plumb it
        into the NetBox journal entry's comments.
        """
        # Defensive guard — same pattern as QRLifecycleService.bind/retire. The
        # decommission flow opens its own session.begin() inside
        # patch_with_attribution + nested calls, so being called inside an
        # already-active tx would conflict.
        if self._session.in_transaction():
            raise RuntimeError(
                "DeviceDecommissionService.decommission called inside an active "
                "transaction — would conflict with patch_with_attribution's audit tx"
            )

        # Step A — find bound QR (read-only). qr_one_per_device partial unique
        # index guarantees ≤1.
        async with self._session.begin():
            bound_qr = await self._qr_code_repo.find_by_bound_device_id(device_id)

        # Step B — retire bound QR if present. QR-first ordering (decision C).
        post_retire_version: str | None = None
        if bound_qr is not None:
            try:
                _retired_qr, updated_device = await self._lifecycle_service.retire(
                    qr_id=bound_qr.id,
                    expected_version=expected_version,
                    user=user,
                )
            except QRRetireInconsistencyError:
                # Correction 4: QR is in undefined state; don't make it worse
                # by changing the device status on top. Endpoint translates
                # this to QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT 500.
                logger.critical(
                    "device_decommission_aborted_qr_inconsistent",
                    qr_id=bound_qr.id,
                    device_id=device_id,
                    expected_version=expected_version,
                    reason=reason,
                    request_id=current_request_id(),
                )
                raise
            # BOUND retire path always returns the updated device dict.
            assert updated_device is not None
            post_retire_version = updated_device["last_updated"]

        # Step C — device status PATCH. ``expected_version_for_patch`` uses
        # the post-retire device version when we retired a QR (the retire
        # PATCH already bumped last_updated); else the caller-provided one.
        expected_version_for_patch = post_retire_version or expected_version
        try:
            updated = await self._write_service.patch_with_attribution(
                netbox_path=f"/api/dcim/devices/{device_id}/",
                netbox_object_type="dcim.device",
                netbox_object_id=device_id,
                entity_type="device",
                entity_id=str(device_id),
                operation="device.decommission",
                expected_version=expected_version_for_patch,
                changes={"status": _DECOMMISSIONING_STATUS},
                user=user,
            )
        except Exception as patch_err:
            if bound_qr is None:
                # Nothing to undo — let the exception propagate (endpoint maps
                # to 409/404/502 via the existing exception types).
                raise
            # Decision C compensation: QR retired but device PATCH failed;
            # re-bind the QR to restore consistency. ``post_retire_version``
            # is the deterministic OCC token (see service docstring).
            assert post_retire_version is not None
            await self._compensate_rebind_or_inconsistency(
                bound_qr_id=bound_qr.id,
                device_id=device_id,
                expected_version=expected_version,
                post_retire_version=post_retire_version,
                user=user,
                original_err=patch_err,
            )

        return DeviceResponse(data=to_device_data(updated), version=updated["last_updated"])

    async def _compensate_rebind_or_inconsistency(
        self,
        *,
        bound_qr_id: str,
        device_id: int,
        expected_version: str,
        post_retire_version: str,
        user: AuthUser,
        original_err: BaseException,
    ) -> NoReturn:
        """Branch 2/3 compensation. Always raises.

        Re-binds the QR via ``QRLifecycleService.bind`` using the captured
        post-retire device version. Branch 2 (re-bind ok) → log error +
        ``DeviceDecommissionRolledBackError``. Branch 3 (re-bind raises) →
        log critical + best-effort danger journal +
        ``DeviceDecommissionInconsistencyError``.
        """
        request_id = current_request_id()
        try:
            await self._lifecycle_service.bind(
                qr_id=bound_qr_id,
                device_id=device_id,
                expected_version=post_retire_version,
                user=user,
            )
        except Exception as rebind_err:
            # ===== Branch 3: re-bind failed =====
            logger.critical(
                "device_decommission_inconsistency_unrecoverable",
                qr_id=bound_qr_id,
                device_id=device_id,
                expected_version=expected_version,
                post_retire_version=post_retire_version,
                request_id=request_id,
                original_error=repr(original_err),
                rebind_error=repr(rebind_err),
            )
            await self._best_effort_inconsistency_journal(
                device_id=device_id,
                qr_id=bound_qr_id,
                original_err=original_err,
                rebind_err=rebind_err,
            )
            raise DeviceDecommissionInconsistencyError(device_id, bound_qr_id) from rebind_err

        # ===== Branch 2: re-bind succeeded =====
        logger.error(
            "device_decommission_db_failed_qr_recompensated",
            qr_id=bound_qr_id,
            device_id=device_id,
            expected_version=expected_version,
            post_retire_version=post_retire_version,
            request_id=request_id,
            original_error=repr(original_err),
        )
        raise DeviceDecommissionRolledBackError(device_id, bound_qr_id) from original_err

    async def _best_effort_inconsistency_journal(
        self,
        *,
        device_id: int,
        qr_id: str,
        original_err: BaseException,
        rebind_err: BaseException,
    ) -> None:
        """Branch 3 only: post a danger-kind NetBox journal entry. Swallows on failure."""
        try:
            await self._netbox.post(
                _JOURNAL_PATH,
                json={
                    "assigned_object_type": "dcim.device",
                    "assigned_object_id": device_id,
                    "kind": "danger",
                    "comments": (
                        f"INCONSISTENCY: device {device_id} decommission re-bind "
                        f"compensation failed for QR {qr_id}.\n"
                        f"NetBox device.status and/or custom_fields.qr_id may "
                        f"diverge from qr_codes. Manual cleanup required.\n"
                        f"Original error: {original_err!r}\n"
                        f"Re-bind error: {rebind_err!r}"
                    ),
                },
            )
        except Exception as journal_err:
            logger.warning(
                "device_decommission_inconsistency_journal_failed",
                qr_id=qr_id,
                device_id=device_id,
                request_id=current_request_id(),
                error=repr(journal_err),
            )


__all__ = [
    "DeviceDecommissionInconsistencyError",
    "DeviceDecommissionRolledBackError",
    "DeviceDecommissionService",
]
