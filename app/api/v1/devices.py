"""Device endpoints. Architecture §3.2, ToR §4.3.

- ``GET /api/v1/devices/{device_id}`` — read a device from NetBox with its
  optimistic-concurrency ``version``.
- ``PATCH /api/v1/devices/{device_id}`` — update editable fields, gated by the
  client's ``If-Unmodified-Since`` header. 409 on a stale version.

Both require the ``dcinv-mobile-user`` role.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role
from app.db.repositories.audit_log import AuditLogRepository
from app.db.session import get_session
from app.netbox.client import get_netbox_client
from app.services.device import (
    DeviceResponse,
    DeviceService,
    DeviceUpdateRequest,
    to_device_data,
    to_netbox_changes,
)
from app.services.netbox_write import NetBoxWriteService, WriteConflictError

router = APIRouter()


def get_device_service() -> DeviceService:
    """Build the device read service from the process-wide NetBox client."""
    return DeviceService(get_netbox_client())


def get_write_service(session: AsyncSession = Depends(get_session)) -> NetBoxWriteService:
    """Build the three-record-write service from the per-request session."""
    return NetBoxWriteService(get_netbox_client(), session, AuditLogRepository(session))


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
