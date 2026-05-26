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

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_session
from app.netbox.client import get_netbox_client
from app.services.device import DeviceService, to_device_data
from app.services.netbox_write import NetBoxWriteService, WriteConflictError
from app.services.qr.lifecycle import (
    MissingVersionError,
    QRAlreadyBoundError,
    QRBindInconsistencyError,
    QRBindRolledBackError,
    QRLifecycleService,
    QRNotFoundError,
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
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
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    lifecycle: QRLifecycleService = Depends(get_lifecycle_service),
    session: AsyncSession = Depends(get_session),
) -> QRLookupResponse | JSONResponse:
    """Atomic free→bound transition with NetBox attribution. Architecture §4.

    On success returns the combined QR+device response. On any error returns a
    structured ``{"error": {"code": ...}}`` body — see the table in
    ``docs/sprint-4.md`` Task 1 step 8.
    """
    try:
        bound_qr, device_dict = await lifecycle.bind(
            qr_id=qr_id,
            device_id=request.device_id,
            expected_version=request.version,
            user=user,
        )
    except QRNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"code": "QR_NOT_FOUND", "message": f"QR {qr_id} not registered"}},
        )
    except QRStateConflictError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "QR_STATE_CONFLICT",
                    "message": f"QR is in {exc.current_status.value} state — cannot bind",
                    "current_status": exc.current_status.value,
                }
            },
        )
    except WriteConflictError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": to_device_data(exc.current_object).model_dump(),
                    "current_version": exc.current_version,
                }
            },
        )
    except QRAlreadyBoundError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "QR_ALREADY_BOUND",
                    "message": f"Device {exc.device_id} already has a bound QR",
                }
            },
        )
    except QRBindRolledBackError:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "QR_BIND_ROLLED_BACK",
                    "message": "Bind failed (rolled back)",
                }
            },
        )
    except QRBindInconsistencyError:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "QR_BIND_INCONSISTENCY",
                    "message": "Bind failed, manual cleanup required",
                }
            },
        )

    # Build the combined response: QR (with batch info) + device. Decision H:
    # device.qr_id is sourced from the app DB (the QR we just bound), not from
    # NetBox — must be passed explicitly so the bind response is consistent
    # with what GET /api/v1/qr/{id} would return for the same now-bound QR.
    batch = await QRBatchRepository(session).get_by_id(bound_qr.batch_id)
    # The qr_codes.batch_id FK guarantees the batch row exists.
    assert batch is not None
    return QRLookupResponse(
        qr=to_qr_info(bound_qr, batch),
        device=to_device_data(device_dict, qr_id=bound_qr.id),
        device_error=None,
    )


@router.post(
    "/{qr_id}/retire",
    response_model=QRRetireResponse,
    response_model_exclude_none=True,
)
async def retire_qr(
    qr_id: str,
    request: QRRetireRequest,
    user: AuthUser = Depends(require_role("dcinv-admin")),
    lifecycle: QRLifecycleService = Depends(get_lifecycle_service),
    session: AsyncSession = Depends(get_session),
) -> QRRetireResponse | JSONResponse:
    """Retire a QR. FREE→RETIRED is DB-only; BOUND→RETIRED clears
    ``custom_fields.qr_id`` on the bound device with the same three-branch
    compensation as bind. Role ``dcinv-admin`` (decision I) — retire is
    destructive; safer default than ``dcinv-mobile-user``.
    """
    try:
        retired = await lifecycle.retire(
            qr_id=qr_id,
            expected_version=request.version,
            user=user,
        )
    except QRNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"code": "QR_NOT_FOUND", "message": f"QR {qr_id} not registered"}},
        )
    except QRStateConflictError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "QR_STATE_CONFLICT",
                    "message": f"QR is in {exc.current_status.value} state — cannot retire",
                    "current_status": exc.current_status.value,
                }
            },
        )
    except MissingVersionError:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VERSION_REQUIRED",
                    "message": "BOUND QR retire requires the device's expected version",
                }
            },
        )
    except WriteConflictError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": to_device_data(exc.current_object).model_dump(),
                    "current_version": exc.current_version,
                }
            },
        )
    except QRRetireRolledBackError:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "QR_RETIRE_ROLLED_BACK",
                    "message": "Retire failed (rolled back)",
                }
            },
        )
    except QRRetireInconsistencyError:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "QR_RETIRE_INCONSISTENCY",
                    "message": "Retire failed, manual cleanup required",
                }
            },
        )

    batch = await QRBatchRepository(session).get_by_id(retired.batch_id)
    assert batch is not None
    return QRRetireResponse(qr=to_qr_info(retired, batch))
