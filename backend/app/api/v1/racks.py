"""Rack endpoints. Backend TZ: ``docs/backend-tz-rack-elevation.md``.

- ``GET /api/v1/racks/{rack_id}/elevation`` — the aggregate the mobile
  client draws its rack visualisation from (devices with face + honest
  u_height, reservations, occupancy counters).

Read-only, role ``dcinv-mobile-user`` (same gate as the other mobile
reads), no active shift required, no audit row.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.auth.dependencies import AuthUser, require_role
from app.netbox.client import get_netbox_client
from app.netbox.errors import NetBoxNotFound
from app.services.rack_elevation import (
    RackElevationResponse,
    RackElevationService,
    get_elevation_cache,
)

router = APIRouter()


def get_elevation_service() -> RackElevationService:
    """Per-request service over the process-wide 60s elevation cache."""
    return RackElevationService(get_netbox_client(), get_elevation_cache())


@router.get("/{rack_id}/elevation", response_model=RackElevationResponse)
async def get_rack_elevation(
    rack_id: int,
    service: RackElevationService = Depends(get_elevation_service),
    _user: AuthUser = Depends(require_role("dcinv-mobile-user")),
) -> RackElevationResponse | JSONResponse:
    """Rack elevation aggregate: positioned devices (face + u_height ≥ 1),
    reservations, unpositioned count, occupied-units count.

    Unknown rack → structured 404 ``RACK_NOT_FOUND`` (the TZ error table)
    rather than the generic NetBoxNotFound handler's body, so the mobile
    client can branch on the code.
    """
    try:
        return await service.get_elevation(rack_id)
    except NetBoxNotFound:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "RACK_NOT_FOUND",
                    "message": f"Rack {rack_id} not found in NetBox",
                }
            },
        )
