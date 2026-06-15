"""Unit tests for app.services.rack_elevation — NetBox faked with respx.

Covers the three defects the TZ (docs/backend-tz-rack-elevation.md) calls out:
face passthrough, honest u_height (top-level → device_type → 1 fallback,
fractional rounded up), and reservations. Plus the occupancy counters and
the 60s cache behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx

from app.netbox.client import NetBoxClient
from app.services.cache import TTLCache
from app.services.rack_elevation import (
    RackElevationService,
    _resolve_face,
    _resolve_u_height,
    get_elevation_cache,
)

NETBOX_URL = "https://netbox.example.com"

_RACK_ID = 4


@pytest.fixture
def netbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


def _rack_payload(rack_id: int = _RACK_ID) -> dict[str, Any]:
    return {
        "id": rack_id,
        "name": "Server-Rack-1.12",
        "site": {"id": 1, "name": "DC-1"},
        "u_height": 42,
    }


def _page(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(results), "next": None, "previous": None, "results": results}


def _device(
    *,
    device_id: int = 238,
    name: str | None = "srv-51",
    position: float | None = 34,
    u_height: float | None = 1,
    face: str | None = "front",
    nested_u_height: float | None = None,
    device_type_id: int = 11,
) -> dict[str, Any]:
    """A NetBox device row trimmed to what the elevation transform reads.

    The nested ``device_type`` carries only ``id`` reliably — ``u_height`` is
    resolved from the device-types resource, not from here (mirrors NetBox's
    brief nested serializer)."""
    return {
        "id": device_id,
        "name": name,
        "status": {"value": "active", "label": "Active"},
        "role": {"id": 31, "name": "Server"},
        "device_type": {
            "id": device_type_id,
            "model": "PowerEdge R640",
            "display": "PowerEdge R640",
            "u_height": nested_u_height,
        },
        "position": position,
        "u_height": u_height,
        "face": {"value": face, "label": face.capitalize()} if face else None,
    }


def _device_types(types: list[dict[str, Any]]) -> dict[str, Any]:
    """A NetBox device-types list payload (the authoritative u_height source)."""
    return _page(
        [
            {
                "id": t["id"],
                "model": t.get("model", "model"),
                "manufacturer": {"name": "Dell"},
                "u_height": t["u_height"],
            }
            for t in types
        ]
    )


def _mock_rack_routes(
    router: respx.Router,
    *,
    devices: list[dict[str, Any]],
    reservations: list[dict[str, Any]] | None = None,
    device_types: list[dict[str, Any]] | None = None,
) -> None:
    router.get(f"{NETBOX_URL}/api/dcim/racks/{_RACK_ID}/").respond(json=_rack_payload())
    router.get(f"{NETBOX_URL}/api/dcim/devices/").respond(json=_page(devices))
    router.get(f"{NETBOX_URL}/api/dcim/rack-reservations/").respond(
        json=_page(reservations or [])
    )
    # Default: device-type 11 (used by the _device() helper) at 1U.
    router.get(f"{NETBOX_URL}/api/dcim/device-types/").respond(
        json=_device_types(device_types or [{"id": 11, "u_height": 1}])
    )


@pytest.fixture(autouse=True)
def _clear_meta_cache() -> Any:
    """The device-types lookup reads the process-wide meta cache singleton;
    clear it around each test so mocked responses aren't masked by a warm
    cache from another test."""
    from app.services.meta import get_meta_cache

    get_meta_cache.cache_clear()
    yield
    get_meta_cache.cache_clear()


# ---------- u_height resolver (pure helper) ----------


def test_resolve_u_height_prefers_device_type_map() -> None:
    """The authoritative {device_type_id: u_height} map wins over any inline
    value — the device serializer's nested u_height is unreliable/absent."""
    device = {"device_type": {"id": 11, "u_height": 99}, "u_height": 99}
    assert _resolve_u_height(device, {11: 2}) == 2


def test_resolve_u_height_falls_back_to_inline_when_type_not_in_map() -> None:
    assert _resolve_u_height({"u_height": 4, "device_type": {"id": 11}}, {}) == 4
    assert _resolve_u_height({"u_height": None, "device_type": {"u_height": 4}}, {}) == 4


def test_resolve_u_height_defaults_to_one_when_absent_everywhere() -> None:
    """Real TZ defect: device 238 came back with null u_height everywhere AND
    not in the map — mobile must still get a drawable ≥ 1 block."""
    assert _resolve_u_height({"u_height": None, "device_type": {"u_height": None}}, {}) == 1
    assert _resolve_u_height({}, {}) == 1


def test_resolve_u_height_rounds_inline_fractional_up_and_clamps_to_one() -> None:
    assert _resolve_u_height({"u_height": 0.5}, {}) == 1  # half-U → one block
    assert _resolve_u_height({"u_height": 2.5}, {}) == 3
    assert _resolve_u_height({"u_height": 0}, {}) == 1  # 0U (vertical PDU) clamps


def test_resolve_face_passes_front_and_rear_defaults_otherwise() -> None:
    assert _resolve_face({"face": {"value": "rear", "label": "Rear"}}) == "rear"
    assert _resolve_face({"face": {"value": "front", "label": "Front"}}) == "front"
    assert _resolve_face({"face": None}) == "front"
    assert _resolve_face({}) == "front"
    assert _resolve_face({"face": {"value": "sideways"}}) == "front"  # junk → default


# ---------- full fetch transform ----------


async def test_get_elevation_assembles_rack_devices_and_reservations(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            _mock_rack_routes(
                router,
                devices=[
                    _device(device_id=238, position=34, face="front", device_type_id=11),
                    _device(
                        device_id=239, name="san-1", position=10, face="rear",
                        device_type_id=12,
                    ),
                ],
                device_types=[{"id": 11, "u_height": 1}, {"id": 12, "u_height": 4}],
                reservations=[
                    {"units": [20, 21], "description": "Под новый SAN, заявка №123"}
                ],
            )
            result = await RackElevationService(
                client, TTLCache(ttl_seconds=60)
            ).get_elevation(_RACK_ID)

    assert result.rack.id == _RACK_ID
    assert result.rack.name == "Server-Rack-1.12"
    assert result.rack.site_id == 1
    assert result.rack.u_height == 42

    assert [d.id for d in result.devices] == [238, 239]
    front = result.devices[0]
    assert front.face == "front"
    assert front.position == 34
    assert front.u_height == 1
    assert front.role_name == "Server"
    assert front.device_type_model == "PowerEdge R640"
    assert front.status == {"value": "active", "label": "Active"}
    assert result.devices[1].face == "rear"

    assert result.reservations[0].units == [20, 21]
    assert "SAN" in result.reservations[0].description
    assert result.unpositioned_count == 0
    # 238 occupies unit 34; 239 occupies 10..13 → 5 distinct units.
    assert result.occupied_units == 5


async def test_get_elevation_excludes_unpositioned_devices_but_counts_them(
    clean_env: None, netbox_env: None
) -> None:
    """TZ contract: position=null devices are NOT in the array — they're
    surfaced via unpositioned_count only."""
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            _mock_rack_routes(
                router,
                devices=[
                    _device(device_id=1, position=5),
                    _device(device_id=2, position=None),
                    _device(device_id=3, position=None),
                ],
            )
            result = await RackElevationService(
                client, TTLCache(ttl_seconds=60)
            ).get_elevation(_RACK_ID)

    assert [d.id for d in result.devices] == [1]
    assert result.unpositioned_count == 2


async def test_get_elevation_occupied_units_deduplicates_front_rear_overlap(
    clean_env: None, netbox_env: None
) -> None:
    """Half-depth front + rear devices on the same units: a unit occupied on
    both faces counts once (set union, not a sum of u_heights)."""
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            _mock_rack_routes(
                router,
                devices=[
                    _device(device_id=1, position=10, face="front"),
                    _device(device_id=2, position=10, face="rear"),
                ],
                device_types=[{"id": 11, "u_height": 2}],
            )
            result = await RackElevationService(
                client, TTLCache(ttl_seconds=60)
            ).get_elevation(_RACK_ID)

    # Sum of u_heights would say 4; honest distinct-unit count is 2.
    assert result.occupied_units == 2


async def test_get_elevation_resolves_u_height_from_device_types_resource(
    clean_env: None, netbox_env: None
) -> None:
    """2026-06-15 mobile bug: a 2U disk shelf whose nested device_type carries
    NO u_height (brief serializer) must still resolve to 2U via the
    authoritative /api/dcim/device-types resource — not default to 1."""
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            _mock_rack_routes(
                router,
                devices=[
                    _device(
                        device_id=238, position=34, u_height=None,
                        nested_u_height=None, device_type_id=11,
                    )
                ],
                device_types=[{"id": 11, "u_height": 2}],
            )
            result = await RackElevationService(
                client, TTLCache(ttl_seconds=60)
            ).get_elevation(_RACK_ID)

    assert result.devices[0].u_height == 2


async def test_get_elevation_caches_within_ttl(clean_env: None, netbox_env: None) -> None:
    """Second call inside the TTL serves from cache — zero extra NetBox calls."""
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            _mock_rack_routes(router, devices=[_device()])
            cache = TTLCache(ttl_seconds=60)
            service = RackElevationService(client, cache)

            first = await service.get_elevation(_RACK_ID)
            calls_after_first = router.calls.call_count
            second = await service.get_elevation(_RACK_ID)
            assert router.calls.call_count == calls_after_first  # no new HTTP
    assert first == second


def test_get_elevation_cache_is_a_60s_singleton(clean_env: None) -> None:
    """Pin the TTL: elevation embeds device positions, so the project's
    device-data caching cap (≤ 60s) applies — NOT the 5-minute meta TTL the
    TZ originally asked for. See the module docstring deviation note."""
    cache = get_elevation_cache()
    assert cache is get_elevation_cache()
    assert cache._ttl == 60.0
