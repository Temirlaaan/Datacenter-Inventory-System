"""Unit tests for app.services.device.DeviceService — NetBox faked with respx."""

from __future__ import annotations

from typing import Any

import pytest
import respx
from pydantic import ValidationError

from app.netbox.client import NetBoxClient
from app.netbox.errors import NetBoxNotFound, NetBoxServerError
from app.services.device import (
    DeviceService,
    DeviceUpdateRequest,
    to_device_data,
    to_netbox_changes,
)

NETBOX_URL = "https://netbox.example.com"
_VERSION = "2026-05-18T10:00:00.000000Z"


@pytest.fixture
def netbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip retry sleeps so the server-error test isn't gated on real wall time."""
    from app.netbox import client as client_module

    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


def _device(**overrides: Any) -> dict[str, Any]:
    """A NetBox device payload — every key the parser reads is present."""
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
        "last_updated": _VERSION,
    }
    device.update(overrides)
    return device


async def test_get_device_parses_a_full_device(clean_env: None, netbox_env: None) -> None:
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/5/").respond(json=_device())
            result = await DeviceService(client).get_device(5)

    assert result.version == _VERSION
    data = result.data
    assert data.id == 5
    assert data.name == "sw-01"
    assert data.status.value == "active"
    assert data.status.label == "Active"
    assert data.site.id == 1
    assert data.site.name == "DC-1"
    assert data.rack is not None
    assert data.rack.id == 7
    assert data.rack.name == "R-14"
    assert data.position == 42
    assert data.serial == "ABC123"
    assert data.asset_tag == "A-9"
    assert data.comments == "core switch"


async def test_get_device_handles_a_device_with_no_rack(clean_env: None, netbox_env: None) -> None:
    """An unracked device: rack/position null, no asset_tag custom field."""
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/5/").respond(
                json=_device(rack=None, position=None, custom_fields={})
            )
            result = await DeviceService(client).get_device(5)

    assert result.data.rack is None
    assert result.data.position is None
    assert result.data.asset_tag is None


async def test_get_device_propagates_not_found(clean_env: None, netbox_env: None) -> None:
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/999/").respond(status_code=404)
            with pytest.raises(NetBoxNotFound):
                await DeviceService(client).get_device(999)


async def test_get_device_propagates_server_error(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/5/").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await DeviceService(client).get_device(5)


# ---------- DeviceUpdateRequest validation ----------


def test_device_update_request_accepts_empty_body() -> None:
    """An empty PATCH body is valid — `to_netbox_changes` will produce {}."""
    DeviceUpdateRequest()


def test_device_update_request_accepts_all_eight_fields() -> None:
    req = DeviceUpdateRequest(
        status="active",
        site_id=1,
        rack_id=7,
        position=42,
        name="sw-01",
        serial="ABC123",
        asset_tag="A-9",
        comments="core switch",
    )
    assert req.name == "sw-01"
    assert req.position == 42


def test_device_update_request_accepts_name_exactly_64_chars() -> None:
    DeviceUpdateRequest(name="x" * 64)


def test_device_update_request_rejects_name_longer_than_64_chars() -> None:
    with pytest.raises(ValidationError):
        DeviceUpdateRequest(name="x" * 65)


def test_device_update_request_rejects_serial_longer_than_50_chars() -> None:
    with pytest.raises(ValidationError):
        DeviceUpdateRequest(serial="x" * 51)


def test_device_update_request_rejects_asset_tag_longer_than_50_chars() -> None:
    with pytest.raises(ValidationError):
        DeviceUpdateRequest(asset_tag="x" * 51)


def test_device_update_request_rejects_comments_longer_than_1000_chars() -> None:
    with pytest.raises(ValidationError):
        DeviceUpdateRequest(comments="x" * 1001)


def test_device_update_request_rejects_unknown_field() -> None:
    """`extra='forbid'` catches typos like `serial_number` vs `serial`."""
    with pytest.raises(ValidationError):
        DeviceUpdateRequest.model_validate({"serial_number": "ABC"})


# ---------- to_netbox_changes ----------


def test_to_netbox_changes_returns_empty_when_no_fields_set() -> None:
    assert to_netbox_changes(DeviceUpdateRequest()) == {}


def test_to_netbox_changes_includes_only_explicitly_set_fields() -> None:
    """`model_dump(exclude_unset=True)` semantics — unset fields stay out."""
    changes = to_netbox_changes(DeviceUpdateRequest(name="sw-01"))
    assert changes == {"name": "sw-01"}


def test_to_netbox_changes_maps_site_id_and_rack_id_to_netbox_keys() -> None:
    changes = to_netbox_changes(DeviceUpdateRequest(site_id=1, rack_id=7))
    assert changes == {"site": 1, "rack": 7}


def test_to_netbox_changes_nests_asset_tag_under_custom_fields() -> None:
    changes = to_netbox_changes(DeviceUpdateRequest(asset_tag="A-9"))
    assert changes == {"custom_fields": {"asset_tag": "A-9"}}


def test_to_netbox_changes_forwards_explicit_null_rack_id_to_unrack() -> None:
    """A client unrack action sends rack_id=None explicitly — must survive."""
    changes = to_netbox_changes(DeviceUpdateRequest(rack_id=None))
    assert changes == {"rack": None}


def test_to_netbox_changes_emits_all_keys_when_every_field_is_set() -> None:
    changes = to_netbox_changes(
        DeviceUpdateRequest(
            status="active",
            site_id=1,
            rack_id=7,
            position=42,
            name="sw-01",
            serial="ABC123",
            asset_tag="A-9",
            comments="core switch",
        )
    )
    assert changes == {
        "status": "active",
        "site": 1,
        "rack": 7,
        "position": 42,
        "name": "sw-01",
        "serial": "ABC123",
        "custom_fields": {"asset_tag": "A-9"},
        "comments": "core switch",
    }


# ---------- to_device_data — Sprint 4 Task 3 additions ----------


def _full_device(**overrides: Any) -> dict[str, Any]:
    """A complete NetBox device payload incl. all Task 3 fields."""
    device = {
        "id": 5,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "core switch",
        "custom_fields": {
            "asset_tag": "A-9",
            "qr_id": "DCQR-OLDVALUE",  # NetBox-side value; decision H ignores this
            "rack_unit_offset": 2,  # arbitrary extra cf — must survive into `custom_fields`
            "deprecated_note": None,  # null — must be filtered out
        },
        "last_updated": _VERSION,
        "device_type": {
            "id": 11,
            "display": "C9300-48U",
            "model": "C9300-48U",
            "manufacturer": {"id": 21, "name": "Cisco"},
            "u_height": 1,
        },
        "role": {"id": 31, "name": "Access Switch"},
        "primary_ip4": {"id": 41, "address": "192.0.2.10/24"},
        "primary_ip6": {"id": 42, "address": "2001:db8::a/64"},
    }
    device.update(overrides)
    return device


def test_to_device_data_extracts_all_new_fields_from_full_device() -> None:
    data = to_device_data(_full_device())
    assert data.device_type is not None
    assert data.device_type.id == 11
    assert data.device_type.name == "C9300-48U"
    assert data.manufacturer is not None
    assert data.manufacturer.id == 21
    assert data.manufacturer.name == "Cisco"
    assert data.device_role is not None
    assert data.device_role.id == 31
    assert data.device_role.name == "Access Switch"
    assert data.u_height == 1
    assert data.primary_ip4 == "192.0.2.10/24"
    assert data.primary_ip6 == "2001:db8::a/64"
    assert data.last_updated == _VERSION


def test_to_device_data_returns_none_for_missing_device_type() -> None:
    """Sprint 3-style fixtures don't carry device_type — must default cleanly."""
    data = to_device_data(_device())
    assert data.device_type is None
    assert data.manufacturer is None
    assert data.u_height is None


def test_to_device_data_returns_none_for_missing_role() -> None:
    data = to_device_data(_device())
    assert data.device_role is None


def test_to_device_data_handles_netbox_3x_device_role_alias() -> None:
    """NetBox 3.x exposes the role under `device_role`; 4.x uses `role`."""
    device = _device(device_role={"id": 99, "name": "Legacy Role"})
    data = to_device_data(device)
    assert data.device_role is not None
    assert data.device_role.id == 99
    assert data.device_role.name == "Legacy Role"


def test_to_device_data_returns_none_for_missing_primary_ips() -> None:
    data = to_device_data(_device())
    assert data.primary_ip4 is None
    assert data.primary_ip6 is None


def test_to_device_data_extracts_primary_ip4_address_string() -> None:
    device = _device(primary_ip4={"id": 41, "address": "192.0.2.10/24"})
    assert to_device_data(device).primary_ip4 == "192.0.2.10/24"


def test_to_device_data_extracts_primary_ip6_address_string() -> None:
    device = _device(primary_ip6={"id": 42, "address": "2001:db8::a/64"})
    assert to_device_data(device).primary_ip6 == "2001:db8::a/64"


def test_to_device_data_extracts_u_height_from_device_type() -> None:
    device = _device(
        device_type={
            "id": 11,
            "display": "Foo",
            "manufacturer": {"id": 1, "name": "X"},
            "u_height": 4,
        }
    )
    assert to_device_data(device).u_height == 4


def test_to_device_data_device_type_name_falls_back_to_model_when_display_absent() -> None:
    device = _device(
        device_type={"id": 11, "model": "C9300-48U", "manufacturer": {"id": 1, "name": "X"}}
    )
    data = to_device_data(device)
    assert data.device_type is not None
    assert data.device_type.name == "C9300-48U"


def test_to_device_data_qr_id_defaults_to_none_when_kwarg_absent() -> None:
    """Standalone /devices/{id} read: no qr_id lookup, value stays None."""
    assert to_device_data(_full_device()).qr_id is None


def test_to_device_data_qr_id_uses_kwarg_value() -> None:
    """Combined response path: lookup service injects the app-DB qr_id."""
    data = to_device_data(_full_device(), qr_id="DCQR-FROMAPPDB")
    assert data.qr_id == "DCQR-FROMAPPDB"


def test_to_device_data_qr_id_kwarg_overrides_netbox_custom_field(
    # decision H: app DB is the source of truth even when NetBox disagrees.
) -> None:
    device = _full_device()  # NetBox custom_fields.qr_id = "DCQR-OLDVALUE"
    data = to_device_data(device, qr_id="DCQR-APPDBWINS")
    assert data.qr_id == "DCQR-APPDBWINS"


def test_to_device_data_custom_fields_drops_none_values() -> None:
    data = to_device_data(_full_device())
    assert data.custom_fields is not None
    assert "deprecated_note" not in data.custom_fields


def test_to_device_data_custom_fields_excludes_asset_tag_and_qr_id() -> None:
    """The dedicated typed fields own these; custom_fields holds the rest."""
    data = to_device_data(_full_device())
    assert data.custom_fields is not None
    assert "asset_tag" not in data.custom_fields
    assert "qr_id" not in data.custom_fields
    # The other custom field passes through.
    assert data.custom_fields["rack_unit_offset"] == 2


def test_to_device_data_custom_fields_returns_none_when_empty_after_filter() -> None:
    """A device with only excluded keys yields no custom_fields entry."""
    device = _device(custom_fields={"asset_tag": "A-9", "qr_id": "x"})
    assert to_device_data(device).custom_fields is None


def test_to_device_data_last_updated_passes_through() -> None:
    data = to_device_data(_device(last_updated="2026-05-24T10:00:00Z"))
    assert data.last_updated == "2026-05-24T10:00:00Z"


def test_to_device_data_sprint3_fields_unchanged_with_minimal_device() -> None:
    """Regression: Sprint 3 callers using the minimal _device() shape still work."""
    data = to_device_data(_device())
    assert data.id == 5
    assert data.name == "sw-01"
    assert data.asset_tag == "A-9"
    # Task 3 additions all default to None
    assert data.qr_id is None
    assert data.device_type is None


# ---------- DeviceService.get_device_raw ----------


async def test_get_device_raw_returns_raw_netbox_payload(clean_env: None, netbox_env: None) -> None:
    payload = _full_device()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/5/").respond(json=payload)
            raw = await DeviceService(client).get_device_raw(5)
    assert raw == payload  # exact pass-through


async def test_get_device_raw_propagates_not_found(clean_env: None, netbox_env: None) -> None:
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/999/").respond(status_code=404)
            with pytest.raises(NetBoxNotFound):
                await DeviceService(client).get_device_raw(999)


async def test_get_device_raw_propagates_server_error(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/5/").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await DeviceService(client).get_device_raw(5)
