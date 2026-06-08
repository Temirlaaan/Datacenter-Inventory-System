"""Device endpoints. Architecture §3.2, ToR §4.3.

- ``GET /api/v1/devices/{device_id}`` — read a device from NetBox with its
  optimistic-concurrency ``version``.
- ``PATCH /api/v1/devices/{device_id}`` — update editable fields, gated by the
  client's ``If-Unmodified-Since`` header. 409 on a stale version.
- ``POST /api/v1/devices/`` (Sprint 5 Task 2) — create a new device.
  Mobile entry point is ToR §4.3.2's "Create New Device" button on the
  Free QR screen. Translates NetBox 4xx validation errors to structured
  422 responses (Sprint 5 Correction 2).
- ``POST /api/v1/devices/{device_id}/comments`` (Sprint 5 Task 3) — append
  a NetBox journal entry to the device. ToR §4.3.6.
- ``POST /api/v1/devices/{device_id}/decommission`` (Sprint 5 Task 4) —
  decommission a device with QR-first ordering; admin-only. ToR §4.3.5.

Reads + comment + create require ``dcinv-mobile-user``; decommission
requires ``dcinv-admin`` (Sprint 5 decision G).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1._helpers import netbox_validation_error_response
from app.auth.dependencies import AuthUser, require_role, require_role_with_active_shift
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_session, get_sessionmaker
from app.netbox.client import get_netbox_client
from app.netbox.errors import NetBoxValidationError
from app.services.comment import CommentService
from app.services.device import (
    DeviceCreateRequest,
    DeviceResponse,
    DeviceService,
    DeviceUpdateRequest,
    to_device_data,
    to_netbox_changes,
    to_netbox_create_payload,
)
from app.services.device_decommission import (
    DeviceDecommissionInconsistencyError,
    DeviceDecommissionRolledBackError,
    DeviceDecommissionService,
)
from app.services.idempotency import with_optional_idempotency_outer
from app.services.netbox_write import NetBoxWriteService, WriteConflictError
from app.services.qr.lifecycle import (
    QRLifecycleService,
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
)

router = APIRouter()


def get_device_service() -> DeviceService:
    """Build the device read service from the process-wide NetBox client."""
    return DeviceService(get_netbox_client())


def get_write_service(session: AsyncSession = Depends(get_session)) -> NetBoxWriteService:
    """Build the three-record-write service from the per-request session."""
    return NetBoxWriteService(get_netbox_client(), session, AuditLogRepository(session))


def get_comment_service(
    write_service: NetBoxWriteService = Depends(get_write_service),
) -> CommentService:
    """Build the per-request CommentService (Sprint 5 Task 3)."""
    return CommentService(write_service)


def get_decommission_service(
    session: AsyncSession = Depends(get_session),
    write_service: NetBoxWriteService = Depends(get_write_service),
) -> DeviceDecommissionService:
    """Build the per-request DeviceDecommissionService (Sprint 5 Task 4).

    Reuses ``write_service`` (and thus its bound ``AuditLogRepository``) so
    every audit row from this request — Step B's ``qr.retire`` row, Step C's
    ``device.decommission`` row, any compensation rows — lands through the
    same per-request session.
    """
    netbox = get_netbox_client()
    audit_repo = AuditLogRepository(session)
    qr_code_repo = QRCodeRepository(session)
    lifecycle = QRLifecycleService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=qr_code_repo,
        audit_log_repo=audit_repo,
        write_service=write_service,
    )
    return DeviceDecommissionService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=qr_code_repo,
        write_service=write_service,
        lifecycle_service=lifecycle,
    )


class AddCommentRequest(BaseModel):
    """``POST /api/v1/devices/{id}/comments`` payload. Sprint 5 Task 3.

    ``max_length=2000`` (Sprint 5 Correction 3): per-incident notes (RMA
    numbers, ticket refs, observation context) — bounds audit_log JSONB
    growth (50 ops/day * 2k chars = 100k/day vs 500k at 10k). NetBox
    journal `comments` supports more, but 2k is the policy cap.
    """

    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1, max_length=2000)


class AddCommentResponse(BaseModel):
    """201 response — just the journal entry id."""

    id: int


class DeviceDecommissionRequest(BaseModel):
    """``POST /api/v1/devices/{id}/decommission`` payload. Sprint 5 Task 4."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=2000)


@router.post("/", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(
    request: DeviceCreateRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-mobile-user")),
    write_service: NetBoxWriteService = Depends(get_write_service),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Create a new NetBox device. ToR §4.3.2 (Create New Device flow).

    On NetBox validation failure (4xx — duplicate name, position collision,
    invalid status, etc.) translates to a structured 422 with
    ``error.code="NETBOX_VALIDATION_ERROR"`` carrying NetBox's actual message
    so the mobile client can surface it (Sprint 5 Correction 2). Other
    NetBox errors (404 on a referenced FK, 5xx) flow through ``main.py``'s
    global handlers (404 / 502).

    Sprint 9 Task 0: optional ``Idempotency-Key`` header. Especially
    important for create — NetBox has no native dedupe, so a retried
    create without idempotency creates two devices.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        payload = to_netbox_create_payload(request)
        try:
            created = await write_service.post_with_attribution(
                netbox_path="/api/dcim/devices/",
                netbox_object_type="dcim.device",
                netbox_object_id=None,
                entity_type="device",
                entity_id=None,
                operation="device.create",
                payload=payload,
                user=user,
                attach_journal=True,
            )
        except NetBoxValidationError as exc:
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the create request"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))
        return status.HTTP_201_CREATED, DeviceResponse(
            data=to_device_data(created), version=created["last_updated"]
        ).model_dump(mode="json")

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload=request.model_dump(mode="json"),
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)


@router.post(
    "/{device_id}/comments",
    response_model=AddCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    device_id: int,
    request: AddCommentRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-mobile-user")),
    comment_service: CommentService = Depends(get_comment_service),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Append a NetBox journal entry to ``device_id``. Sprint 5 Task 3, ToR §4.3.6.

    NetBoxNotFound flows through ``main.py``'s 404 handler. Sprint 7 Task 5:
    NetBoxValidationError (other 4xx) translates to the structured 422 here;
    other NetBox errors (5xx, timeout) flow through ``main.py``'s 502 handler.

    Sprint 9 Task 0: optional ``Idempotency-Key`` header. Critical for
    comments — NetBox doesn't dedupe journal entries, so a retried add
    without idempotency leaves two identical comment rows.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        try:
            created = await comment_service.add_comment(
                device_id=device_id,
                comment=request.comment,
                user=user,
            )
        except NetBoxValidationError as exc:
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the comment"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))
        return status.HTTP_201_CREATED, AddCommentResponse(
            id=created["id"]
        ).model_dump(mode="json")

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload={"device_id": device_id, **request.model_dump(mode="json")},
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)


@router.post("/{device_id}/decommission", response_model=DeviceResponse)
async def decommission_device(
    device_id: int,
    request: DeviceDecommissionRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    service: DeviceDecommissionService = Depends(get_decommission_service),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Decommission ``device_id``. Sprint 5 Task 4, ToR §4.3.5.

    Role ``dcinv-admin`` (Sprint 5 decision G). QR-first ordering: if the
    device has a bound QR, retire it before changing the device's NetBox
    status. The compensation logic re-binds the QR if the device PATCH
    fails after a successful retire; see
    ``app/services/device_decommission.py``.

    Sprint 9 Task 0: optional ``Idempotency-Key`` header.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        try:
            decom = await service.decommission(
                device_id=device_id,
                expected_version=request.version,
                reason=request.reason,
                user=user,
            )
        except QRStateConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "QR_STATE_CONFLICT",
                    "message": (
                        f"Bound QR is in {exc.current_status.value} state — "
                        "cannot retire as part of decommission"
                    ),
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
        except QRRetireRolledBackError as exc:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_RETIRE_ROLLED_BACK",
                    "qr_id": exc.qr_id,
                    "device_id": exc.device_id,
                    "message": (
                        "QR retire failed and was rolled back; decommission did not proceed."
                    ),
                }
            }
        except DeviceDecommissionRolledBackError as exc:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "DECOMMISSION_ROLLED_BACK",
                    "device_id": exc.device_id,
                    "qr_id": exc.qr_id,
                    "message": "Decommission failed (rolled back).",
                }
            }
        except DeviceDecommissionInconsistencyError as exc:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "DECOMMISSION_INCONSISTENCY",
                    "device_id": exc.device_id,
                    "qr_id": exc.qr_id,
                    "message": "Decommission failed, manual cleanup required.",
                }
            }
        except QRRetireInconsistencyError as exc:
            return status.HTTP_500_INTERNAL_SERVER_ERROR, {
                "error": {
                    "code": "QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT",
                    "qr_id": exc.qr_id,
                    "device_id": exc.device_id,
                    "message": (
                        "QR is in inconsistent state; manual cleanup required "
                        "before retrying decommission"
                    ),
                }
            }
        except NetBoxValidationError as exc:
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the decommission"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))
        return status.HTTP_200_OK, decom.model_dump(mode="json")

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload={"device_id": device_id, **request.model_dump(mode="json")},
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)


@router.get("/{device_id}", response_model=DeviceResponse)
async def read_device(
    device_id: int,
    service: DeviceService = Depends(get_device_service),
    _user: AuthUser = Depends(require_role("dcinv-mobile-user")),
) -> DeviceResponse:
    """Fetch a device from NetBox with its optimistic-concurrency version.

    404 if NetBox has no such device; 502 if NetBox is unreachable — both via
    the `NetBoxClientError` handlers in `main.py`.
    """
    return await service.get_device(device_id)


@router.patch("/{device_id}", response_model=DeviceResponse)
async def update_device(
    device_id: int,
    request: DeviceUpdateRequest,
    if_unmodified_since: str = Header(..., alias="If-Unmodified-Since"),
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-mobile-user")),
    write_service: NetBoxWriteService = Depends(get_write_service),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """PATCH editable fields on a NetBox device with optimistic concurrency.

    Returns the updated device on success. On version mismatch returns 409 with
    the current state so the mobile client can prompt the user to retry from a
    fresh read. NetBox 404 / 5xx flow through ``main.py``'s global handlers.

    Sprint 9 Task 0: optional ``Idempotency-Key`` header.
    """

    async def _do_work() -> tuple[int, dict[str, object]]:
        changes = to_netbox_changes(request)
        try:
            updated = await write_service.patch_with_attribution(
                netbox_path=f"/api/dcim/devices/{device_id}/",
                netbox_object_type="dcim.device",
                netbox_object_id=device_id,
                entity_type="device",
                operation="device.update",
                expected_version=if_unmodified_since,
                changes=changes,
                user=user,
            )
        except WriteConflictError as exc:
            return status.HTTP_409_CONFLICT, {
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": to_device_data(exc.current_object).model_dump(),
                    "current_version": exc.current_version,
                }
            }
        except NetBoxValidationError as exc:
            resp = netbox_validation_error_response(
                exc, fallback_message="NetBox rejected the update"
            )
            import json as _json

            return resp.status_code, _json.loads(bytes(resp.body))
        return status.HTTP_200_OK, DeviceResponse(
            data=to_device_data(updated), version=updated["last_updated"]
        ).model_dump(mode="json")

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=sessionmaker,
        user_keycloak_id=UUID(user.sub),
        idempotency_key=idempotency_key,
        request_payload={
            "device_id": device_id,
            "if_unmodified_since": if_unmodified_since,
            **request.model_dump(mode="json"),
        },
        do_work=_do_work,
    )
    return JSONResponse(body, status_code=status_code)
