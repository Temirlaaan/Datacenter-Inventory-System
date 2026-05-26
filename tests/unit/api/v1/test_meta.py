"""Endpoint tests for app.api.v1.meta.

Handler logic is exercised by direct ``await`` (coverage traces that reliably);
the ``AsyncClient`` tests prove routing, role-gating, and response_model wiring.
The meta endpoints touch no database — the ``MetaLookupService`` is stubbed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import httpx

from app.api.v1.meta import (
    get_device_form,
    get_meta_service,
    list_racks,
    list_sites,
    list_statuses,
)
from app.auth.dependencies import AuthUser
from app.main import app
from app.services.device_form import DeviceFormConfig
from app.services.meta import MetaLookupService, MetaRack, MetaSite, MetaStatus


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        sub="11111111-1111-1111-1111-111111111111",
        email="alice@example.com",
        roles=roles,
        session_id=None,
    )


class _StubMetaService:
    """Stands in for MetaLookupService — returns canned lookups, never hits NetBox."""

    def __init__(
        self,
        *,
        sites: list[MetaSite] | None = None,
        racks: list[MetaRack] | None = None,
        statuses: list[MetaStatus] | None = None,
    ) -> None:
        self._sites = sites or []
        self._racks = racks or []
        self._statuses = statuses or []

    async def get_sites(self) -> list[MetaSite]:
        return self._sites

    async def get_racks(self) -> list[MetaRack]:
        return self._racks

    async def get_statuses(self) -> list[MetaStatus]:
        return self._statuses


# ---------- handler logic (direct await) ----------


async def test_list_sites_handler_returns_service_result() -> None:
    stub = _StubMetaService(sites=[MetaSite(id=1, name="DC-1")])
    result = await list_sites(
        service=cast(MetaLookupService, stub), _user=_user("dcinv-mobile-user")
    )
    assert result == [MetaSite(id=1, name="DC-1")]


async def test_list_racks_handler_returns_service_result() -> None:
    stub = _StubMetaService(racks=[MetaRack(id=7, name="R-14", site_id=1, u_height=42)])
    result = await list_racks(
        service=cast(MetaLookupService, stub), _user=_user("dcinv-mobile-user")
    )
    assert result == [MetaRack(id=7, name="R-14", site_id=1, u_height=42)]


async def test_list_statuses_handler_returns_service_result() -> None:
    stub = _StubMetaService(statuses=[MetaStatus(value="active", label="Active")])
    result = await list_statuses(
        service=cast(MetaLookupService, stub), _user=_user("dcinv-mobile-user")
    )
    assert result == [MetaStatus(value="active", label="Active")]


async def test_get_meta_service_builds_a_meta_lookup_service() -> None:
    assert isinstance(get_meta_service(), MetaLookupService)


# ---------- routing / role-gating / response_model (AsyncClient) ----------


async def test_get_sites_endpoint_returns_200_and_shape(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService(
        sites=[MetaSite(id=1, name="DC-1")]
    )
    resp = await client.get("/api/v1/meta/sites")
    assert resp.status_code == 200
    assert resp.json() == [{"id": 1, "name": "DC-1"}]


async def test_get_racks_endpoint_returns_200_and_shape(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService(
        racks=[MetaRack(id=7, name="R-14", site_id=1, u_height=42)]
    )
    resp = await client.get("/api/v1/meta/racks")
    assert resp.status_code == 200
    assert resp.json() == [{"id": 7, "name": "R-14", "site_id": 1, "u_height": 42}]


async def test_get_statuses_endpoint_returns_200_and_shape(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService(
        statuses=[MetaStatus(value="active", label="Active")]
    )
    resp = await client.get("/api/v1/meta/statuses")
    assert resp.status_code == 200
    assert resp.json() == [{"value": "active", "label": "Active"}]


async def test_get_sites_endpoint_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user()  # authenticated, but no roles
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService()
    resp = await client.get("/api/v1/meta/sites")
    assert resp.status_code == 403


# ---------- device-form ----------


async def test_get_device_form_handler_returns_config() -> None:
    result = await get_device_form(_user=_user("dcinv-mobile-user"))
    assert isinstance(result, DeviceFormConfig)
    assert result.version
    assert {field.key for field in result.fields} == {
        "status",
        "site",
        "rack",
        "position",
        "name",
        "serial",
        "cf_asset_tag",
        "comments",
    }


async def test_get_device_form_endpoint_returns_200_with_passthrough_keys(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get("/api/v1/meta/device-form")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]
    assert len(body["fields"]) == 8
    status_field = next(f for f in body["fields"] if f["key"] == "status")
    # A field-specific key not modelled on FormField must survive serialization.
    assert status_field["choices_endpoint"] == "/api/v1/meta/statuses"


async def test_get_device_form_endpoint_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user()  # authenticated, but no roles
    resp = await client.get("/api/v1/meta/device-form")
    assert resp.status_code == 403
