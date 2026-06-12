"""Endpoint tests for app.api.v1.racks.

Handler logic by direct ``await`` (coverage traces that reliably); the
service is stubbed — the transform itself is covered in
``tests/unit/services/test_rack_elevation.py``.
"""

from __future__ import annotations

import json

from app.api.v1.racks import get_rack_elevation
from app.auth.dependencies import AuthUser
from app.netbox.errors import NetBoxNotFound
from app.services.rack_elevation import (
    ElevationDevice,
    ElevationRack,
    ElevationReservation,
    RackElevationResponse,
)


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        sub="11111111-1111-1111-1111-111111111111",
        email="alice@example.com",
        roles=roles,
        session_id=None,
    )


def _canned_elevation(rack_id: int = 4) -> RackElevationResponse:
    return RackElevationResponse(
        rack=ElevationRack(id=rack_id, name="Server-Rack-1.12", site_id=1, u_height=42),
        devices=[
            ElevationDevice(
                id=238,
                name="srv-51",
                status={"value": "active", "label": "Active"},
                role_name="Server",
                device_type_model="PowerEdge R640",
                position=34,
                u_height=1,
                face="front",
            )
        ],
        reservations=[ElevationReservation(units=[10, 11], description="planned SAN")],
        unpositioned_count=2,
        occupied_units=28,
    )


class _StubElevationService:
    def __init__(self, *, response: RackElevationResponse | None = None) -> None:
        self._response = response
        self.requested_rack_ids: list[int] = []

    async def get_elevation(self, rack_id: int) -> RackElevationResponse:
        self.requested_rack_ids.append(rack_id)
        if self._response is None:
            raise NetBoxNotFound(f"GET /api/dcim/racks/{rack_id}/ -> 404")
        return self._response


async def test_get_rack_elevation_returns_aggregate_for_known_rack() -> None:
    stub = _StubElevationService(response=_canned_elevation())

    result = await get_rack_elevation(
        rack_id=4,
        service=stub,  # type: ignore[arg-type]
        _user=_user("dcinv-mobile-user"),
    )

    assert isinstance(result, RackElevationResponse)
    assert result.rack.id == 4
    assert result.devices[0].face == "front"
    assert result.unpositioned_count == 2
    assert result.occupied_units == 28
    assert stub.requested_rack_ids == [4]


async def test_get_rack_elevation_unknown_rack_returns_structured_404() -> None:
    """NetBoxNotFound → 404 with the RACK_NOT_FOUND code from the TZ error
    table — NOT the generic NetBoxNotFound handler body, so the mobile
    client can branch on the code."""
    stub = _StubElevationService(response=None)

    result = await get_rack_elevation(
        rack_id=999,
        service=stub,  # type: ignore[arg-type]
        _user=_user("dcinv-mobile-user"),
    )

    assert result.status_code == 404  # type: ignore[union-attr]
    body = json.loads(bytes(result.body))  # type: ignore[union-attr]
    assert body["error"]["code"] == "RACK_NOT_FOUND"
    assert "999" in body["error"]["message"]
