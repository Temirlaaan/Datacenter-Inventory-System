"""Endpoint tests for app.api.v1.devices.

Handler logic by direct ``await``; ``AsyncClient`` proves routing, role-gating,
and the NetBox-error -> HTTP mapping. The device endpoint touches no database —
``DeviceService`` is stubbed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.devices import (
    get_device_service,
    get_write_service,
    read_device,
    update_device,
)
from app.auth.dependencies import AuthUser
from app.main import app, handle_netbox_error, handle_netbox_not_found
from app.netbox.errors import NetBoxNotFound, NetBoxServerError
from app.services.device import (
    DeviceData,
    DeviceResponse,
    DeviceService,
    DeviceUpdateRequest,
    ObjectRef,
    StatusRef,
)
from app.services.netbox_write import NetBoxWriteService, WriteConflictError

_VERSION = "2026-05-18T10:00:00.000000Z"
_NEW_VERSION = "2026-05-18T11:30:00.000000Z"


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        sub="11111111-1111-1111-1111-111111111111",
        email="alice@example.com",
        roles=roles,
        session_id=None,
    )


def _device_response() -> DeviceResponse:
    return DeviceResponse(
        data=DeviceData(
            id=5,
            name="sw-01",
            status=StatusRef(value="active", label="Active"),
            site=ObjectRef(id=1, name="DC-1"),
            rack=ObjectRef(id=7, name="R-14"),
            position=42,
            serial="ABC123",
            asset_tag="A-9",
            comments="core switch",
        ),
        version=_VERSION,
    )


class _StubDeviceService:
    """Stands in for DeviceService — returns a canned device or raises."""

    def __init__(
        self,
        *,
        response: DeviceResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error

    async def get_device(self, device_id: int) -> DeviceResponse:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


# ---------- handler logic (direct await) ----------


async def test_read_device_handler_returns_device() -> None:
    stub = _StubDeviceService(response=_device_response())
    result = await read_device(
        device_id=5,
        service=cast(DeviceService, stub),
        _user=_user("dcinv-mobile-user"),
    )
    assert result.data.id == 5
    assert result.version == _VERSION


async def test_get_device_service_builds_a_device_service() -> None:
    assert isinstance(get_device_service(), DeviceService)


# ---------- routing / role / error mapping (AsyncClient) ----------


async def test_get_device_endpoint_returns_200_and_shape(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_device_service] = lambda: _StubDeviceService(
        response=_device_response()
    )
    resp = await client.get("/api/v1/devices/5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == _VERSION
    assert body["data"]["id"] == 5
    assert body["data"]["status"]["value"] == "active"
    assert body["data"]["site"]["name"] == "DC-1"


async def test_get_device_endpoint_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user()  # authenticated, but no roles
    app.dependency_overrides[get_device_service] = lambda: _StubDeviceService(
        response=_device_response()
    )
    resp = await client.get("/api/v1/devices/5")
    assert resp.status_code == 403


async def test_get_device_endpoint_404_for_unknown_device(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_device_service] = lambda: _StubDeviceService(
        error=NetBoxNotFound("GET /api/dcim/devices/999/ → 404")
    )
    resp = await client.get("/api/v1/devices/999")
    assert resp.status_code == 404


async def test_get_device_endpoint_502_on_netbox_error(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_device_service] = lambda: _StubDeviceService(
        error=NetBoxServerError("GET /api/dcim/devices/5/ → 503")
    )
    resp = await client.get("/api/v1/devices/5")
    assert resp.status_code == 502


async def test_get_device_endpoint_422_for_non_integer_id(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get("/api/v1/devices/not-an-int")
    assert resp.status_code == 422


# ---------- NetBox exception handlers (direct await) ----------
# The AsyncClient tests above prove the status codes; coverage.py does not trace
# handler bodies run through the ASGI exception stack, so assert them directly.


async def test_handle_netbox_not_found_returns_404() -> None:
    resp = await handle_netbox_not_found(
        cast(Request, None), NetBoxNotFound("GET /api/dcim/devices/999/ → 404")
    )
    assert resp.status_code == 404


async def test_handle_netbox_error_returns_502() -> None:
    resp = await handle_netbox_error(
        cast(Request, None), NetBoxServerError("GET /api/dcim/devices/5/ → 503")
    )
    assert resp.status_code == 502


# ---------- PATCH /devices/{id} ----------


def _device_dict(version: str = _VERSION, **overrides: Any) -> dict[str, Any]:
    """A raw NetBox device payload — every key `to_device_data` reads is present."""
    device = {
        "id": 5,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "core switch",
        "custom_fields": {"asset_tag": "A-9"},
        "last_updated": version,
    }
    device.update(overrides)
    return device


class _StubWriteService:
    """Stands in for NetBoxWriteService — returns a canned updated device or raises."""

    def __init__(
        self,
        *,
        updated: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._updated = updated
        self._error = error

    async def patch_with_attribution(self, **_kwargs: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        assert self._updated is not None
        return self._updated


# ---------- update handler (direct await) ----------


async def test_update_device_handler_returns_updated_device() -> None:
    updated = _device_dict(_NEW_VERSION, name="sw-01-new")
    stub = _StubWriteService(updated=updated)

    result = await update_device(
        device_id=5,
        request=DeviceUpdateRequest(name="sw-01-new"),
        if_unmodified_since=_VERSION,
        user=_user("dcinv-mobile-user"),
        write_service=cast(NetBoxWriteService, stub),
    )

    assert isinstance(result, DeviceResponse)
    assert result.version == _NEW_VERSION
    assert result.data.name == "sw-01-new"


async def test_update_device_handler_returns_409_on_write_conflict() -> None:
    current = _device_dict(_NEW_VERSION)
    stub = _StubWriteService(
        error=WriteConflictError(current_object=current, current_version=_NEW_VERSION)
    )

    result = await update_device(
        device_id=5,
        request=DeviceUpdateRequest(name="ignored"),
        if_unmodified_since=_VERSION,
        user=_user("dcinv-mobile-user"),
        write_service=cast(NetBoxWriteService, stub),
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION
    assert body["error"]["current_state"]["id"] == 5
    assert body["error"]["current_state"]["name"] == "sw-01"


async def test_get_write_service_builds_a_netbox_write_service() -> None:
    fake_session = cast(AsyncSession, object())
    assert isinstance(get_write_service(session=fake_session), NetBoxWriteService)


# ---------- update endpoint (AsyncClient) ----------


async def test_patch_device_endpoint_returns_200_with_updated_device(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    updated = _device_dict(_NEW_VERSION, name="sw-01-new")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(updated=updated)

    resp = await client.patch(
        "/api/v1/devices/5",
        json={"name": "sw-01-new"},
        headers={"If-Unmodified-Since": _VERSION},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == _NEW_VERSION
    assert body["data"]["name"] == "sw-01-new"


async def test_patch_device_endpoint_returns_409_on_write_conflict(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    current = _device_dict(_NEW_VERSION)
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(
        error=WriteConflictError(current_object=current, current_version=_NEW_VERSION)
    )

    resp = await client.patch(
        "/api/v1/devices/5",
        json={"name": "sw-01-new"},
        headers={"If-Unmodified-Since": _VERSION},
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION
    assert body["error"]["current_state"]["id"] == 5


async def test_patch_device_endpoint_returns_422_when_if_unmodified_since_missing(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(updated=_device_dict())

    resp = await client.patch("/api/v1/devices/5", json={"name": "sw-01-new"})

    assert resp.status_code == 422


async def test_patch_device_endpoint_returns_422_for_over_length_name(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(updated=_device_dict())

    resp = await client.patch(
        "/api/v1/devices/5",
        json={"name": "x" * 65},
        headers={"If-Unmodified-Since": _VERSION},
    )

    assert resp.status_code == 422


async def test_patch_device_endpoint_returns_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user()  # authenticated, but no roles
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(updated=_device_dict())

    resp = await client.patch(
        "/api/v1/devices/5",
        json={"name": "sw-01-new"},
        headers={"If-Unmodified-Since": _VERSION},
    )

    assert resp.status_code == 403


async def test_patch_device_endpoint_returns_404_for_unknown_device(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(
        error=NetBoxNotFound("GET /api/dcim/devices/999/ → 404")
    )

    resp = await client.patch(
        "/api/v1/devices/999",
        json={"name": "sw-01-new"},
        headers={"If-Unmodified-Since": _VERSION},
    )

    assert resp.status_code == 404
