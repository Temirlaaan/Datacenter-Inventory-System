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


@lru_cache
def get_meta_cache() -> TTLCache:
    """Process-wide cache for NetBox static lookups (5-minute TTL)."""
    return TTLCache(_META_TTL_SECONDS)
