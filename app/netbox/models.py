"""Pydantic models for NetBox responses — minimal Sprint 1/2 fields only.

Anti-bloat rule (CLAUDE.md cross-cutting #1): NetBox is the source of truth, so we
keep app-side models intentionally thin. Add fields only when a real call site needs
them, never speculatively.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _NetBoxModel(BaseModel):
    """Base for all NetBox response models. Allows extra fields so NetBox upgrades
    that add new keys don't break parsing — we just ignore what we don't need."""

    model_config = ConfigDict(extra="ignore")


class Status(_NetBoxModel):
    """`/api/status/` payload — surfaces NetBox version for /health and debug logs."""

    netbox_version: str = Field(alias="netbox-version")


class Site(_NetBoxModel):
    id: int
    name: str
    slug: str


class Rack(_NetBoxModel):
    id: int
    name: str
    site: Site


class DeviceStatus(_NetBoxModel):
    value: str
    label: str


class Device(_NetBoxModel):
    id: int
    name: str
    status: DeviceStatus
    # last_updated is required: it's the value we stamp into If-Unmodified-Since
    # for optimistic concurrency on writes (CLAUDE.md cross-cutting #3).
    last_updated: datetime
