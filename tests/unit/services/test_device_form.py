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
