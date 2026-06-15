"""Rack elevation aggregate for the mobile client's rack visualisation.

Backend TZ: ``docs/backend-tz-rack-elevation.md``. The mobile app's phase-1
elevation was assembled client-side from ``/api/v1/devices/search?rack=N``
and had three known defects this endpoint fixes server-side:

1. **face** — front/rear devices on the same units overlapped client-side;
   NetBox knows the face, we now pass it through.
2. **u_height** — the device serializer sometimes omits it; we resolve via
   the top-level field → ``device_type.u_height`` fallback (same chain as
   ``to_device_data``) and guarantee an int ≥ 1.
3. **reservations** — NetBox rack reservations looked like free units;
   now included.

Three NetBox round-trips per cold fetch (rack, devices, reservations).

CACHING DEVIATION FROM THE TZ: the TZ asks for the 5-minute meta cache, but
elevation embeds *device* data (positions move when engineers move hardware)
and the project caching policy caps device-data caches at 60 seconds
(CLAUDE.md). An engineer who repositions a server and immediately opens the
rack view must not see a 5-minute-old lie. 60s keeps NetBox load negligible
while staying policy-compliant.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from app.netbox.client import NetBoxClient
from app.services.cache import TTLCache

_ELEVATION_TTL_SECONDS = 60.0


class ElevationRack(BaseModel):
    """The rack frame the elevation is drawn in."""

    id: int
    name: str
    site_id: int
    u_height: int


class ElevationDevice(BaseModel):
    """One positioned device block in the elevation.

    ``position`` is the lowest unit the device occupies (NetBox convention).
    ``u_height`` is always ≥ 1 (resolved from device_type when the device
    serializer omits it; fractional heights like 0.5U are rounded up so the
    mobile client can draw whole blocks). ``face`` defaults to ``"front"``
    when NetBox returns null (legacy rows).
    """

    id: int
    name: str | None
    status: dict[str, str]  # {"value": "active", "label": "Active"}
    role_name: str | None
    device_type_model: str | None
    position: int
    u_height: int
    face: str  # "front" | "rear"


class ElevationReservation(BaseModel):
    """A NetBox rack reservation — units blocked for planned work."""

    units: list[int]
    description: str


class RackElevationResponse(BaseModel):
    """Envelope for ``GET /api/v1/racks/{rack_id}/elevation``."""

    rack: ElevationRack
    devices: list[ElevationDevice]
    reservations: list[ElevationReservation]
    unpositioned_count: int
    occupied_units: int


def _resolve_u_height(device: dict[str, Any], u_height_map: dict[int, int]) -> int:
    """Device u_height, ≥ 1.

    Primary source is ``u_height_map`` (``{device_type_id: u_height}`` resolved
    from the authoritative device-types resource) — the device serializer
    nests ``device_type`` as a brief object WITHOUT ``u_height`` (2026-06-15
    mobile bug: multi-U disk shelves rendered as 1U). Falls back to any inline
    value, then 1. ``math.ceil`` covers fractional heights (NetBox allows
    0.5U) so the client never draws a zero-height block.
    """
    device_type = device.get("device_type") or {}
    dt_id = device_type.get("id")
    if dt_id is not None and dt_id in u_height_map:
        return u_height_map[dt_id]
    raw = device.get("u_height")
    if raw is None:
        raw = device_type.get("u_height")
    if raw is None:
        return 1
    return max(1, math.ceil(float(raw)))


def _resolve_face(device: dict[str, Any]) -> str:
    """NetBox returns ``face`` as ``{"value": "front", "label": "Front"}``
    or null. Null (legacy rows / odd fixtures) defaults to front — the
    common mounting side, and a wrong-side block beats a missing block."""
    face_raw = device.get("face")
    if isinstance(face_raw, dict) and face_raw.get("value") in ("front", "rear"):
        return str(face_raw["value"])
    return "front"


class RackElevationService:
    """Aggregates rack + devices + reservations into one elevation payload."""

    def __init__(self, netbox_client: NetBoxClient, cache: TTLCache) -> None:
        self._netbox = netbox_client
        self._cache = cache

    async def get_elevation(self, rack_id: int) -> RackElevationResponse:
        """Cached elevation for ``rack_id``. Raises ``NetBoxNotFound`` when
        the rack doesn't exist (the endpoint translates it to the structured
        404 ``RACK_NOT_FOUND`` per the TZ error table)."""

        async def _fetch() -> RackElevationResponse:
            return await self._fetch_elevation(rack_id)

        return await self._cache.get_or_fetch(f"elevation:{rack_id}", _fetch)

    async def _u_height_by_device_type(
        self, devices: list[dict[str, Any]]
    ) -> dict[int, int]:
        """``{device_type_id: u_height}`` from the cached ``/meta/device-types``.

        The device serializer nests ``device_type`` without ``u_height``, so we
        resolve the height from the authoritative device-types resource. That
        list is cached 5 minutes, so this adds no NetBox round trip on a warm
        cache no matter how many device types the rack holds.
        """
        type_ids = {
            tid
            for d in devices
            if (tid := (d.get("device_type") or {}).get("id")) is not None
        }
        if not type_ids:
            return {}
        from app.services.meta import MetaLookupService, get_meta_cache

        types = await MetaLookupService(self._netbox, get_meta_cache()).get_device_types()
        return {t.id: max(1, t.u_height) for t in types if t.id in type_ids}

    async def _fetch_elevation(self, rack_id: int) -> RackElevationResponse:
        # Rack first — a 404 here aborts before the two list calls.
        rack_resp = await self._netbox.get(f"/api/dcim/racks/{rack_id}/")
        rack_raw = rack_resp.json()

        devices_resp = await self._netbox.get(
            "/api/dcim/devices/", params={"rack_id": rack_id, "limit": 0}
        )
        devices_raw: list[dict[str, Any]] = devices_resp.json().get("results", [])

        reservations_resp = await self._netbox.get(
            "/api/dcim/rack-reservations/", params={"rack_id": rack_id, "limit": 0}
        )
        reservations_raw: list[dict[str, Any]] = reservations_resp.json().get("results", [])

        # {device_type_id: u_height} from the authoritative device-types
        # resource (cached 5 min) — the device list nests device_type as a
        # brief object without u_height (2026-06-15 fix).
        u_height_map = await self._u_height_by_device_type(devices_raw)

        devices: list[ElevationDevice] = []
        unpositioned = 0
        occupied: set[int] = set()
        for d in devices_raw:
            position = d.get("position")
            if position is None:
                # Assigned to the rack but not racked at a unit — counted,
                # not drawn (the TZ contract: such devices are excluded from
                # the array, surfaced via unpositioned_count).
                unpositioned += 1
                continue
            u_height = _resolve_u_height(d, u_height_map)
            bottom_unit = int(position)
            status_raw = d.get("status") or {}
            role_raw = d.get("role") or d.get("device_role") or {}
            device_type_raw = d.get("device_type") or {}
            devices.append(
                ElevationDevice(
                    id=d["id"],
                    name=d.get("name"),
                    status={
                        "value": str(status_raw.get("value", "")),
                        "label": str(status_raw.get("label", "")),
                    },
                    role_name=role_raw.get("name"),
                    device_type_model=(
                        device_type_raw.get("model") or device_type_raw.get("display")
                    ),
                    position=bottom_unit,
                    u_height=u_height,
                    face=_resolve_face(d),
                )
            )
            # A unit is "occupied" if anything sits on it on either face —
            # set union handles front+rear half-depth devices sharing units
            # without double-counting.
            occupied.update(range(bottom_unit, bottom_unit + u_height))

        reservations = [
            ElevationReservation(
                units=[int(u) for u in (r.get("units") or [])],
                description=r.get("description") or "",
            )
            for r in reservations_raw
        ]

        return RackElevationResponse(
            rack=ElevationRack(
                id=rack_raw["id"],
                name=rack_raw["name"],
                site_id=rack_raw["site"]["id"],
                u_height=rack_raw["u_height"],
            ),
            devices=devices,
            reservations=reservations,
            unpositioned_count=unpositioned,
            occupied_units=len(occupied),
        )


@lru_cache
def get_elevation_cache() -> TTLCache:
    """Process-wide elevation cache singleton — 60s TTL (device-data cap)."""
    return TTLCache(ttl_seconds=_ELEVATION_TTL_SECONDS)
