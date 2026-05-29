"""Server-driven device-edit form configuration. Architecture §5, CLAUDE.md #5.

The form the mobile app renders is described by a YAML file packaged with the
backend, not by mobile code. Adding an editable field means editing
``forms/device_edit.yaml`` and bumping its ``version``, then redeploying — no
mobile release.

The backend validates only the generic *skeleton* — a field has a key, a label,
and one of six generic types. Every other key (``choices_endpoint``,
``confirmation``, ``depends_on``, …) passes through to the mobile client
untouched (``extra='allow'``), so the backend never hardcodes field-specific
knowledge.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

_FORMS_DIR = Path(__file__).resolve().parent / "forms"
_DEFAULT_FORM_FILENAME = "device_edit.yaml"


class FieldType(StrEnum):
    """The six generic field types the mobile app has renderers for (Architecture §5.2)."""

    CHOICE = "choice"
    REFERENCE = "reference"
    INTEGER = "integer"
    TEXT = "text"
    MULTILINE_TEXT = "multiline_text"
    BOOLEAN = "boolean"


class FormField(BaseModel):
    """One editable field.

    Only the generic skeleton is typed; field-specific keys (``choices_endpoint``,
    ``confirmation``, ``depends_on``, ``min``, ``max_from``, ``max_length``,
    ``netbox_field``, …) are accepted and passed through verbatim.
    """

    model_config = ConfigDict(extra="allow")

    key: str
    label: str
    type: FieldType
    required: bool = False


class DeviceFormConfig(BaseModel):
    """The whole form: a ``version`` token (Architecture §5.3) and an ordered field list."""

    version: str
    fields: list[FormField]


def load_device_form_config(path: Path) -> DeviceFormConfig:
    """Parse and validate the form-config YAML at ``path``.

    Raises ``yaml.YAMLError`` on malformed YAML and ``pydantic.ValidationError``
    on a structurally invalid config — a broken file fails loudly rather than
    serving an empty form.
    """
    raw = yaml.safe_load(path.read_text())
    return DeviceFormConfig.model_validate(raw)


@lru_cache
def get_device_form_config(filename: str = _DEFAULT_FORM_FILENAME) -> DeviceFormConfig:
    """A packaged form config, parsed once per filename and cached.

    Sprint 3 shipped one form (``device_edit.yaml``). Sprint 5 Task 2 added
    ``device_create.yaml`` (decision D — separate from edit because creation
    has fields edit doesn't, e.g. ``device_type_id``, ``role_id``). The
    ``filename`` parameter selects which packaged YAML to load; default
    preserves Sprint 3 callers.
    """
    return load_device_form_config(_FORMS_DIR / filename)
