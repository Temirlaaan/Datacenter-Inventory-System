"""Device read. Architecture §3.2, ToR §4.3.

``DeviceService.get_device`` fetches a device from NetBox and returns it with a
``version`` token (the NetBox ``last_updated``) — the value the mobile client
sends back as ``If-Unmodified-Since`` on a later update (optimistic concurrency).

Scoped to the editable-field values the server-driven form pre-fills; the full
ToR §4.3.3 device-screen field set is delivered by Sprint 4's combined
QR+device response.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.netbox.client import NetBoxClient


class StatusRef(BaseModel):
    """A device status — ``value`` is sent to NetBox, ``label`` shown to the user."""

    value: str
    label: str


class ObjectRef(BaseModel):
    """A NetBox object reference (site, rack) — ``id`` for the form, ``name`` for display."""

    id: int
    name: str


class DeviceData(BaseModel):
    """Device-screen field set. ToR §4.3.3.

    Sprint 3 shipped the editable-field subset (decision F: status, site, rack,
    position, name, serial, asset_tag, comments). Sprint 4 Task 3 extended this
    additively with the rest of the ToR §4.3.3 device-screen fields so the
    combined ``QRLookupResponse`` can give the mobile client everything in one
    fetch. All Task-3 additions default to ``None`` so the standalone
    ``GET /api/v1/devices/{id}`` keeps working (its handler doesn't populate
    ``qr_id``; the NetBox payload supplies the rest).
    """

    # --- Sprint 3 (decision F) editable fields ---
    id: int
    name: str
    status: StatusRef
    site: ObjectRef
    rack: ObjectRef | None
    position: int | None
    serial: str
    asset_tag: str | None
    comments: str

    # --- Sprint 4 Task 3 additions (ToR §4.3.3) ---
    device_type: ObjectRef | None = None
    manufacturer: ObjectRef | None = None
    device_role: ObjectRef | None = None
    u_height: int | None = None
    primary_ip4: str | None = None
    primary_ip6: str | None = None
    last_updated: str | None = None
    qr_id: str | None = None  # decision H: app-DB source of truth, not NetBox
    custom_fields: dict[str, Any] | None = None  # populated keys only


class DeviceResponse(BaseModel):
    """A device read result. ``version`` is the optimistic-concurrency token (§3.2)."""

    data: DeviceData
    version: str


_EXTRACTED_CUSTOM_FIELDS = frozenset({"qr_id"})
"""Custom-field keys we surface typed on ``DeviceData`` rather than in the
catch-all ``custom_fields`` map. Production verification 2026-06-04 moved
``asset_tag`` off the custom-fields path onto NetBox 4.x's native field."""


def to_device_data(device: dict[str, Any], *, qr_id: str | None = None) -> DeviceData:
    """Project a raw NetBox device object onto ``DeviceData``.

    The Sprint 4 Task 3 additions are all read defensively (``.get()`` chains)
    so partial test fixtures don't break — production NetBox returns the full
    shape, but unit tests for the editable-field subset don't need to
    enumerate every new key. ``qr_id`` is sourced from the app DB
    (``QRLifecycleService``'s ``qr_codes`` row) — the combined-response
    path passes it explicitly; the standalone read leaves it ``None``
    (decision H).

    NetBox shape assumptions flagged in ``docs/parking-lot.md`` — production
    deploy must verify role-key naming (``role`` vs ``device_role``),
    ``u_height`` location (nested under ``device_type``), and
    ``primary_ip{4,6}`` field name.
    """
    rack = device["rack"]

    device_type_raw = device.get("device_type") or None
    device_type = (
        ObjectRef(
            id=device_type_raw["id"],
            # `display` is NetBox's human-readable label (often "Model X"); fall
            # back to bare `model` when absent.
            name=device_type_raw.get("display") or device_type_raw.get("model") or "",
        )
        if device_type_raw
        else None
    )
    # Manufacturer is nested in device_type per NetBox 3.x/4.x.
    manufacturer_raw = (device_type_raw or {}).get("manufacturer") if device_type_raw else None
    manufacturer = (
        ObjectRef(id=manufacturer_raw["id"], name=manufacturer_raw["name"])
        if manufacturer_raw
        else None
    )

    # NetBox 4.x uses "role"; 3.x uses "device_role". Support both per parking-lot.
    role_raw = device.get("role") or device.get("device_role")
    device_role = ObjectRef(id=role_raw["id"], name=role_raw["name"]) if role_raw else None

    # u_height is a property of the device model, exposed under device_type.
    u_height = (device_type_raw or {}).get("u_height") if device_type_raw else None

    primary_ip4_raw = device.get("primary_ip4")
    primary_ip4 = primary_ip4_raw["address"] if primary_ip4_raw else None
    primary_ip6_raw = device.get("primary_ip6")
    primary_ip6 = primary_ip6_raw["address"] if primary_ip6_raw else None

    # custom_fields: drop None values + drop keys we already expose typed.
    raw_cf: dict[str, Any] = device.get("custom_fields") or {}
    filtered_cf = {
        k: v for k, v in raw_cf.items() if v is not None and k not in _EXTRACTED_CUSTOM_FIELDS
    }
    custom_fields: dict[str, Any] | None = filtered_cf if filtered_cf else None

    return DeviceData(
        id=device["id"],
        name=device["name"],
        status=StatusRef(value=device["status"]["value"], label=device["status"]["label"]),
        site=ObjectRef(id=device["site"]["id"], name=device["site"]["name"]),
        rack=ObjectRef(id=rack["id"], name=rack["name"]) if rack else None,
        position=device["position"],
        serial=device["serial"],
        asset_tag=device.get("asset_tag"),
        comments=device["comments"],
        # Task 3 additions
        device_type=device_type,
        manufacturer=manufacturer,
        device_role=device_role,
        u_height=u_height,
        primary_ip4=primary_ip4,
        primary_ip6=primary_ip6,
        last_updated=device.get("last_updated"),
        qr_id=qr_id,
        custom_fields=custom_fields,
    )


class DeviceUpdateRequest(BaseModel):
    """``PATCH /api/v1/devices/{id}`` payload — decision F's 8 editable fields.

    All fields are optional with PATCH semantics: only fields the client
    explicitly sets are forwarded to NetBox (``model_dump(exclude_unset=True)``,
    the CLAUDE.md #3 pattern). ``extra='forbid'`` rejects unknown keys so a
    client typo (``serial_number`` vs ``serial``) fails loudly instead of being
    silently dropped.

    Bounds mirror the form YAML (``forms/device_edit.yaml``) and ToR §4.3.4.
    """

    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    site_id: int | None = None
    rack_id: int | None = None
    position: int | None = None
    name: str | None = Field(default=None, max_length=64)
    serial: str | None = Field(default=None, max_length=50)
    asset_tag: str | None = Field(default=None, max_length=50)
    comments: str | None = Field(default=None, max_length=1000)


class DeviceCreateRequest(BaseModel):
    """``POST /api/v1/devices/`` payload — Sprint 5 Task 2.

    Field set sourced from:
    - ToR §4.3.4 editable fields (Status, Site, Rack, Position, Name, Serial,
      Asset Tag, Comments) — also settable on CREATE.
    - NetBox semantic requirements (``device_type``, ``role``) — required at
      POST. ToR §4.3.4 marks them "NOT editable from mobile in MVP" but is
      silent on creation; Sprint 5 includes them in the create payload (with
      Task 2 plan flagging this addition explicitly).

    ``extra='forbid'`` catches typos. Length bounds mirror
    ``forms/device_create.yaml`` and ToR §4.3.4.
    """

    model_config = ConfigDict(extra="forbid")

    # Required — NetBox semantics + ToR §4.3.4
    device_type_id: int
    role_id: int
    site_id: int
    status: str  # slug from /api/v1/meta/statuses; no default — engineer picks
    name: str = Field(min_length=1, max_length=64)

    # Optional — ToR §4.3.4 ("0 to N chars" for text; rack/position are
    # explicitly optional on the form)
    rack_id: int | None = None
    position: int | None = None
    serial: str | None = Field(default=None, max_length=50)
    asset_tag: str | None = Field(default=None, max_length=50)
    comments: str | None = Field(default=None, max_length=1000)


def to_netbox_create_payload(req: DeviceCreateRequest) -> dict[str, Any]:
    """Map a ``DeviceCreateRequest`` to NetBox's ``POST /api/dcim/devices/`` body.

    Renames per ``forms/device_create.yaml``'s ``netbox_field``:
    - ``site_id`` → ``site``
    - ``rack_id`` → ``rack``
    - ``device_type_id`` → ``device_type``
    - ``role_id`` → ``role`` (NetBox 4.x convention; NetBox 3.x uses
      ``device_role`` — flagged in ``docs/parking-lot.md`` alongside the
      Sprint 4 Task 3 role-key entry)
    - ``asset_tag`` → ``asset_tag`` (NetBox 4.x native field; verified
      2026-06-04 against the production NetBox)

    Only fields the client provided appear in the output; optional fields
    left as None are omitted.
    """
    payload: dict[str, Any] = {
        "device_type": req.device_type_id,
        "role": req.role_id,
        "site": req.site_id,
        "status": req.status,
        "name": req.name,
    }
    if req.rack_id is not None:
        payload["rack"] = req.rack_id
    if req.position is not None:
        payload["position"] = req.position
    if req.serial is not None:
        payload["serial"] = req.serial
    if req.comments is not None:
        payload["comments"] = req.comments
    if req.asset_tag is not None:
        payload["asset_tag"] = req.asset_tag
    return payload


def to_netbox_changes(request: DeviceUpdateRequest) -> dict[str, Any]:
    """Map a ``DeviceUpdateRequest`` to NetBox's PATCH wire shape.

    Only explicitly-set fields appear in the output (``exclude_unset``):
    omitted fields produce no change; explicit ``null`` is forwarded (e.g.
    ``rack_id=None`` unracks the device).

    Field renames: ``site_id``/``rack_id`` -> ``site``/``rack`` (NetBox's FK
    keys take the raw id); ``asset_tag`` is the NetBox 4.x native field
    (verified 2026-06-04 against the production NetBox).
    """
    sent = request.model_dump(exclude_unset=True)
    changes: dict[str, Any] = {}
    if "status" in sent:
        changes["status"] = sent["status"]
    if "site_id" in sent:
        changes["site"] = sent["site_id"]
    if "rack_id" in sent:
        changes["rack"] = sent["rack_id"]
    if "position" in sent:
        changes["position"] = sent["position"]
    if "name" in sent:
        changes["name"] = sent["name"]
    if "serial" in sent:
        changes["serial"] = sent["serial"]
    if "asset_tag" in sent:
        changes["asset_tag"] = sent["asset_tag"]
    if "comments" in sent:
        changes["comments"] = sent["comments"]
    return changes


class DeviceSearchResponse(BaseModel):
    """``GET /api/v1/devices/search`` result envelope (Sprint 9 Task 1).

    Each entry is the same ``DeviceResponse`` shape mobile already consumes
    from ``GET /devices/{id}`` so the search-result list can feed directly
    into the device-detail / edit flow without a re-fetch.
    """

    results: list[DeviceResponse]
    page: int
    page_size: int
    has_more: bool


class DeviceService:
    """Reads devices from NetBox."""

    def __init__(self, netbox_client: NetBoxClient) -> None:
        self._netbox = netbox_client

    async def get_device(self, device_id: int) -> DeviceResponse:
        """Fetch device ``device_id`` from NetBox. Raises ``NetBoxNotFound`` if absent."""
        device = await self.get_device_raw(device_id)
        return DeviceResponse(data=to_device_data(device), version=device["last_updated"])

    async def get_device_raw(self, device_id: int) -> dict[str, Any]:
        """Like ``get_device`` but returns the raw NetBox payload.

        Used by the combined ``QRLookupResponse`` path so the lookup service can
        inject the app-DB ``qr_id`` when calling ``to_device_data`` (decision H).
        Same error contract as ``get_device``: ``NetBoxNotFound`` for 404,
        ``NetBoxServerError`` / ``NetBoxClientError`` otherwise.
        """
        response = await self._netbox.get(f"/api/dcim/devices/{device_id}/")
        return response.json()  # type: ignore[no-any-return]

    async def search(
        self,
        *,
        name: str | None = None,
        asset_tag: str | None = None,
        serial: str | None = None,
        site_id: int | None = None,
        rack_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> DeviceSearchResponse:
        """Search NetBox devices by name / asset_tag / serial / site / rack.

        Sprint 9 Task 1. Mobile-driven: when an engineer can't read a stuck
        QR sticker, they search by device name / serial / asset tag to
        locate the row. The endpoint is read-only; no audit row.

        Pagination is 1-indexed (``page=1`` is the first page); we request
        ``page_size + 1`` from NetBox and trim to detect ``has_more``
        without a separate COUNT call — mirrors the audit-log pagination
        pattern from Sprint 7.

        Filter mapping to NetBox query params:
        - ``name`` → ``name__ic`` (case-insensitive contains)
        - ``asset_tag`` → ``asset_tag`` (exact)
        - ``serial`` → ``serial`` (exact)
        - ``site_id`` → ``site_id``
        - ``rack_id`` → ``rack_id``
        """
        params: dict[str, Any] = {
            "limit": page_size + 1,
            "offset": (page - 1) * page_size,
        }
        if name:
            params["name__ic"] = name
        if asset_tag:
            params["asset_tag"] = asset_tag
        if serial:
            params["serial"] = serial
        if site_id:
            params["site_id"] = site_id
        if rack_id:
            params["rack_id"] = rack_id
        response = await self._netbox.get("/api/dcim/devices/", params=params)
        payload = response.json()
        raw_devices: list[dict[str, Any]] = payload.get("results", [])
        has_more = len(raw_devices) > page_size
        trimmed = raw_devices[:page_size]
        return DeviceSearchResponse(
            results=[
                DeviceResponse(data=to_device_data(d), version=d["last_updated"])
                for d in trimmed
            ],
            page=page,
            page_size=page_size,
            has_more=has_more,
        )
