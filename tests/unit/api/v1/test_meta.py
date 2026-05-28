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
    get_device_create_form,
    get_device_form,
    get_meta_service,
    list_device_types,
    list_racks,
    list_roles,
    list_sites,
    list_statuses,
)
from app.auth.dependencies import AuthUser
from app.main import app
from app.services.device_form import DeviceFormConfig
from app.services.meta import (
    MetaDeviceType,
    MetaLookupService,
    MetaRack,
    MetaRole,
    MetaSite,
    MetaStatus,
)


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
        device_types: list[MetaDeviceType] | None = None,
        roles: list[MetaRole] | None = None,
    ) -> None:
        self._sites = sites or []
        self._racks = racks or []
        self._statuses = statuses or []
        self._device_types = device_types or []
        self._roles = roles or []

    async def get_sites(self) -> list[MetaSite]:
        return self._sites

    async def get_racks(self) -> list[MetaRack]:
        return self._racks

    async def get_statuses(self) -> list[MetaStatus]:
        return self._statuses

    async def get_device_types(self) -> list[MetaDeviceType]:
        return self._device_types

    async def get_roles(self) -> list[MetaRole]:
        return self._roles


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


# ---------- device-types + roles + device-create-form (Sprint 5 Task 2) ----------


async def test_list_device_types_handler_returns_service_result() -> None:
    stub = _StubMetaService(
        device_types=[
            MetaDeviceType(id=11, model="C9300-48U", manufacturer_name="Cisco", u_height=1),
        ]
    )
    result = await list_device_types(
        service=cast(MetaLookupService, stub),
        _user=_user("dcinv-mobile-user"),
    )
    assert result == [
        MetaDeviceType(id=11, model="C9300-48U", manufacturer_name="Cisco", u_height=1)
    ]


async def test_list_roles_handler_returns_service_result() -> None:
    stub = _StubMetaService(roles=[MetaRole(id=31, name="Access Switch")])
    result = await list_roles(
        service=cast(MetaLookupService, stub),
        _user=_user("dcinv-mobile-user"),
    )
    assert result == [MetaRole(id=31, name="Access Switch")]


async def test_get_device_types_endpoint_returns_200_and_shape(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService(
        device_types=[
            MetaDeviceType(id=11, model="C9300-48U", manufacturer_name="Cisco", u_height=1),
        ]
    )
    resp = await client.get("/api/v1/meta/device-types")
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": 11, "model": "C9300-48U", "manufacturer_name": "Cisco", "u_height": 1},
    ]


async def test_get_roles_endpoint_returns_200_and_shape(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService(
        roles=[MetaRole(id=31, name="Access Switch")]
    )
    resp = await client.get("/api/v1/meta/roles")
    assert resp.status_code == 200
    assert resp.json() == [{"id": 31, "name": "Access Switch"}]


async def test_get_device_types_endpoint_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user()
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService()
    resp = await client.get("/api/v1/meta/device-types")
    assert resp.status_code == 403


async def test_get_roles_endpoint_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user()
    app.dependency_overrides[get_meta_service] = lambda: _StubMetaService()
    resp = await client.get("/api/v1/meta/roles")
    assert resp.status_code == 403


async def test_get_device_create_form_handler_returns_create_yaml_config() -> None:
    result = await get_device_create_form(_user=_user("dcinv-mobile-user"))
    assert isinstance(result, DeviceFormConfig)
    assert result.version
    # device_create.yaml has 10 fields incl. device_type_id and role_id
    keys = {field.key for field in result.fields}
    assert "device_type_id" in keys
    assert "role_id" in keys
    assert "site_id" in keys


async def test_get_device_create_form_endpoint_returns_200_independent_of_edit_form(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Both endpoints work, return distinct configs."""
    as_user("dcinv-mobile-user")
    resp_edit = await client.get("/api/v1/meta/device-form")
    resp_create = await client.get("/api/v1/meta/device-create-form")

    assert resp_edit.status_code == 200
    assert resp_create.status_code == 200
    edit_keys = {f["key"] for f in resp_edit.json()["fields"]}
    create_keys = {f["key"] for f in resp_create.json()["fields"]}
    # Create form has device_type_id + role_id; edit form doesn't.
    assert "device_type_id" in create_keys
    assert "device_type_id" not in edit_keys
    assert len(create_keys) == 10
    assert len(edit_keys) == 8


async def test_get_device_create_form_endpoint_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user()
    resp = await client.get("/api/v1/meta/device-create-form")
    assert resp.status_code == 403
