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
    create_device,
    get_device_service,
    get_write_service,
    read_device,
    update_device,
)
from app.auth.dependencies import AuthUser
from app.main import app, handle_netbox_error, handle_netbox_not_found
from app.netbox.errors import NetBoxNotFound, NetBoxServerError, NetBoxValidationError
from app.services.device import (
    DeviceCreateRequest,
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


async def test_handle_no_active_shift_returns_409_with_structured_body() -> None:
    """Sprint 6 Task 4 step (a): dep-layer ``NoActiveShiftError`` translates to
    the structured 409 ``{"error": {"code": "NO_ACTIVE_SHIFT", ...}}`` body
    mobile clients can render."""
    import json

    from app.auth.dependencies import NoActiveShiftError
    from app.main import handle_no_active_shift

    resp = await handle_no_active_shift(cast(Request, None), NoActiveShiftError())

    assert resp.status_code == 409
    body = json.loads(bytes(resp.body))
    assert body["error"]["code"] == "NO_ACTIVE_SHIFT"


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
    """Stands in for NetBoxWriteService — returns canned PATCH/POST results or raises."""

    def __init__(
        self,
        *,
        updated: dict[str, Any] | None = None,
        created: dict[str, Any] | None = None,
        error: Exception | None = None,
        post_error: Exception | None = None,
    ) -> None:
        self._updated = updated
        self._created = created
        self._error = error
        self._post_error = post_error
        self.last_post_kwargs: dict[str, Any] | None = None

    async def patch_with_attribution(self, **_kwargs: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        assert self._updated is not None
        return self._updated

    async def post_with_attribution(self, **kwargs: Any) -> dict[str, Any]:
        self.last_post_kwargs = kwargs
        if self._post_error is not None:
            raise self._post_error
        assert self._created is not None
        return self._created


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


async def test_update_device_handler_translates_netbox_validation_error_to_422() -> None:
    """Sprint 7 Task 5: NBV from the device PATCH surfaces as structured 422."""
    netbox_body = {"status": ["Invalid value for status."]}
    stub = _StubWriteService(error=NetBoxValidationError(status_code=400, detail=netbox_body))

    result = await update_device(
        device_id=5,
        request=DeviceUpdateRequest(name="ignored"),
        if_unmodified_since=_VERSION,
        user=_user("dcinv-mobile-user"),
        write_service=cast(NetBoxWriteService, stub),
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 422
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body
    assert body["error"]["message"] == "NetBox rejected the update"


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


# ---------- create device (Sprint 5 Task 2) ----------


def _create_body() -> dict[str, Any]:
    """The smallest valid create body — required fields only."""
    return {
        "device_type_id": 11,
        "role_id": 31,
        "site_id": 1,
        "status": "active",
        "name": "sw-99",
    }


def _created_dict(device_id: int = 99) -> dict[str, Any]:
    """A raw NetBox create response — all keys to_device_data reads."""
    return {
        "id": device_id,
        "name": "sw-99",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": None,
        "position": None,
        "serial": "",
        "comments": "",
        "custom_fields": {"asset_tag": None},
        "last_updated": _NEW_VERSION,
    }


# --- handler logic (direct await) ---


async def test_create_device_handler_returns_device_response_on_success() -> None:
    stub = _StubWriteService(created=_created_dict())
    result = await create_device(
        request=DeviceCreateRequest(**_create_body()),
        user=_user("dcinv-mobile-user"),
        write_service=cast(NetBoxWriteService, stub),
    )
    assert isinstance(result, DeviceResponse)
    assert result.data.id == 99
    assert result.data.name == "sw-99"
    assert result.version == _NEW_VERSION


async def test_create_device_handler_passes_payload_through_post_with_attribution() -> None:
    stub = _StubWriteService(created=_created_dict())
    await create_device(
        request=DeviceCreateRequest(**_create_body(), serial="ABC", asset_tag="A-9"),
        user=_user("dcinv-mobile-user"),
        write_service=cast(NetBoxWriteService, stub),
    )
    assert stub.last_post_kwargs is not None
    kwargs = stub.last_post_kwargs
    assert kwargs["netbox_path"] == "/api/dcim/devices/"
    assert kwargs["netbox_object_type"] == "dcim.device"
    assert kwargs["netbox_object_id"] is None
    assert kwargs["entity_type"] == "device"
    assert kwargs["entity_id"] is None  # derived from response by post_with_attribution
    assert kwargs["operation"] == "device.create"
    assert kwargs["attach_journal"] is True
    # Payload is `to_netbox_create_payload`'s output (renames applied)
    assert kwargs["payload"]["device_type"] == 11
    assert kwargs["payload"]["role"] == 31
    assert kwargs["payload"]["site"] == 1
    assert kwargs["payload"]["serial"] == "ABC"
    assert kwargs["payload"]["custom_fields"] == {"asset_tag": "A-9"}


async def test_create_device_handler_returns_422_on_netbox_validation_error() -> None:
    """Correction 2: NetBox 4xx surfaces as a structured 422 with NetBox's
    actual message (not a 502 'bad gateway')."""
    netbox_body = {"name": ["device with this name already exists."]}
    stub = _StubWriteService(
        post_error=NetBoxValidationError(status_code=400, detail=netbox_body),
    )
    result = await create_device(
        request=DeviceCreateRequest(**_create_body()),
        user=_user("dcinv-mobile-user"),
        write_service=cast(NetBoxWriteService, stub),
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 422
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body


async def test_create_device_handler_422_message_uses_str_detail_when_text_body() -> None:
    """When NetBox returned a non-JSON body (e.g. HTML 403), the str detail
    is surfaced as the message verbatim."""
    stub = _StubWriteService(
        post_error=NetBoxValidationError(status_code=403, detail="Forbidden"),
    )
    result = await create_device(
        request=DeviceCreateRequest(**_create_body()),
        user=_user("dcinv-mobile-user"),
        write_service=cast(NetBoxWriteService, stub),
    )
    body = json.loads(bytes(cast(JSONResponse, result).body))
    assert body["error"]["message"] == "Forbidden"
    assert body["error"]["netbox_status"] == 403


# --- routing / role / validation (AsyncClient) ---


async def test_post_create_device_endpoint_returns_201(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(created=_created_dict())

    resp = await client.post("/api/v1/devices/", json=_create_body())

    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["id"] == 99
    assert body["version"] == _NEW_VERSION


async def test_post_create_device_endpoint_returns_422_for_missing_required_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(created=_created_dict())

    body = _create_body()
    del body["device_type_id"]
    resp = await client.post("/api/v1/devices/", json=body)

    assert resp.status_code == 422


async def test_post_create_device_endpoint_returns_422_for_extra_body_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """extra='forbid' on DeviceCreateRequest catches typos."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(created=_created_dict())

    body = {**_create_body(), "device_type": "not-the-right-field-name"}
    resp = await client.post("/api/v1/devices/", json=body)

    assert resp.status_code == 422


async def test_post_create_device_endpoint_returns_422_for_over_length_name(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(created=_created_dict())

    body = {**_create_body(), "name": "x" * 65}
    resp = await client.post("/api/v1/devices/", json=body)

    assert resp.status_code == 422


async def test_post_create_device_endpoint_returns_422_with_netbox_validation_body(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """End-to-end: NetBox 4xx → structured 422 with netbox_detail in the body."""
    as_user("dcinv-mobile-user")
    netbox_body = {"name": ["device with this name already exists."]}
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(
        post_error=NetBoxValidationError(status_code=400, detail=netbox_body),
    )

    resp = await client.post("/api/v1/devices/", json=_create_body())

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body


async def test_post_create_device_endpoint_returns_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")  # admin only, no mobile
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(created=_created_dict())

    resp = await client.post("/api/v1/devices/", json=_create_body())

    assert resp.status_code == 403


async def test_post_create_device_endpoint_returns_502_on_netbox_5xx(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """NetBoxServerError flows through main.py's global handler → 502
    (not 422 — that's only for VALIDATION errors)."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_write_service] = lambda: _StubWriteService(
        post_error=NetBoxServerError("POST /api/dcim/devices/ → 503"),
    )

    resp = await client.post("/api/v1/devices/", json=_create_body())

    assert resp.status_code == 502
