"""Unit tests for app.services.device_form."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.services.device_form import (
    FieldType,
    get_device_form_config,
    load_device_form_config,
)

_VALID_YAML = """\
version: "2026-01-01.1"
fields:
  - key: status
    label: Status
    type: choice
    required: true
    choices_endpoint: /api/v1/meta/statuses
  - key: name
    label: Name
    type: text
"""

_MVP_FIELD_KEYS = {
    "status",
    "site",
    "rack",
    "position",
    "name",
    "serial",
    "cf_asset_tag",
    "comments",
}


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "form.yaml"
    path.write_text(content)
    return path


def test_load_device_form_config_parses_a_valid_file(tmp_path: Path) -> None:
    config = load_device_form_config(_write(tmp_path, _VALID_YAML))

    assert config.version == "2026-01-01.1"
    assert [field.key for field in config.fields] == ["status", "name"]
    assert config.fields[0].type is FieldType.CHOICE
    assert config.fields[0].required is True
    assert config.fields[1].required is False  # defaults to False


def test_load_device_form_config_passes_through_field_specific_keys(tmp_path: Path) -> None:
    """`choices_endpoint` is not a typed FormField attribute — it must survive
    via `extra='allow'` so the mobile client receives it untouched."""
    config = load_device_form_config(_write(tmp_path, _VALID_YAML))

    assert config.fields[0].model_dump()["choices_endpoint"] == "/api/v1/meta/statuses"


def test_load_device_form_config_raises_on_malformed_yaml(tmp_path: Path) -> None:
    with pytest.raises(yaml.YAMLError):
        load_device_form_config(_write(tmp_path, "version: [unterminated"))


def test_load_device_form_config_raises_on_invalid_field_type(tmp_path: Path) -> None:
    bad = """\
version: "2026-01-01.1"
fields:
  - key: status
    label: Status
    type: not_a_real_type
"""
    with pytest.raises(ValidationError):
        load_device_form_config(_write(tmp_path, bad))


def test_load_device_form_config_raises_when_version_missing(tmp_path: Path) -> None:
    no_version = """\
fields:
  - key: name
    label: Name
    type: text
"""
    with pytest.raises(ValidationError):
        load_device_form_config(_write(tmp_path, no_version))


def test_device_edit_yaml_loads_with_the_eight_mvp_fields() -> None:
    """The packaged config parses and carries exactly the decision-F field set."""
    config = get_device_form_config()

    assert config.version
    assert {field.key for field in config.fields} == _MVP_FIELD_KEYS
    by_key = {field.key: field for field in config.fields}
    assert by_key["status"].model_dump()["choices_endpoint"] == "/api/v1/meta/statuses"
    assert by_key["cf_asset_tag"].model_dump()["netbox_field"] == "custom_fields.asset_tag"


def test_get_device_form_config_is_cached() -> None:
    assert get_device_form_config() is get_device_form_config()


# ---------- Sprint 5 Task 2: device_create.yaml + filename parameter ----------


_CREATE_FIELD_KEYS = {
    "device_type_id",
    "role_id",
    "site_id",
    "status",
    "name",
    "rack_id",
    "position",
    "serial",
    "asset_tag",
    "comments",
}


def test_get_device_form_config_default_filename_loads_device_edit_yaml() -> None:
    """Regression: Sprint 3 callers pass no argument and expect the edit form."""
    config = get_device_form_config()
    # Edit form has the eight Sprint 3 keys, not the create form's keys.
    assert "device_type_id" not in {f.key for f in config.fields}
    assert "status" in {f.key for f in config.fields}


def test_get_device_form_config_loads_device_create_yaml_by_filename() -> None:
    """Sprint 5: the new filename parameter selects device_create.yaml."""
    config = get_device_form_config("device_create.yaml")

    assert config.version
    assert {field.key for field in config.fields} == _CREATE_FIELD_KEYS


def test_device_create_yaml_has_all_ten_required_and_optional_fields() -> None:
    config = get_device_form_config("device_create.yaml")
    by_key = {field.key: field for field in config.fields}

    # Required fields (Sprint 5 Task 2 plan + NetBox semantics)
    assert by_key["device_type_id"].required is True
    assert by_key["role_id"].required is True
    assert by_key["site_id"].required is True
    assert by_key["status"].required is True
    assert by_key["name"].required is True

    # Optional fields (ToR §4.3.4 — 0..N chars)
    assert by_key["rack_id"].required is False
    assert by_key["position"].required is False
    assert by_key["serial"].required is False
    assert by_key["asset_tag"].required is False
    assert by_key["comments"].required is False


def test_device_create_yaml_field_specific_keys_pass_through() -> None:
    """The mobile client receives netbox_field / search_endpoint / depends_on
    untouched via extra='allow' (same as edit form)."""
    config = get_device_form_config("device_create.yaml")
    by_key = {field.key: field.model_dump() for field in config.fields}

    assert by_key["device_type_id"]["netbox_field"] == "device_type"
    assert by_key["role_id"]["netbox_field"] == "role"
    assert by_key["site_id"]["netbox_field"] == "site"
    assert by_key["rack_id"]["depends_on"] == ["site_id"]
    assert by_key["position"]["depends_on"] == ["rack_id"]
    assert by_key["asset_tag"]["netbox_field"] == "custom_fields.asset_tag"
    assert by_key["status"]["choices_endpoint"] == "/api/v1/meta/statuses"


def test_get_device_form_config_caches_per_filename() -> None:
    """Sprint 5: lru_cache keys on the filename — same filename returns the
    same instance; different filenames return different instances."""
    edit_a = get_device_form_config("device_edit.yaml")
    edit_b = get_device_form_config("device_edit.yaml")
    create_a = get_device_form_config("device_create.yaml")
    create_b = get_device_form_config("device_create.yaml")

    assert edit_a is edit_b
    assert create_a is create_b
    assert edit_a is not create_a
