"""NetBox static-lookup endpoints behind a 5-minute in-process cache.

`MetaLookupService` serves the choice/reference option sets the server-driven
device-edit form needs (Task 4): device statuses, sites, racks. Static lookups
may cache for 5 minutes (CLAUDE.md caching policy) — long enough to spare NetBox
repeated reads, short enough that an admin's change shows up promptly.

Statuses are discovered dynamically from NetBox via `OPTIONS /api/dcim/devices/`
(`parking-lot.md`: the status set is never hardcoded), so adding a NetBox status
needs no code change here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from app.netbox.client import NetBoxClient
from app.services.cache import TTLCache

_META_TTL_SECONDS = 300.0

_SITES_KEY = "meta:sites"
_RACKS_KEY = "meta:racks"
_STATUSES_KEY = "meta:statuses"
_DEVICE_TYPES_KEY = "meta:device-types"
_ROLES_KEY = "meta:roles"


class MetaSite(BaseModel):
    """A NetBox site, trimmed to what a `reference` form field needs."""

    id: int
    name: str


class MetaRack(BaseModel):
    """A NetBox rack. `site_id` lets the mobile app scope racks to the chosen
    site client-side; `u_height` bounds the Position field."""

    id: int
    name: str
    site_id: int
    u_height: int


class MetaStatus(BaseModel):
    """A device-status choice — `value` is sent to NetBox, `label` shown to the user."""

    value: str
    label: str


class MetaDeviceType(BaseModel):
    """A NetBox device type — Sprint 5 Task 2 device-create form `reference` field.

    `manufacturer_name` is the human-readable manufacturer label (e.g. "Cisco"),
    derived from the nested `device_type.manufacturer.name`. NetBox 4.x always
    populates this for a device-type. `u_height` lets the mobile client know
    how many U the device will occupy when selecting a rack position.
    """

    id: int
    model: str
    manufacturer_name: str
    u_height: int


class MetaRole(BaseModel):
    """A NetBox device role — Sprint 5 Task 2 device-create form `reference` field."""

    id: int
    name: str


class MetaLookupService:
    """Fetches NetBox static lookups, caching each for 5 minutes."""

    def __init__(self, netbox_client: NetBoxClient, cache: TTLCache) -> None:
        self._netbox = netbox_client
        self._cache = cache

    async def get_sites(self) -> list[MetaSite]:
        return await self._cache.get_or_fetch(_SITES_KEY, self._fetch_sites)

    async def get_racks(self) -> list[MetaRack]:
        return await self._cache.get_or_fetch(_RACKS_KEY, self._fetch_racks)

    async def get_statuses(self) -> list[MetaStatus]:
        return await self._cache.get_or_fetch(_STATUSES_KEY, self._fetch_statuses)

    async def get_device_types(self) -> list[MetaDeviceType]:
        """Sprint 5 Task 2: device-create form's `device_type_id` reference field."""
        return await self._cache.get_or_fetch(_DEVICE_TYPES_KEY, self._fetch_device_types)

    async def get_roles(self) -> list[MetaRole]:
        """Sprint 5 Task 2: device-create form's `role_id` reference field.

        NetBox 4.x exposes this at `/api/dcim/device-roles/` (3.x also). The
        write payload uses key `"role"` per NetBox 4.x convention (see
        `to_netbox_create_payload`); only the WRITE side is version-dependent.
        """
        return await self._cache.get_or_fetch(_ROLES_KEY, self._fetch_roles)

    async def _fetch_sites(self) -> list[MetaSite]:
        response = await self._netbox.get("/api/dcim/sites/", params={"limit": 0})
        results: list[dict[str, Any]] = response.json()["results"]
        return [MetaSite(id=site["id"], name=site["name"]) for site in results]

    async def _fetch_racks(self) -> list[MetaRack]:
        response = await self._netbox.get("/api/dcim/racks/", params={"limit": 0})
        results: list[dict[str, Any]] = response.json()["results"]
        return [
            MetaRack(
                id=rack["id"],
                name=rack["name"],
                site_id=rack["site"]["id"],
                u_height=rack["u_height"],
            )
            for rack in results
        ]

    async def _fetch_statuses(self) -> list[MetaStatus]:
        response = await self._netbox.options("/api/dcim/devices/")
        choices: list[dict[str, Any]] = response.json()["actions"]["POST"]["status"]["choices"]
        return [MetaStatus(value=choice["value"], label=choice["display"]) for choice in choices]

    async def _fetch_device_types(self) -> list[MetaDeviceType]:
        response = await self._netbox.get("/api/dcim/device-types/", params={"limit": 0})
        results: list[dict[str, Any]] = response.json()["results"]
        return [
            MetaDeviceType(
                id=dt["id"],
                model=dt["model"],
                manufacturer_name=dt["manufacturer"]["name"],
                u_height=dt["u_height"],
            )
            for dt in results
        ]

    async def _fetch_roles(self) -> list[MetaRole]:
        # NetBox 3.x and 4.x both expose device roles at /api/dcim/device-roles/.
        response = await self._netbox.get("/api/dcim/device-roles/", params={"limit": 0})
        results: list[dict[str, Any]] = response.json()["results"]
        return [MetaRole(id=role["id"], name=role["name"]) for role in results]


@lru_cache
def get_meta_cache() -> TTLCache:
    """Process-wide cache for NetBox static lookups (5-minute TTL)."""
    return TTLCache(_META_TTL_SECONDS)
