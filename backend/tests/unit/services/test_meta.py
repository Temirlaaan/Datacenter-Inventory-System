"""Unit tests for app.services.meta.MetaLookupService — NetBox faked with respx."""

from __future__ import annotations

from typing import Any

import pytest
import respx

from app.netbox.client import NetBoxClient
from app.netbox.errors import NetBoxServerError
from app.services.cache import TTLCache
from app.services.meta import (
    MetaDeviceType,
    MetaLookupService,
    MetaRack,
    MetaRole,
    MetaSite,
    MetaStatus,
    get_meta_cache,
)

NETBOX_URL = "https://netbox.example.com"


@pytest.fixture
def netbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip retry sleeps so the error-path test isn't gated on real wall time."""
    from app.netbox import client as client_module

    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


def _page(results: list[dict[str, Any]]) -> dict[str, Any]:
    """A NetBox paginated list-endpoint payload."""
    return {"count": len(results), "next": None, "previous": None, "results": results}


def _options_payload(choices: list[dict[str, str]]) -> dict[str, Any]:
    """A NetBox OPTIONS payload exposing the device `status` choice set."""
    return {"actions": {"POST": {"status": {"choices": choices}}}}


# ---------- sites ----------


async def test_get_sites_fetches_and_transforms(clean_env: None, netbox_env: None) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/sites/").respond(
                json=_page([{"id": 1, "name": "DC-1"}, {"id": 2, "name": "DC-2"}])
            )
            sites = await MetaLookupService(client, cache).get_sites()

    assert sites == [MetaSite(id=1, name="DC-1"), MetaSite(id=2, name="DC-2")]


async def test_get_sites_served_from_cache_on_second_call(
    clean_env: None, netbox_env: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/dcim/sites/").respond(
                json=_page([{"id": 1, "name": "DC-1"}])
            )
            service = MetaLookupService(client, cache)
            first = await service.get_sites()
            second = await service.get_sites()

    assert first == second
    assert route.call_count == 1  # second call served from cache — NetBox hit once


async def test_get_sites_returns_empty_list_when_netbox_has_no_sites(
    clean_env: None, netbox_env: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/sites/").respond(json=_page([]))
            sites = await MetaLookupService(client, cache).get_sites()

    assert sites == []


async def test_get_sites_propagates_netbox_error(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/sites/").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await MetaLookupService(client, cache).get_sites()


# ---------- racks ----------


async def test_get_racks_fetches_and_transforms(clean_env: None, netbox_env: None) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/racks/").respond(
                json=_page(
                    [{"id": 7, "name": "R-14", "site": {"id": 1, "name": "DC-1"}, "u_height": 42}]
                )
            )
            racks = await MetaLookupService(client, cache).get_racks()

    assert racks == [MetaRack(id=7, name="R-14", site_id=1, u_height=42)]


async def test_get_racks_filters_by_site_id(clean_env: None, netbox_env: None) -> None:
    """``site_id`` scopes the result to one site (2026-06-15 fix — the filter
    was previously ignored and every site's racks came back). One upstream
    read, in-memory filter over the cached full list."""
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/racks/").respond(
                json=_page(
                    [
                        {"id": 7, "name": "R-14", "site": {"id": 1, "name": "DC-1"}, "u_height": 42},
                        {"id": 8, "name": "R-02", "site": {"id": 2, "name": "DC-2"}, "u_height": 42},
                        {"id": 9, "name": "R-15", "site": {"id": 1, "name": "DC-1"}, "u_height": 47},
                    ]
                )
            )
            service = MetaLookupService(client, cache)
            site_1 = await service.get_racks(site_id=1)
            # Second call hits the cache (assert_all_called still satisfied by
            # the single upstream GET); proves filtering is post-cache.
            site_2 = await service.get_racks(site_id=2)
            all_racks = await service.get_racks()

    assert [r.id for r in site_1] == [7, 9]
    assert [r.id for r in site_2] == [8]
    assert {r.id for r in all_racks} == {7, 8, 9}


async def test_get_racks_returns_empty_list_when_netbox_has_no_racks(
    clean_env: None, netbox_env: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/racks/").respond(json=_page([]))
            racks = await MetaLookupService(client, cache).get_racks()

    assert racks == []


# ---------- statuses ----------


async def test_get_statuses_parses_options_choices(clean_env: None, netbox_env: None) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.options(f"{NETBOX_URL}/api/dcim/devices/").respond(
                json=_options_payload(
                    [
                        {"value": "active", "display": "Active"},
                        {"value": "offline", "display": "Offline"},
                    ]
                )
            )
            statuses = await MetaLookupService(client, cache).get_statuses()

    assert statuses == [
        MetaStatus(value="active", label="Active"),
        MetaStatus(value="offline", label="Offline"),
    ]


async def test_get_statuses_returns_empty_list_when_no_choices(
    clean_env: None, netbox_env: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.options(f"{NETBOX_URL}/api/dcim/devices/").respond(json=_options_payload([]))
            statuses = await MetaLookupService(client, cache).get_statuses()

    assert statuses == []


async def test_get_statuses_accepts_netbox_4x_label_key(
    clean_env: None, netbox_env: None
) -> None:
    """NetBox 4.x choice serializer uses ``label`` where 3.x used ``display``.
    Real 2026-06-11 prod incident: a NetBox upgrade silently broke the mobile
    Edit/Create flows because the handler hard-coded ``choice["display"]``."""
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.options(f"{NETBOX_URL}/api/dcim/devices/").respond(
                json=_options_payload(
                    [
                        {"value": "active", "label": "Active"},
                        {"value": "offline", "label": "Offline"},
                    ]
                )
            )
            statuses = await MetaLookupService(client, cache).get_statuses()

    assert statuses == [
        MetaStatus(value="active", label="Active"),
        MetaStatus(value="offline", label="Offline"),
    ]


async def test_get_statuses_falls_back_to_value_when_no_label_or_display(
    clean_env: None, netbox_env: None
) -> None:
    """Defense in depth: if a future NetBox version drops human-readable text
    altogether, fall back to the slug rather than 500-ing. UI is uglier but
    the picker still works."""
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.options(f"{NETBOX_URL}/api/dcim/devices/").respond(
                json=_options_payload([{"value": "active"}])
            )
            statuses = await MetaLookupService(client, cache).get_statuses()

    assert statuses == [MetaStatus(value="active", label="active")]


# ---------- cache singleton ----------


def test_get_meta_cache_returns_singleton(clean_env: None) -> None:
    assert get_meta_cache() is get_meta_cache()


# ---------- device-types (Sprint 5 Task 2) ----------


async def test_get_device_types_fetches_and_transforms(clean_env: None, netbox_env: None) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/device-types/").respond(
                json=_page(
                    [
                        {
                            "id": 11,
                            "model": "C9300-48U",
                            "manufacturer": {"id": 21, "name": "Cisco"},
                            "u_height": 1,
                        }
                    ]
                )
            )
            device_types = await MetaLookupService(client, cache).get_device_types()

    assert device_types == [
        MetaDeviceType(id=11, model="C9300-48U", manufacturer_name="Cisco", u_height=1),
    ]


async def test_get_device_types_returns_empty_list_when_netbox_has_no_types(
    clean_env: None, netbox_env: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/device-types/").respond(json=_page([]))
            device_types = await MetaLookupService(client, cache).get_device_types()

    assert device_types == []


async def test_get_device_types_served_from_cache_on_second_call(
    clean_env: None, netbox_env: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/dcim/device-types/").respond(
                json=_page(
                    [
                        {
                            "id": 11,
                            "model": "C9300-48U",
                            "manufacturer": {"id": 21, "name": "Cisco"},
                            "u_height": 1,
                        }
                    ]
                )
            )
            service = MetaLookupService(client, cache)
            await service.get_device_types()
            await service.get_device_types()

    assert route.call_count == 1  # cache hit on second call


# ---------- roles (Sprint 5 Task 2) ----------


async def test_get_roles_fetches_and_transforms(clean_env: None, netbox_env: None) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/device-roles/").respond(
                json=_page([{"id": 31, "name": "Access Switch"}])
            )
            roles = await MetaLookupService(client, cache).get_roles()

    assert roles == [MetaRole(id=31, name="Access Switch")]


async def test_get_roles_returns_empty_list_when_netbox_has_no_roles(
    clean_env: None, netbox_env: None
) -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/device-roles/").respond(json=_page([]))
            roles = await MetaLookupService(client, cache).get_roles()

    assert roles == []
