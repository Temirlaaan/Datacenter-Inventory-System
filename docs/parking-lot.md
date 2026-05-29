# Parking lot

Cross-sprint items that aren't part of any current sprint plan: deployment
dependencies owned by people outside the codebase, and Phase 2 hardening that
the MVP deliberately skips. Sprint plans (`sprint-N.md`) and the work-log cover
what's *in* a sprint; this file holds what's parked.

---

## Pending NetBox configuration (deployment dependency for Sprint 5+)

Per ToR §4.3.7, NetBox currently has only `Active` / `Offline` device statuses.
Before the Decommission use case ships, the NetBox admin must add the standard
NetBox statuses:

- `Staged`, `Decommissioning`, `Inventory`, `Failed`

**Owner:** NetBox admin (user)
**Blocker for:** Sprint 5 (Decommission flow — needs `Decommissioning`)
**Not a blocker for:** Sprints 3-4 (Update + bind/retire flows use statuses
discovered dynamically from NetBox via the `/api/v1/meta/statuses` endpoint
— they do not hardcode the status set).

**Slug verification gate (Sprint 5 Task 4 — `app/services/device_decommission.py`):**
The decommission service hardcodes `changes={"status": "decommissioning"}` as
the lowercase NetBox-convention slug. When the NetBox admin adds the status,
record the exact slug returned by `OPTIONS /api/dcim/devices/`'s
`actions.POST.status.choices[].value` field. If it differs from
`"decommissioning"` (e.g. some NetBox installs use display-cased slugs),
update the constant in `device_decommission.py` to match — single call
site, one focused commit. Production deploy of Sprint 5 gates on this
verification.

---

## NetBox custom field name verification (deployment dependency for production)

The code carries two NetBox custom-field names as design assumptions, written
in Sprints 3-4 with respx-mocked NetBox (no live instance available).
Production deploy must verify they match the deployed NetBox schema:

- **`custom_fields.asset_tag`** (Sprint 3) — used by `app/services/forms/device_edit.yaml`
  (the form's `netbox_field: custom_fields.asset_tag`) and by
  `app/services/device.py::to_device_data` (reads
  `device["custom_fields"].get("asset_tag")`). NetBox devices also have a
  *native* `asset_tag` field; if the deployed NetBox uses the native field,
  swap the YAML's `netbox_field` to `asset_tag` and the parser to
  `device["asset_tag"]`.
- **`custom_fields.qr_id`** (Sprint 4) — written by the QR bind flow to attach
  the QR token to the device, and cleared by QR retire on a BOUND QR. Read by
  the combined QR+device response. The exact NetBox custom-field name is
  unverified.

**Owner:** NetBox admin (user) + the engineer running the production deploy.
**Blocker for:** production deploy of Sprints 3-4 — neither sprint can write
to the wrong NetBox field at runtime.
**Not a blocker for:** Sprint 4 development (respx-mocked tests don't depend
on real NetBox; same constraint Sprints 1-3 worked under).
**How to fix if wrong:** each field has exactly one source-of-truth call site
(the YAML for `asset_tag`'s read path; the bind/retire NetBox-write payload
for `qr_id`); isolated by design.

---

## NetBox response shape verification (Sprint 4 Task 3 — pending production)

Three defensive code paths in `to_device_data()` (`app/services/device.py`)
assume specific NetBox response shapes. Production deploy must verify each
against the real NetBox; current respx-mocked tests use the assumed shapes,
so a mismatch only surfaces at runtime.

1. **Device role key.**
   - Code uses: `device.get("role") or device.get("device_role")`
   - NetBox 4.x exposes it under `"role"`; NetBox 3.x under `"device_role"`.
   - Verify which key is actually present in the deployed NetBox version.

2. **`u_height` location.**
   - Code uses: `device["device_type"]["u_height"]`
   - Verify it's nested under `device_type`, not at the device root (in
     some NetBox versions it may appear in both places).

3. **`primary_ip{4,6}` shape.**
   - Code uses: `device["primary_ip4"]["address"]`
   - Verify the field is named `address` (not `value` or other) and that
     it returns the full CIDR notation (e.g. `"192.0.2.10/24"`).

**Owner:** NetBox admin (user) + the engineer running the production deploy.
**Blocker for:** correct device-screen field display in the combined
`GET /api/v1/qr/{qr_id}` response for BOUND QRs. Wrong assumptions silently
return `None` for the affected field rather than crash — so a deploy could
pass smoke tests with a missing manufacturer/role/IP and only surface in
mobile-app QA.
**How to fix if any assumption differs:** adjust the extraction in
`to_device_data` in one focused commit; update the affected unit tests in
`tests/unit/services/test_device.py` to match the real shape.

---

## RBAC: device-create permission level (decided in Sprint 5, revisit post-rollout)

Sprint 5 decision G allows any `dcinv-mobile-user` to call
`POST /api/v1/devices/`. This is consistent with ToR §4.3's role assignments
but may be too permissive in practice — device creation is not a routine
mobile operation (existing devices are scanned, not created), and a
mis-creation pollutes NetBox with hard-to-spot junk records.

Consider after rollout feedback:

- **Option A** — Add a `dcinv-mobile-power-user` Keycloak role for trusted
  engineers; restrict device create + decommission to it. Bind/retire/update
  stay on `dcinv-mobile-user`.
- **Option B** — Restrict device create to `dcinv-admin` only (matches
  decommission's role per decision G). Mobile engineers create-request via
  out-of-band tooling; admin executes.

**Decision deferred** to post-rollout operational feedback. Both options
are non-breaking (tightening a role is just a config swap on the endpoint's
`require_role(...)`). Sprint 5 does not implement.

---

## Phase 2: alerting on three-record partial failures

Sprint 3 cross-cutting Decision B: the three-record write is **NetBox-write-first,
best-effort attribution**. If the NetBox device PATCH succeeds but the NetBox
journal POST or the app-DB `audit_log` row write fails, the backend logs loudly
but does **not** roll back the NetBox change (no distributed transaction exists).
This is acceptable for the MVP.

**Phase 2 must add:** alerting on these partial-failure events — e.g. a count of
`result='partial_failure'` `audit_log` rows per hour, surfaced to whoever owns
operational monitoring. Until then, partial failures are visible only in logs.
