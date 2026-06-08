"""Endpoint tests for GET /api/v1/devices/search (Sprint 9 Task 1).

Direct-await: stub the ``DeviceService`` so no NetBox round-trip; the
service layer's NetBox-call assertions live in
``tests/unit/services/test_device.py``. The endpoint layer's only job is:

- pass filter params to ``service.search()``
- cache the result for 30s on the full query-key
- live below ``/{device_id}`` in the route table so ``GET /devices/search``
  matches this handler, not the int-typed read route
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.api.v1.devices import (
    get_device_search_cache,
    reset_device_search_cache,
    search_devices,
)
from app.auth.dependencies import AuthUser
from app.services.device import (
    DeviceData,
    DeviceResponse,
    DeviceSearchResponse,
    DeviceService,
    ObjectRef,
    StatusRef,
)


def _user() -> AuthUser:
    return AuthUser(
        sub="11111111-1111-1111-1111-111111111111",
        email="alice@example.com",
        roles=("dcinv-mobile-user",),
        session_id=None,
        shift_session_id=None,
    )


def _envelope(*device_ids: int, has_more: bool = False) -> DeviceSearchResponse:
    return DeviceSearchResponse(
        results=[
            DeviceResponse(
                data=DeviceData(
                    id=d_id,
                    name=f"sw-{d_id:02}",
                    status=StatusRef(value="active", label="Active"),
                    site=ObjectRef(id=1, name="DC-1"),
                    rack=None,
                    position=None,
                    serial=None,
                    asset_tag=None,
                    comments=None,
                    custom_fields={},
                ),
                version="2026-06-08T12:00:00Z",
            )
            for d_id in device_ids
        ],
        page=1,
        page_size=20,
        has_more=has_more,
    )


class _StubDeviceService:
    def __init__(self, *, returns: DeviceSearchResponse) -> None:
        self._returns = returns
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> DeviceSearchResponse:
        self.calls.append(kwargs)
        return self._returns


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    reset_device_search_cache()


async def test_search_devices_passes_filter_params_through_to_service() -> None:
    """Every filter kwarg in the handler signature reaches the service."""
    stub = _StubDeviceService(returns=_envelope(42))
    result = await search_devices(
        name="sw",
        asset_tag="A-9",
        serial="ABC123",
        site_id=1,
        rack_id=7,
        page=2,
        page_size=10,
        service=cast(DeviceService, stub),
        cache=get_device_search_cache(),
        _user=_user(),
    )
    assert [d.data.id for d in result.results] == [42]
    assert stub.calls == [
        {
            "name": "sw",
            "asset_tag": "A-9",
            "serial": "ABC123",
            "site_id": 1,
            "rack_id": 7,
            "page": 2,
            "page_size": 10,
        }
    ]


async def test_search_devices_second_call_within_ttl_does_not_hit_service() -> None:
    """Cache hit: identical query within 30s shares one service.search call."""
    stub = _StubDeviceService(returns=_envelope(7))
    cache = get_device_search_cache()

    # First call — service is hit, result cached.
    await search_devices(
        name="sw",
        asset_tag=None,
        serial=None,
        site_id=None,
        rack_id=None,
        page=1,
        page_size=20,
        service=cast(DeviceService, stub),
        cache=cache,
        _user=_user(),
    )
    # Second call with the same params — should NOT hit the service.
    second = await search_devices(
        name="sw",
        asset_tag=None,
        serial=None,
        site_id=None,
        rack_id=None,
        page=1,
        page_size=20,
        service=cast(DeviceService, stub),
        cache=cache,
        _user=_user(),
    )
    assert len(stub.calls) == 1
    assert [d.data.id for d in second.results] == [7]


async def test_search_devices_different_filter_creates_a_separate_cache_entry() -> None:
    """``name=sw`` and ``name=router`` hash to different cache keys."""
    stub = _StubDeviceService(returns=_envelope(1))
    cache = get_device_search_cache()
    common_kwargs: dict[str, Any] = {
        "asset_tag": None,
        "serial": None,
        "site_id": None,
        "rack_id": None,
        "page": 1,
        "page_size": 20,
        "service": cast(DeviceService, stub),
        "cache": cache,
        "_user": _user(),
    }
    await search_devices(name="sw", **common_kwargs)
    await search_devices(name="router", **common_kwargs)
    assert len(stub.calls) == 2


def test_devices_search_route_declared_before_read_device_route() -> None:
    """Regression guard: FastAPI dispatches by registration order. If
    ``GET /{device_id}`` (int-typed) registers before ``GET /search``,
    a request for ``/devices/search`` 422s on the int parser.
    """
    from app.api.v1.devices import router

    paths = [getattr(r, "path", None) for r in router.routes if getattr(r, "path", None)]
    search_idx = paths.index("/search")
    detail_idx = paths.index("/{device_id}")
    assert search_idx < detail_idx, (
        f"/search must come before /{{device_id}}; got search={search_idx}, "
        f"detail={detail_idx}"
    )
