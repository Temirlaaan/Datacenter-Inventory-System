"""Endpoint tests for POST /api/v1/devices/{id}/decommission — Sprint 5 Task 4.

Mirrors test_qr_retire.py: handler logic by direct ``await``; ``AsyncClient``
proves routing + role gating + body validation. Service orchestration is
stubbed — its own units live in ``tests/unit/services/test_device_decommission.py``.

The decommission endpoint doesn't touch Postgres directly (no
batch_id lookup needed in the response — it returns the device, not a QR),
but reuses ``conftest.py`` so it picks up the standard skip-gate and the
``as_user``/``client`` fixtures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from fastapi.responses import JSONResponse

from app.api.v1.devices import (
    DeviceDecommissionRequest,
    decommission_device,
    get_decommission_service,
)
from app.auth.dependencies import AuthUser
from app.main import app
from app.netbox.errors import NetBoxNotFound, NetBoxServerError
from app.services.device import DeviceResponse
from app.services.device_decommission import (
    DeviceDecommissionInconsistencyError,
    DeviceDecommissionRolledBackError,
    DeviceDecommissionService,
)
from app.services.netbox_write import WriteConflictError
from app.services.qr.lifecycle import (
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
)
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_DEVICE_ID = 99
_QR_ID = "DCQR-FREEKLM2"
_VERSION = "2026-05-21T08:00:00.000000Z"
_NEW_VERSION = "2026-05-21T10:00:00.000000Z"


def _device_dict(version: str = _NEW_VERSION, *, status: str = "decommissioning") -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": status, "label": status.title()},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "",
        "custom_fields": {"asset_tag": "A-9", "qr_id": None},
        "last_updated": version,
    }


class _StubDecommissionService:
    """Stand-in for ``DeviceDecommissionService.decommission`` — canned result or raises."""

    def __init__(
        self,
        *,
        result: DeviceResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.last_kwargs: dict[str, Any] | None = None

    async def decommission(
        self,
        *,
        device_id: int,
        expected_version: str,
        reason: str | None,
        user: AuthUser,
    ) -> DeviceResponse:
        self.last_kwargs = {
            "device_id": device_id,
            "expected_version": expected_version,
            "reason": reason,
            "user": user,
        }
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _decommissioned_device_response() -> DeviceResponse:
    from app.services.device import to_device_data

    raw = _device_dict()
    return DeviceResponse(data=to_device_data(raw), version=raw["last_updated"])


# ---------- handler logic (direct await) ----------


async def test_decommission_handler_returns_device_response_on_happy_path() -> None:
    stub = _StubDecommissionService(result=_decommissioned_device_response())
    result = await decommission_device(
        device_id=_DEVICE_ID,
        request=DeviceDecommissionRequest(version=_VERSION, reason="EOL"),
        user=make_user("dcinv-admin"),
        service=cast(DeviceDecommissionService, stub),
    )
    assert isinstance(result, DeviceResponse)
    assert result.data.status.value == "decommissioning"
    # Service received the unpacked request fields.
    assert stub.last_kwargs is not None
    assert stub.last_kwargs["device_id"] == _DEVICE_ID
    assert stub.last_kwargs["expected_version"] == _VERSION
    assert stub.last_kwargs["reason"] == "EOL"


async def test_decommission_handler_returns_409_on_write_conflict() -> None:
    current_obj = _device_dict(_NEW_VERSION, status="active")
    stub = _StubDecommissionService(
        error=WriteConflictError(current_object=current_obj, current_version=_NEW_VERSION)
    )
    result = await decommission_device(
        device_id=_DEVICE_ID,
        request=DeviceDecommissionRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        service=cast(DeviceDecommissionService, stub),
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    import json as _json

    body = _json.loads(result.body)
    assert body["error"]["code"] == "DEVICE_CONFLICT"
    assert body["error"]["current_version"] == _NEW_VERSION


async def test_decommission_handler_returns_409_on_qr_state_conflict() -> None:
    from app.domain.qr import QRStatus

    stub = _StubDecommissionService(error=QRStateConflictError(QRStatus.RETIRED))
    result = await decommission_device(
        device_id=_DEVICE_ID,
        request=DeviceDecommissionRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        service=cast(DeviceDecommissionService, stub),
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    import json as _json

    body = _json.loads(result.body)
    assert body["error"]["code"] == "QR_STATE_CONFLICT"
    assert body["error"]["current_status"] == "retired"


async def test_decommission_handler_returns_500_on_qr_retire_rolled_back() -> None:
    stub = _StubDecommissionService(error=QRRetireRolledBackError(_QR_ID, _DEVICE_ID))
    result = await decommission_device(
        device_id=_DEVICE_ID,
        request=DeviceDecommissionRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        service=cast(DeviceDecommissionService, stub),
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    import json as _json

    body = _json.loads(result.body)
    assert body["error"]["code"] == "QR_RETIRE_ROLLED_BACK"
    assert body["error"]["qr_id"] == _QR_ID
    assert body["error"]["device_id"] == _DEVICE_ID


async def test_decommission_endpoint_returns_decommission_rolled_back_on_branch_2() -> None:
    """Q3 Branch 2: service raised DeviceDecommissionRolledBackError → 500 + structured body."""
    stub = _StubDecommissionService(error=DeviceDecommissionRolledBackError(_DEVICE_ID, _QR_ID))
    result = await decommission_device(
        device_id=_DEVICE_ID,
        request=DeviceDecommissionRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        service=cast(DeviceDecommissionService, stub),
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    import json as _json

    body = _json.loads(result.body)
    assert body["error"]["code"] == "DECOMMISSION_ROLLED_BACK"
    assert body["error"]["device_id"] == _DEVICE_ID
    assert body["error"]["qr_id"] == _QR_ID


async def test_decommission_endpoint_returns_decommission_inconsistency_on_branch_3() -> None:
    """Q3 Branch 3: service raised DeviceDecommissionInconsistencyError → 500 + structured body."""
    stub = _StubDecommissionService(error=DeviceDecommissionInconsistencyError(_DEVICE_ID, _QR_ID))
    result = await decommission_device(
        device_id=_DEVICE_ID,
        request=DeviceDecommissionRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        service=cast(DeviceDecommissionService, stub),
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    import json as _json

    body = _json.loads(result.body)
    assert body["error"]["code"] == "DECOMMISSION_INCONSISTENCY"
    assert body["error"]["device_id"] == _DEVICE_ID
    assert body["error"]["qr_id"] == _QR_ID
    assert "manual cleanup" in body["error"]["message"].lower()


async def test_decommission_endpoint_returns_qr_inconsistent_error_on_inconsistency_path() -> None:
    """Correction 4: service raised QRRetireInconsistencyError → 500
    QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT (distinct from retire-endpoint's code)."""
    stub = _StubDecommissionService(error=QRRetireInconsistencyError(_QR_ID, _DEVICE_ID))
    result = await decommission_device(
        device_id=_DEVICE_ID,
        request=DeviceDecommissionRequest(version=_VERSION),
        user=make_user("dcinv-admin"),
        service=cast(DeviceDecommissionService, stub),
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
    import json as _json

    body = _json.loads(result.body)
    assert body["error"]["code"] == "QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT"
    assert body["error"]["qr_id"] == _QR_ID
    assert body["error"]["device_id"] == _DEVICE_ID
    assert "manual cleanup" in body["error"]["message"].lower()


# ---------- routing / role / validation (AsyncClient) ----------


async def test_post_decommission_endpoint_returns_200_on_happy_path(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_decommission_service] = lambda: _StubDecommissionService(
        result=_decommissioned_device_response()
    )

    resp = await client.post(
        f"/api/v1/devices/{_DEVICE_ID}/decommission",
        json={"version": _VERSION, "reason": "EOL"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"]["value"] == "decommissioning"
    assert body["version"] == _NEW_VERSION


async def test_post_decommission_endpoint_returns_403_without_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Decision G: decommission requires dcinv-admin, NOT dcinv-mobile-user."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_decommission_service] = lambda: _StubDecommissionService()

    resp = await client.post(
        f"/api/v1/devices/{_DEVICE_ID}/decommission",
        json={"version": _VERSION},
    )

    assert resp.status_code == 403


async def test_post_decommission_endpoint_returns_422_for_missing_version(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_decommission_service] = lambda: _StubDecommissionService()

    resp = await client.post(f"/api/v1/devices/{_DEVICE_ID}/decommission", json={})

    assert resp.status_code == 422


async def test_post_decommission_endpoint_returns_422_for_extra_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """extra='forbid' catches typos."""
    as_user("dcinv-admin")
    app.dependency_overrides[get_decommission_service] = lambda: _StubDecommissionService()

    resp = await client.post(
        f"/api/v1/devices/{_DEVICE_ID}/decommission",
        json={"version": _VERSION, "comment": "should be 'reason'"},
    )

    assert resp.status_code == 422


async def test_post_decommission_endpoint_returns_404_when_device_not_found(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_decommission_service] = lambda: _StubDecommissionService(
        error=NetBoxNotFound("device 999 not found")
    )

    resp = await client.post(
        "/api/v1/devices/999/decommission",
        json={"version": _VERSION},
    )

    assert resp.status_code == 404


async def test_post_decommission_endpoint_returns_502_on_netbox_5xx(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_decommission_service] = lambda: _StubDecommissionService(
        error=NetBoxServerError("netbox 503"),
    )

    resp = await client.post(
        f"/api/v1/devices/{_DEVICE_ID}/decommission",
        json={"version": _VERSION},
    )

    assert resp.status_code == 502
