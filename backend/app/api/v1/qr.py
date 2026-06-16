"""QR endpoints. ToR §8.2.

- ``GET /api/v1/qr/{qr_id}`` — resolve a scanned QR id to its current state.
  Sprint 2 shape (just the QR + batch) until Task 3 extends it to the combined
  QR+device response. Role ``dcinv-mobile-user``.
- ``POST /api/v1/qr/{qr_id}/bind`` — Sprint 4 Task 1: atomic free→bound
  transition with NetBox attribution, returning the combined QR+device
  response. Role ``dcinv-mobile-user``.

``response_model_exclude_none`` drops fields that don't apply to the QR's
state (e.g. ``retired_reason`` on a non-retired code) so the mobile client
never sees NULL noise.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1._helpers import netbox_validation_error_response
from app.auth.dependencies import AuthUser, require_role, require_role_with_active_shift
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_session, get_sessionmaker
from app.netbox.client import get_netbox_client
from app.netbox.errors import NetBoxNotFound, NetBoxValidationError
from app.services.device import DeviceService, to_device_data
from app.services.idempotency import with_optional_idempotency_outer
from app.services.netbox_write import NetBoxWriteService, WriteConflictError
from app.services.qr.lifecycle import (
    DeviceAlreadyBoundError,
    MissingVersionError,
    QRAlreadyBoundError,
    QRBindInconsistencyError,
    QRBindRolledBackError,
    QRLifecycleService,
    QRNotFoundError,
    QRRebindInconsistencyError,
    QRRebindRolledBackError,
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
    QRUnbindInconsistencyError,
    QRUnbindRolledBackError,
    SameDeviceError,
)
from app.services.qr.lookup import (
    QRInfo,  # kept for backward-compat re-export; deletable in Sprint 5
    QRLookupResponse,
    QRLookupService,
    to_qr_info,
)

router = APIRouter()


class QRBindRequest(BaseModel):
    """``POST /api/v1/qr/{qr_id}/bind`` payload.

    ``version`` is the device's expected ``last_updated`` from a prior
    ``GET /api/v1/devices/{device_id}`` — the backend re-reads and compares
    (Sprint 3 decision A). ``extra='forbid'`` rejects unknown keys.
    """

    model_config = ConfigDict(extra="forbid")

    device_id: int
    version: str


class QRRebindRequest(BaseModel):
    """``POST /api/v1/qr/{qr_id}/rebind`` payload (docs/backend-tz-qr-rebind.md).

    ``device_id`` is the NEW device; ``version`` is that device's expected
    ``last_updated`` (the backend re-reads + compares, Sprint 3 decision A).
    ``reason`` is mandatory (1..2000) — the rebind moves a label between
    physical devices, so the WHY is required for the audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    device_id: int
    version: str
    reason: str = Field(min_length=1, max_length=2000)


class QRUnbindRequest(BaseModel):
    """``POST /api/v1/qr/{qr_id}/unbind`` payload (docs/backend-tz-qr-unbind.md).

    ``version`` is the bound device's expected ``last_updated`` (OCC when the
    backend clears its ``qr_id``). ``reason`` is mandatory (1..2000) — unbind
    returns a label to the free pool, the WHY is required for the audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    reason: str = Field(min_length=1, max_length=2000)


class QRRetireRequest(BaseModel):
    """``POST /api/v1/qr/{qr_id}/retire`` payload.

    ``version`` is the device's expected ``last_updated`` — required only for
    BOUND→RETIRED (the backend's NetBox PATCH clears ``custom_fields.qr_id``
    with optimistic concurrency). FREE→RETIRED ignores the field silently
    (decision: a stray version on a FREE retire is harmless overhead, not a
    user error worth a 422 — the body is otherwise valid).
    """

    model_config = ConfigDict(extra="forbid")

    version: str | None = None


class QRRetireResponse(BaseModel):
    """``POST /api/v1/qr/{qr_id}/retire`` success body. No device — the
    binding is gone."""

    qr: QRInfo


def get_lifecycle_service(
    session: AsyncSession = Depends(get_session),
) -> QRLifecycleService:
    """Build the per-request QR lifecycle service.

    Shares one ``AuditLogRepository`` between the lifecycle service and its
    inner ``NetBoxWriteService`` so the compensation audit row (Task 1 step 4)
    and the regular SUCCESS audit row from ``patch_with_attribution`` both
    target the same per-request session.
    """
    netbox = get_netbox_client()
    audit_repo = AuditLogRepository(session)
    return QRLifecycleService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=audit_repo,
        write_service=NetBoxWriteService(netbox, session, audit_repo),
    )


def get_lookup_service(
    session: AsyncSession = Depends(get_session),
) -> QRLookupService:
    """Build the per-request QR lookup service.

    Constructed via FastAPI dependency so endpoint tests can override the
    whole service in one ``app.dependency_overrides`` call (note 2 in Task 3
    review). The NetBox client is the process-wide singleton; the repositories
    and ``DeviceService`` are per-request and bound to the same session.
    """
    return QRLookupService(
        QRCodeRepository(session),
        QRBatchRepository(session),
        DeviceService(get_netbox_client()),
    )


@router.get(
    "/{qr_id}",
    response_model=QRLookupResponse,
    response_model_exclude_none=True,
)
async def lookup_qr(
    qr_id: str,
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    service: QRLookupService = Depends(get_lookup_service),
) -> QRLookupResponse:
    """Return the combined QR+device response. 404 if the id is not registered.

    Sprint 4 Task 3: the response shape changed from Sprint 2's flat
    ``QRInfo`` to the nested ``QRLookupResponse`` ``{qr, device,
    device_error}``. Mobile clients must adapt — captured in the Sprint 4
    work-log. For FREE/RETIRED QRs ``device`` and ``device_error`` are
    ``None`` (dropped by ``response_model_exclude_none``); for BOUND QRs
    the device is fetched from NetBox, with soft-fail to
    ``device_error="device_unavailable"`` on NetBox errors (decision D).
    """
    result = await service.get_by_id(qr_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="QR not registered")
    return result


@router.post(
    "/{qr_id}/bind",
    response_model=QRLookupResponse,
    response_model_exclude_none=True,
)
async def bind_qr(
    qr_id: str,
    request: QRBindRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-mobile-user")),
    lifecycle: QRLifecycleService = Depends(get_lifecycle_service),
    session: AsyncSession = Depends(get_session),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Atomic free→bound transition with NetBox attribution. Architecture §4.

    On success returns the combined QR+device response. On any error returns a
    structured ``{"error": {"code": ...}}`` body — see the table in
    ``docs/sprint-4.md`` Task 1 step 8.

    Sprint 9 Task 0: optional ``Idempotency-Key`` header — see
    ``with_optional_idempotency_outer`` and the mobile-api-guide for the
    contract.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        try:
            bound_qr, device_dict = await lifecycle.bind(
                qr_id=qr_id,
                device_id=request.device_id,
                expected_version=request.version,
                user=user,
            )
        except QRNotFoundError:
            return status.HTTP_404_NOT_FOUND, {
                "error": {"code": "QR_NOT_FOUND", "message": f"QR {qr_id} not registered"}
            }
        except QRStateConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "QR_STATE_CONFLICT",
                    "message": f"QR is in {exc.current_status.value} state — cannot bind",
                    "current_status": exc.current_status.value,
                }
            }
        except WriteConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": to_device_data(exc.current_object).model_dump(),
                    "current_version": exc.current_version,
                }
            }
        except QRAlreadyBoundError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "QR_ALREADY_BOUND",
                    "message": f"Device {exc.device_id} already has a bound QR",
                }
            }
        except QRBindRolledBackError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_BIND_ROLLED_BACK",
                    "message": "Bind failed (rolled back)",
                }
            }
        except QRBindInconsistencyError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_BIND_INCONSISTENCY",
                    "message": "Bind failed, manual cleanup required",
                }
            }
        except NetBoxValidationError as exc:
            # Sprint 7 Task 5: rare in practice (qr_one_per_device index +
            # NetBoxNotFound handle the common conflicts), but if NetBox does
            # reject the device PATCH with a 4xx, 502 is misleading.
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the bind"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))

        # Build the combined response: QR (with batch info) + device.
        batch = await QRBatchRepository(session).get_by_id(bound_qr.batch_id)
        assert batch is not None
        return status.HTTP_200_OK, QRLookupResponse(
            qr=to_qr_info(bound_qr, batch),
            device=to_device_data(device_dict, qr_id=bound_qr.id),
            device_error=None,
        ).model_dump(mode="json", exclude_none=True)

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload={"qr_id": qr_id, **request.model_dump(mode="json")},
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)


@router.post(
    "/{qr_id}/rebind",
    response_model=QRLookupResponse,
    response_model_exclude_none=True,
)
async def rebind_qr(
    qr_id: str,
    request: QRRebindRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-mobile-user")),
    lifecycle: QRLifecycleService = Depends(get_lifecycle_service),
    session: AsyncSession = Depends(get_session),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Move a BOUND QR to a different device (docs/backend-tz-qr-rebind.md).

    Field case: a label stuck to a rack frame must follow the device that was
    physically swapped, without printing a new sticker. Role
    ``dcinv-mobile-user`` (field op) — the mandatory ``reason`` + full audit
    trail compensate for not gating on admin.

    On success returns the combined QR+device response (device = the new
    binding). Errors per the TZ table — see ``docs/mobile-api-guide.md``.
    Sprint 9 Task 0: optional ``Idempotency-Key`` header.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        try:
            rebound_qr, device_dict = await lifecycle.rebind(
                qr_id=qr_id,
                new_device_id=request.device_id,
                expected_version=request.version,
                reason=request.reason,
                user=user,
            )
        except QRNotFoundError:
            return status.HTTP_404_NOT_FOUND, {
                "error": {"code": "QR_NOT_FOUND", "message": f"QR {qr_id} not registered"}
            }
        except QRStateConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "QR_NOT_BOUND",
                    "message": (
                        f"QR is in {exc.current_status.value} state — only a BOUND "
                        "QR can be rebound"
                    ),
                    "current_status": exc.current_status.value,
                }
            }
        except SameDeviceError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "SAME_DEVICE",
                    "message": f"QR is already bound to device {exc.device_id}",
                }
            }
        except NetBoxNotFound:
            return status.HTTP_404_NOT_FOUND, {
                "error": {
                    "code": "DEVICE_NOT_FOUND",
                    "message": f"Device {request.device_id} not found in NetBox",
                }
            }
        except DeviceAlreadyBoundError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "DEVICE_ALREADY_BOUND",
                    "message": (
                        f"Device {exc.device_id} already has QR {exc.existing_qr_id}"
                    ),
                    "existing_qr_id": exc.existing_qr_id,
                }
            }
        except WriteConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": to_device_data(exc.current_object).model_dump(),
                    "current_version": exc.current_version,
                }
            }
        except QRRebindRolledBackError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_REBIND_ROLLED_BACK",
                    "message": "Rebind failed (rolled back)",
                }
            }
        except QRRebindInconsistencyError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_REBIND_INCONSISTENCY",
                    "message": "Rebind failed, manual cleanup required",
                }
            }
        except NetBoxValidationError as exc:
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the rebind"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))

        batch = await QRBatchRepository(session).get_by_id(rebound_qr.batch_id)
        assert batch is not None
        return status.HTTP_200_OK, QRLookupResponse(
            qr=to_qr_info(rebound_qr, batch),
            device=to_device_data(device_dict, qr_id=rebound_qr.id),
            device_error=None,
        ).model_dump(mode="json", exclude_none=True)

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload={"qr_id": qr_id, **request.model_dump(mode="json")},
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)


@router.post(
    "/{qr_id}/retire",
    response_model=QRRetireResponse,
    response_model_exclude_none=True,
)
async def retire_qr(
    qr_id: str,
    request: QRRetireRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    lifecycle: QRLifecycleService = Depends(get_lifecycle_service),
    session: AsyncSession = Depends(get_session),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Retire a QR. FREE→RETIRED is DB-only; BOUND→RETIRED clears
    ``custom_fields.qr_id`` on the bound device with the same three-branch
    compensation as bind. Role ``dcinv-admin`` (decision I) — retire is
    destructive; safer default than ``dcinv-mobile-user``.

    Sprint 9 Task 0: optional ``Idempotency-Key`` header.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        try:
            # Endpoint only needs the retired QR; the updated-device dict is for
            # Sprint 5 Task 4 (decommission OCC chain).
            retired, _ = await lifecycle.retire(
                qr_id=qr_id,
                expected_version=request.version,
                user=user,
            )
        except QRNotFoundError:
            return status.HTTP_404_NOT_FOUND, {
                "error": {"code": "QR_NOT_FOUND", "message": f"QR {qr_id} not registered"}
            }
        except QRStateConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "QR_STATE_CONFLICT",
                    "message": f"QR is in {exc.current_status.value} state — cannot retire",
                    "current_status": exc.current_status.value,
                }
            }
        except MissingVersionError:
            return status.HTTP_422_UNPROCESSABLE_ENTITY, {
                "error": {
                    "code": "VERSION_REQUIRED",
                    "message": "BOUND QR retire requires the device's expected version",
                }
            }
        except WriteConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": to_device_data(exc.current_object).model_dump(),
                    "current_version": exc.current_version,
                }
            }
        except QRRetireRolledBackError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_RETIRE_ROLLED_BACK",
                    "message": "Retire failed (rolled back)",
                }
            }
        except QRRetireInconsistencyError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_RETIRE_INCONSISTENCY",
                    "message": "Retire failed, manual cleanup required",
                }
            }
        except NetBoxValidationError as exc:
            # Sprint 7 Task 5: included for consistency.
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the retire"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))

        batch = await QRBatchRepository(session).get_by_id(retired.batch_id)
        assert batch is not None
        return status.HTTP_200_OK, QRRetireResponse(
            qr=to_qr_info(retired, batch)
        ).model_dump(mode="json", exclude_none=True)

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload={"qr_id": qr_id, **request.model_dump(mode="json")},
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)


@router.post(
    "/{qr_id}/unbind",
    response_model=QRRetireResponse,
    response_model_exclude_none=True,
)
async def unbind_qr(
    qr_id: str,
    request: QRUnbindRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-mobile-user")),
    lifecycle: QRLifecycleService = Depends(get_lifecycle_service),
    session: AsyncSession = Depends(get_session),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Unbind a BOUND QR, returning it to FREE (docs/backend-tz-qr-unbind.md).

    Field case: a label scanned onto the wrong device (test/mistake/removed)
    needs to go back into the free pool — without burning it (retire) or
    moving it to a specific device (rebind). Clears the device's
    ``custom_fields.qr_id`` with OCC, transitions the registry to FREE, and
    writes one ``qr.unbind`` audit row (former device id + reason).

    Returns ``{qr}`` (no device — the binding is gone). Role
    ``dcinv-mobile-user`` + active shift; mandatory ``reason``. Optional
    ``Idempotency-Key``.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        try:
            freed = await lifecycle.unbind(
                qr_id=qr_id,
                expected_version=request.version,
                reason=request.reason,
                user=user,
            )
        except QRNotFoundError:
            return status.HTTP_404_NOT_FOUND, {
                "error": {"code": "QR_NOT_FOUND", "message": f"QR {qr_id} not registered"}
            }
        except QRStateConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "QR_NOT_BOUND",
                    "message": (
                        f"QR is in {exc.current_status.value} state — only a BOUND "
                        "QR can be unbound"
                    ),
                    "current_status": exc.current_status.value,
                }
            }
        except WriteConflictError as exc:
            # Consistent with bind/rebind/retire: DEVICE_CONFLICT carries the
            # current state + version so the client can re-read and retry.
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": to_device_data(exc.current_object).model_dump(),
                    "current_version": exc.current_version,
                }
            }
        except QRUnbindRolledBackError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_UNBIND_ROLLED_BACK",
                    "message": "Unbind failed (rolled back)",
                }
            }
        except QRUnbindInconsistencyError:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_UNBIND_INCONSISTENCY",
                    "message": "Unbind failed, manual cleanup required",
                }
            }
        except NetBoxValidationError as exc:
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the unbind"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))

        batch = await QRBatchRepository(session).get_by_id(freed.batch_id)
        assert batch is not None
        return status.HTTP_200_OK, QRRetireResponse(
            qr=to_qr_info(freed, batch)
        ).model_dump(mode="json", exclude_none=True)

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload={"qr_id": qr_id, **request.model_dump(mode="json")},
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)
