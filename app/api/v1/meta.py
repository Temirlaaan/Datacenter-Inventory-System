"""Meta lookup endpoints. Architecture §5.

- ``GET /api/v1/meta/{sites,racks,statuses}`` — NetBox static lookups behind a
  5-minute cache, feeding the ``choice``/``reference`` fields of the form.
- ``GET /api/v1/meta/device-form`` — the server-driven device-edit form config.

All require the ``dcinv-mobile-user`` role.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import AuthUser, require_role
from app.netbox.client import get_netbox_client
from app.services.device_form import DeviceFormConfig, get_device_form_config
from app.services.meta import (
    MetaLookupService,
    MetaRack,
    MetaSite,
    MetaStatus,
    get_meta_cache,
)

router = APIRouter()


def get_meta_service() -> MetaLookupService:
    """Build the meta-lookup service from the process-wide NetBox client + cache."""
    return MetaLookupService(get_netbox_client(), get_meta_cache())


@router.get("/sites", response_model=list[MetaSite])
async def list_sites(
    service: MetaLookupService = Depends(get_meta_service),
    _user: AuthUser = Depends(require_role("dcinv-mobile-user")),
) -> list[MetaSite]:
    """All NetBox sites — the form's Site reference field."""
    return await service.get_sites()


@router.get("/racks", response_model=list[MetaRack])
async def list_racks(
    service: MetaLookupService = Depends(get_meta_service),
    _user: AuthUser = Depends(require_role("dcinv-mobile-user")),
) -> list[MetaRack]:
    """All NetBox racks — the form's Rack reference field."""
    return await service.get_racks()


@router.get("/statuses", response_model=list[MetaStatus])
async def list_statuses(
    service: MetaLookupService = Depends(get_meta_service),
    _user: AuthUser = Depends(require_role("dcinv-mobile-user")),
) -> list[MetaStatus]:
    """Device-status choices discovered from NetBox — the form's Status field."""
    return await service.get_statuses()


@router.get("/device-form", response_model=DeviceFormConfig)
async def get_device_form(
    _user: AuthUser = Depends(require_role("dcinv-mobile-user")),
) -> DeviceFormConfig:
    """The server-driven device-edit form config (Architecture §5)."""
    return get_device_form_config()
