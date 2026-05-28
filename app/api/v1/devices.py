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

All require the ``dcinv-mobile-user`` role.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role
from app.db.repositories.audit_log import AuditLogRepository
from app.db.session import get_session
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
from app.services.netbox_write import NetBoxWriteService, WriteConflictError

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


@router.post("/", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(
    request: DeviceCreateRequest,
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    write_service: NetBoxWriteService = Depends(get_write_service),
) -> DeviceResponse | JSONResponse:
    """Create a new NetBox device. ToR §4.3.2 (Create New Device flow).

    On NetBox validation failure (4xx — duplicate name, position collision,
    invalid status, etc.) translates to a structured 422 with
    ``error.code="NETBOX_VALIDATION_ERROR"`` carrying NetBox's actual message
    so the mobile client can surface it (Sprint 5 Correction 2). Other
    NetBox errors (404 on a referenced FK, 5xx) flow through ``main.py``'s
    global handlers (404 / 502).
    """
    payload = to_netbox_create_payload(request)
    try:
        created = await write_service.post_with_attribution(
            netbox_path="/api/dcim/devices/",
            netbox_object_type="dcim.device",
            netbox_object_id=None,  # journal target derived from response
            entity_type="device",
            entity_id=None,  # audit entity_id derived from str(created["id"])
            operation="device.create",
            payload=payload,
            user=user,
            attach_journal=True,
        )
    except NetBoxValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "NETBOX_VALIDATION_ERROR",
                    "message": (
                        exc.detail
                        if isinstance(exc.detail, str)
                        else "NetBox rejected the create request"
                    ),
                    "netbox_status": exc.status_code,
                    "netbox_detail": exc.detail,
                }
            },
        )
    return DeviceResponse(data=to_device_data(created), version=created["last_updated"])


@router.post(
    "/{device_id}/comments",
    response_model=AddCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    device_id: int,
    request: AddCommentRequest,
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    comment_service: CommentService = Depends(get_comment_service),
) -> AddCommentResponse:
    """Append a NetBox journal entry to ``device_id``. Sprint 5 Task 3, ToR §4.3.6.

    NetBoxNotFound / NetBoxClientError flow through ``main.py``'s global
    handlers (404 / 502). No specialised 422 translation here — add-comment
    has a much narrower failure mode than device-create (no FK constraints,
    no uniqueness rules), so the generic 502 is appropriate. Sprint 6
    candidate to extend NetBoxValidationError catching across all endpoints.
    """
    created = await comment_service.add_comment(
        device_id=device_id,
        comment=request.comment,
        user=user,
    )
    return AddCommentResponse(id=created["id"])


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
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    write_service: NetBoxWriteService = Depends(get_write_service),
) -> DeviceResponse | JSONResponse:
    """PATCH editable fields on a NetBox device with optimistic concurrency.

    Returns the updated device on success. On version mismatch returns 409 with
    the current state so the mobile client can prompt the user to retry from a
    fresh read. NetBox 404 / 5xx flow through ``main.py``'s global handlers.
    """
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
    return DeviceResponse(data=to_device_data(updated), version=updated["last_updated"])
