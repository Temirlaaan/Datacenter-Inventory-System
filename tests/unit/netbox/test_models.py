"""Pydantic model shape tests — only fields we rely on in Sprint 1/2 are asserted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_status_parses_minimal_payload() -> None:
    from app.netbox.models import Status

    s = Status.model_validate({"netbox-version": "4.1.0", "django-version": "5.0", "plugins": {}})
    assert s.netbox_version == "4.1.0"


def test_status_requires_netbox_version() -> None:
    from app.netbox.models import Status

    with pytest.raises(ValidationError):
        Status.model_validate({"django-version": "5.0"})


def test_site_parses_minimal_payload() -> None:
    from app.netbox.models import Site

    s = Site.model_validate({"id": 1, "name": "DC1", "slug": "dc1"})
    assert s.id == 1
    assert s.name == "DC1"
    assert s.slug == "dc1"


def test_rack_parses_with_nested_site() -> None:
    from app.netbox.models import Rack

    r = Rack.model_validate(
        {"id": 7, "name": "R-01", "site": {"id": 1, "name": "DC1", "slug": "dc1"}}
    )
    assert r.id == 7
    assert r.site.name == "DC1"


def test_device_parses_minimal_payload() -> None:
    from app.netbox.models import Device

    d = Device.model_validate(
        {
            "id": 42,
            "name": "edge-sw-1",
            "status": {"value": "active", "label": "Active"},
            "last_updated": "2026-05-14T10:00:00Z",
        }
    )
    assert d.id == 42
    assert d.name == "edge-sw-1"
    assert d.status.value == "active"
    assert d.last_updated.year == 2026


def test_device_rejects_missing_last_updated() -> None:
    """last_updated drives optimistic concurrency — must be present (CLAUDE.md cross-cutting #3)."""
    from app.netbox.models import Device

    with pytest.raises(ValidationError):
        Device.model_validate(
            {"id": 1, "name": "x", "status": {"value": "active", "label": "Active"}}
        )
