# Sprint 5 — Device Write Completion

> **Status:** Planned. Awaiting Task 1 go/no-go.
> **Duration target:** 5–7 working days
> **Goal:** Close out the mobile-side device write surface deferred from Sprint
> 4: device creation, device decommission (with bound-QR retire), and the
> add-comment endpoint. All three ride on the Sprint 3/4 apparatus
> (`NetBoxWriteService`, three-record write, `QRLifecycleService.retire`).

## Why this sprint exists

Sprint 4 closed the QR lifecycle (bind, retire, combined QR+device read). The
device side, however, is still half-done — Sprint 3 shipped device read +
update, leaving creation, decommission, and add-comment for Sprint 4+. Sprint
4 explicitly punted those to here because they are all single-NetBox-write
endpoints that the existing apparatus already supports — there is no new
architecture to build, just three more endpoints.

The most interesting task is **decommission**, because it composes the device
PATCH with the QR retire from Sprint 4. Sprint 4 Task 2 deliberately designed
`QRLifecycleService.retire(qr_id, expected_version, user)` as a callable
the decommission flow would reuse directly — that hook is finally used here.

## Scope boundaries

**In scope — 5 tasks:**

1. `post_with_attribution` apparatus extension (peer of
   `patch_with_attribution`, used by Tasks 2 + 3)
2. Device create (`POST /api/v1/devices/`) + creation form
3. Add-comment endpoint (`POST /api/v1/devices/{id}/comments`)
4. Device decommission (`POST /api/v1/devices/{id}/decommission`)
5. Acceptance + close-out

**Out of scope (deferred to Sprint 6+):**

- **`shift_sessions` table + `POST /api/v1/sessions/{start,end}`** — Sprint 6;
  audit rows already carry `session_id` from the JWT `sid` claim (Sprint 3
  decision C), so the table is only needed when shift-start/end UX lands.
- **`GET /api/v1/admin/audit` query endpoint** — Sprint 6 candidate; the
  compensation audit rows from Sprint 4 are now interesting to query, so this
  is closer than it was before, but still not a Sprint 5 blocker.
- **Idempotency-key TTL cleanup job** — pre-existing deferral.
- **PDF label generation** (`GET /api/v1/admin/batches/{id}/pdf`) — Sprint 7;
  blocked on Architecture §11.1 (reportlab vs weasyprint vs fpdf2).
- **Web admin pages** — Sprint 7+.
- **NetBox circuit breaker** (Architecture §3.3) — still deferred (Sprint 3
  decision D).
- **Error-shape unification on `GET /qr/{id}`** — deferred from Sprint 4;
  Sprint 6 candidate. Sprint 5 stays focused on the three device endpoints.
- **Mobile app (Kotlin/Compose)** — separate workstream, Sprint 8+.

## Cross-cutting decisions

These apply across multiple tasks; capturing once so each task doesn't
re-litigate. Locked in for Sprint 5 — corrections welcome during Task 1's
go/no-go gate.

**A. `DeviceDecommissionService` as a service class, not inline orchestration.**
Decommission composes two sub-operations (device PATCH +
optional QR retire). The orchestration is complex enough — state check,
optional retire-call, joining two audit rows — that a dedicated service
mirrors `QRLifecycleService` and keeps the endpoint handler thin.
Decommission is also a future reuse point for a hypothetical
batch-decommission admin tool.

**B. Decommission body — `{version: str, reason: str | None}`.**
`version` (mandatory) is the device's expected `last_updated` for the
status-PATCH's optimistic concurrency check. `reason` (optional) lands in the
journal entry comment AND, if a bound QR is retired, in the QR retire's
journal/audit attribution too.

**C. Decommission order: QR-first, then device PATCH, with explicit
compensation on post-retire device-PATCH failure.** Mirrors Sprint 4's
three-branch compensation pattern, this time applied to the decommission
transition.

QR-first chosen because the post-failure state ("QR retired without device
status change") is more recoverable than the reverse ("device decommissioning
with still-bound QR"). But "more recoverable" still has a failure mode — if
the device PATCH fails after a successful QR retire, the system has a
mismatch (QR side done, device side not). Sprint 5 handles this with
compensation, not "log and propagate":

1. **If device PATCH fails after QR retire succeeded**: attempt to **re-bind
   the QR** to the device — restore the `qr_codes` BOUND row (status=BOUND,
   bound_to_device_id=device_id, bound_at restored from the original retire's
   recorded value) AND NetBox PATCH `custom_fields.qr_id` back to the QR
   token. This uses `QRLifecycleService.bind` directly (the same hook that
   Task 1 / Sprint 4 Task 2 designed for reuse).
2. **If compensation re-bind succeeds**: log `error`
   `device_decommission_db_failed_qr_recompensated` with `{device_id, qr_id,
   request_id}`, return 500 "Decommission failed (rolled back)". System is
   left consistent (QR is BOUND again on the still-Active device).
3. **If compensation re-bind also fails**: log `critical`
   `device_decommission_inconsistency_unrecoverable` with `{device_id,
   qr_id, request_id, original_error, compensation_error}`. Write a NetBox
   journal entry (`kind="danger"`) on the device naming the inconsistency.
   Return 500 "Decommission failed, manual cleanup required".

If there is **no bound QR** to begin with, the flow degenerates to a simple
device PATCH — no QR retire, no compensation needed.

Decommission's compensation re-uses Sprint 4's compensation pattern as much
as possible, but the **re-bind compensation calls `QRLifecycleService.bind`**
(rather than a direct `_compensate_*` helper) — re-bind is itself a full
state transition (free→bound) with its own atomicity requirements; bypassing
the lifecycle service would re-implement that orchestration.

**D. Device-create form — separate `device_create.yaml` + separate endpoint
`GET /api/v1/meta/device-create-form`.** Creation has fields edit doesn't
(`device_type_id`, `role_id` are required at creation but immutable post-
creation in our app's surface). Sharing one YAML with a `creation_only` flag
forces the mobile client to filter; separate configs keep both wire formats
clean. Sprint 3's `device-edit-form` endpoint stays untouched.

**E. Add-comment body — `{comment: str}`, `max_length=2000`, `extra="forbid"`.**
NetBox journal `comments` is a large free-form text field, but the
add-comment use case is per-incident notes (RMA numbers, ticket refs,
observation context). 2k chars gives breathing room for technical notes
without bloating the audit_log JSONB payload (50 ops/day × 2k chars =
100k/day, vs 500k/day at 10k). If someone needs more than 2k chars in a
single comment, that's a symptom of misuse (use the ticket tracker, not
add-comment). No `kind` parameter — comments default to NetBox
`kind="info"` (same as `_format_journal_comment` in Sprint 3).

**F. `post_with_attribution` as a peer of `patch_with_attribution`** on
`NetBoxWriteService`. Generic over POST. Differences:
- No re-read / version compare (POST creates, doesn't mutate).
- No `WriteConflictError` path.
- Still writes journal entry + audit row (three-record write per CLAUDE.md
  cross-cutting rule #2).
- `before_json={}` on the audit row (nothing existed before).
- Used by Task 2 (device create) and Task 3 (add-comment — where the
  "thing created" is the journal entry, not a device).

**G. Decommission role: `dcinv-admin`** (mirrors Sprint 4 decision I: retire
is destructive, safer default is admin). Device create + add-comment are
mobile operations: role `dcinv-mobile-user`.

**H. Device-create's audit `entity_id`** — the newly-created NetBox device's
`id` (from the POST response). For the audit row's `entity_id`, we wait for
NetBox to assign the id, then use it. Same pattern Sprint 4's `qr.bind`
introduced for `entity_id` separate from `netbox_object_id`.

**I. Add-comment uses `post_with_attribution` but with `entity_type="device"`
and `entity_id=device_id`** — the journal entry attaches to the device, so
the audit row attributes to the device. No NetBox object is created from the
backend's perspective other than the journal entry itself (which we don't
need to track).

## Task list

Each task is detailed below with Goal / Steps / Acceptance criteria /
Anti-criteria / Suggested prompt, mirroring `docs/sprint-3.md` and
`docs/sprint-4.md`. Per the working principles, every task gets its own
plan-then-confirm gate **before** any code lands.

---

### Task 1 — `post_with_attribution` apparatus extension

**Goal:** Add `NetBoxWriteService.post_with_attribution(...)` — a generic
NetBox POST + journal entry + audit row, parallel to `patch_with_attribution`.
Used by Task 2 (device create — POSTs the device) and Task 3 (add-comment —
POSTs only the journal entry, audit row attributes to the device). Generic
over the NetBox path so the call site picks `/api/dcim/devices/` for create
vs `/api/extras/journal-entries/` for add-comment.

**Steps:**

1. Add to `app/services/netbox_write.py`:
   ```python
   async def post_with_attribution(
       self,
       *,
       netbox_path: str,
       netbox_object_type: str,
       netbox_object_id: int | None,   # None for new-object creates
       entity_type: str,
       entity_id: str | None,           # None → derived from POST response
       operation: str,
       payload: dict[str, Any],
       user: AuthUser,
       attach_journal: bool = True,     # False for add-comment (the POST IS the journal)
   ) -> dict[str, Any]:
   ```
2. Three execution phases:
   - **POST**: `netbox_client.post(netbox_path, json=payload)` → `created`.
     Any exception → `FAILURE` audit row + re-raise.
   - **Optional journal POST**: if `attach_journal=True`, post a NetBox
     journal entry attaching to `netbox_object_type` / `netbox_object_id`
     (or the newly-created `created["id"]` when `netbox_object_id is None`).
     Best-effort (decision B).
   - **`SUCCESS` audit row**: same shape as `patch_with_attribution`. Best-effort.
3. Audit row's `entity_id` resolution:
   - If `entity_id` is provided → use it (e.g. add-comment passes
     `entity_id=str(device_id)`).
   - Otherwise → `str(created["id"])` (e.g. create passes None,
     derives from POST response).
4. `before_json={}` (nothing pre-existed); `after_json={"object": created}`.
5. Same `request_id` sharing pattern (`current_request_id()` contextvar).
   Same `session_id` from `AuthUser.session_id` (decision C).
6. Tests (`tests/unit/services/test_netbox_write.py` extension):
   - `test_post_with_attribution_returns_created_object_on_success`
   - `test_post_with_attribution_success_writes_success_audit_row`
   - `test_post_with_attribution_success_audit_records_created_object`
   - `test_post_with_attribution_uses_provided_entity_id_when_set`
   - `test_post_with_attribution_derives_entity_id_from_response_when_none`
   - `test_post_with_attribution_writes_failure_audit_when_post_fails`
   - `test_post_with_attribution_succeeds_when_journal_post_fails` (decision B)
   - `test_post_with_attribution_succeeds_when_audit_insert_fails` (decision B)
   - `test_post_with_attribution_skips_journal_when_attach_journal_false`
   - `test_post_with_attribution_audit_request_id_matches_contextvar`
   - Integration test extending `tests/integration/test_netbox_write.py` with
     a POST-flow happy path.

**Acceptance criteria:**

- `pytest` passes; 100% line + branch coverage on the new lines in
  `app/services/netbox_write.py`.
- ruff + black + mypy clean.
- `post_with_attribution` returns the created object dict from NetBox.
- `entity_id` resolution works in both modes (explicit + derived).
- Journal POST is skipped when `attach_journal=False` (Task 3 uses this).
- Decision B holds: journal/audit failures after a successful POST are logged,
  never fatal.

**Anti-criteria:**

- Don't add optimistic concurrency to POST — it's a create, not a mutation.
- Don't add a separate `WriteCreateError` exception class — re-use the
  existing NetBox client errors.
- Don't widen `patch_with_attribution` to do POSTs via a flag — that
  conflates two distinct operations.
- Don't auto-detect `entity_id is None` and skip the audit row; always
  write an audit row (decision B's "every outcome produces an audit row").

**Suggested prompt:**

```
Implement Sprint 5 Task 1: post_with_attribution on NetBoxWriteService.
Generic NetBox POST + journal entry + audit row, parallel to
patch_with_attribution. Takes entity_id: str | None (caller-provided)
falling back to str(created["id"]) from the POST response; takes
attach_journal: bool=True so the add-comment path (Task 3) can skip
the journal POST. before_json={}, after_json={"object": created}.
Decision B uniform: journal + audit best-effort after the POST returns.
TDD: respx-mocked unit tests + a POST-flow integration test, 100%
coverage on the new lines.
```

---

### Task 2 — Device create

**Goal:** `POST /api/v1/devices/` (role `dcinv-mobile-user`) creates a new
NetBox device, drives Task 1's `post_with_attribution`, returns the created
`DeviceResponse`. A new `device_create.yaml` + `GET /api/v1/meta/device-create-form`
lets the mobile client render the creation form server-side (decision D —
separate from the edit form).

**Steps:**

1. `app/services/forms/device_create.yaml` — fields per ToR §4.3 (device
   create flow). Confirmed during detail plan via ToR §4.3 read. Minimum fields:
   - `device_type_id` (reference, required, choices_endpoint TBD or hardcoded
     list — TBD pending ToR §4.3 read)
   - `role_id` (reference, required)
   - `site_id` (reference, required)
   - `name` (text, max_length=64, required)
   - `status` (choice, required, defaults to "active")
   - `rack_id` (reference, optional)
   - `position` (integer, optional, requires rack_id)
   - `serial` (text, max_length=50, optional)
   - `asset_tag` (text, max_length=50, optional)
   - `comments` (multiline_text, max_length=1000, optional)
2. `app/services/device.py` — add:
   - `DeviceCreateRequest` (Pydantic, `extra="forbid"`): required fields
     non-optional, optional fields default `None`. Length bounds mirror the YAML.
   - `to_netbox_create_payload(request) -> dict[str, Any]`: maps Pydantic
     field names to NetBox keys (same rename pattern as `to_netbox_changes`:
     `site_id → site`, `rack_id → rack`, `device_type_id → device_type`,
     `role_id → role`, `asset_tag → custom_fields.asset_tag`).
3. `app/api/v1/devices.py` — add `POST /` endpoint:
   - Role `dcinv-mobile-user`.
   - Uses `get_write_service` dep (existing, Sprint 3).
   - Calls `write_service.post_with_attribution(...)`:
     - `netbox_path="/api/dcim/devices/"`
     - `netbox_object_type="dcim.device"`, `netbox_object_id=None`
     - `entity_type="device"`, `entity_id=None` (derived from response)
     - `operation="device.create"`
     - `payload=to_netbox_create_payload(request)`
     - `attach_journal=True`
   - Returns `DeviceResponse(data=to_device_data(created), version=created["last_updated"])`.
4. `app/api/v1/meta.py` — add `GET /device-create-form`:
   - Role `dcinv-mobile-user`.
   - `response_model=DeviceFormConfig` (existing Sprint 3 type).
   - Loads `app/services/forms/device_create.yaml`.
5. `app/services/device_form.py` — extend `get_device_form_config` to take a
   filename parameter (default `device_edit.yaml`), so the same loader serves
   both endpoints. Sprint 3's `device-edit-form` keeps its existing URL.
6. Error → HTTP mapping (endpoint), **with targeted NetBox-4xx translation
   per Correction 2** (UX fix: don't surface NetBox "name already exists" as
   "bad gateway"):

   **Apparatus prep** (extends Sprint 1's NetBox error hierarchy — needed
   here, not later):
   - Add `NetBoxValidationError(NetBoxClientError)` to `app/netbox/errors.py`,
     carrying `status_code: int` and `detail: str | dict[str, Any]`.
   - Update `app/netbox/client.py::_send`: on 4xx responses (other than 404),
     read the JSON body (or fall back to `response.text` if non-JSON) and
     raise `NetBoxValidationError(status_code=resp.status_code, detail=<body>)`.
     `NetBoxNotFound` (404) stays as-is. Other 4xx → `NetBoxValidationError`.
   - Tests: extend `tests/unit/netbox/test_client.py` with 400/422-body
     handling.

   **Endpoint handling**:
   ```python
   try:
       created = await write_service.post_with_attribution(...)
   except NetBoxValidationError as exc:
       return JSONResponse(
           status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
           content={"error": {
               "code": "NETBOX_VALIDATION_ERROR",
               "message": str(exc.detail) if isinstance(exc.detail, str) else "NetBox rejected the create request",
               "netbox_status": exc.status_code,
               "netbox_detail": exc.detail,
           }},
       )
   # NetBoxNotFound / NetBoxServerError / generic NetBoxClientError still flow
   # through the global handlers (404 / 502).
   ```
   - This is **targeted handling for the known UX failure mode**, NOT a full
     error-translation framework. Sprint 6 candidate for broader coverage
     (e.g. add-comment's NetBox 400 path, decommission's 4xx).
7. Tests:
   - `tests/unit/services/test_device.py` — `DeviceCreateRequest` validation
     (required fields, length bounds, extra="forbid"); `to_netbox_create_payload`
     (renames, optional-field exclusion).
   - `tests/unit/services/test_device_form.py` — `device_create.yaml` loads,
     `get_device_form_config` accepts the new filename param.
   - `tests/unit/api/v1/test_device_create.py` — handler direct-await
     (happy + NetBox 400 → 422 + NetBox 502); `AsyncClient` for routing, 403
     without mobile role, 422 on missing required field / extra body field.
     **Correction 2 test:**
     `test_device_create_returns_422_with_netbox_error_on_validation_failure`
     — respx returns 400 with body
     `{"name": ["device with this name already exists."]}`; assert response is
     422 with `error.code="NETBOX_VALIDATION_ERROR"`,
     `error.netbox_status=400`, `error.netbox_detail` carries the parsed body.
   - `tests/unit/api/v1/test_meta.py` — extend with `device-create-form`
     endpoint scenarios.
   - `tests/integration/test_device_create.py` — respx + real Postgres:
     POST happy path lands SUCCESS audit row with `entity_id` matching the
     created device's id; POST failure path lands FAILURE audit row.

**Acceptance criteria:**

- `pytest` passes; 100% line + branch coverage on changed files.
- ruff + black + mypy clean.
- `POST /api/v1/devices/` → 201 on success with `DeviceResponse`; 422 on
  missing required field or `extra` field; 502 on NetBox error.
- Audit row landed with `operation="device.create"`, `entity_type="device"`,
  `entity_id` = newly-created device's id (string).
- `GET /api/v1/meta/device-create-form` returns the new YAML config.

**Anti-criteria:**

- Don't reuse `device_edit.yaml` with a flag — separate file per decision D.
- Don't add optimistic concurrency to the create — it's a POST.
- Don't return 201 with a body matching `device_edit-form` semantics; return
  the same `DeviceResponse` shape Sprint 3's read/update use.
- Don't bypass `post_with_attribution` to call the NetBox client directly —
  the three-record-write rule applies (CLAUDE.md #2).
- Don't translate NetBox 4xx to anything other than 502 in this task —
  Sprint 6 candidate for richer error translation.

**Suggested prompt:**

```
Implement Sprint 5 Task 2: device create. New device_create.yaml form
config + GET /api/v1/meta/device-create-form. New DeviceCreateRequest
+ to_netbox_create_payload in app/services/device.py. New POST
/api/v1/devices/ endpoint (role dcinv-mobile-user) driving
post_with_attribution. Returns DeviceResponse on success; 422 on
validation error; 502 on NetBox error. TDD, 100% coverage. No
optimistic concurrency on POST.
```

---

### Task 3 — Add-comment endpoint

**Goal:** `POST /api/v1/devices/{id}/comments` (role `dcinv-mobile-user`)
appends a NetBox journal entry to the given device. No device PATCH, no
optimistic concurrency. The "thinnest" task: just a journal POST + audit row
via `post_with_attribution(attach_journal=False, ...)` — the POST itself IS
the journal entry.

**Steps:**

1. `app/services/comment.py` — new `CommentService`:
   - DI: `NetBoxWriteService`.
   - `add_comment(device_id: int, comment: str, user: AuthUser) -> dict[str, Any]`:
     - Build the journal payload (`{assigned_object_type: "dcim.device",
       assigned_object_id: device_id, kind: "info", comments: comment}`).
     - Call `write_service.post_with_attribution(...)`:
       - `netbox_path="/api/extras/journal-entries/"`
       - `netbox_object_type="dcim.device"`, `netbox_object_id=device_id`
       - `entity_type="device"`, `entity_id=str(device_id)`
       - `operation="device.add_comment"`
       - `payload=<the journal entry payload above>`
       - `attach_journal=False` — the POST IS the journal entry; we don't
         need to write a *second* journal entry attributing the journal entry.
     - Returns the created journal entry dict.
2. `app/api/v1/devices.py` — add `POST /{device_id}/comments`:
   - Role `dcinv-mobile-user`.
   - Pydantic `AddCommentRequest {comment: str}` with `max_length=2000`
     (Correction 3 — per-incident notes, bounds audit_log JSONB growth),
     `extra="forbid"`.
   - Returns `JSONResponse(status_code=201, content={"id": created["id"]})`
     — the mobile client only needs to know the journal entry was recorded;
     full payload not useful.
   - Error mapping: `NetBoxNotFound → 404` (device gone),
     `NetBoxClientError → 502` (global handlers).
3. Tests:
   - `tests/unit/services/test_comment.py`:
     - `test_add_comment_calls_post_with_attribution_with_device_attribution`
     - `test_add_comment_uses_attach_journal_false`
     - `test_add_comment_returns_created_journal_entry`
     - `test_add_comment_propagates_netbox_not_found`
     - `test_add_comment_propagates_netbox_client_error`
   - `tests/unit/api/v1/test_device_comments.py`:
     - Handler direct-await happy + 404 + 502
     - `AsyncClient`: 201, 403 without mobile role, 422 on empty body / extra
       field / over-length comment
   - `tests/integration/test_device_comments.py`:
     - respx + real Postgres: happy path lands SUCCESS audit row with
       `operation="device.add_comment"`, `entity_id=str(device_id)`.

**Acceptance criteria:**

- `pytest` passes; 100% line + branch coverage on new files.
- ruff + black + mypy clean.
- `POST /api/v1/devices/{id}/comments` → 201 on success with `{"id": <jid>}`;
  422 on missing/extra/over-length body; 404 on unknown device; 502 on NetBox
  error.
- Audit row landed with `operation="device.add_comment"`,
  `entity_type="device"`, `entity_id=str(device_id)`.
- No secondary journal entry attached (`attach_journal=False` honored).

**Anti-criteria:**

- Don't expose a `kind` parameter — hardcode `"info"`.
- Don't add a NetBox PATCH — this is journal-only.
- Don't trim/normalize the comment text — pass through as-is (NetBox handles
  display formatting; mobile-app input is operator-trusted).
- Don't return the full journal entry payload — `{"id": <jid>}` is enough.

**Suggested prompt:**

```
Implement Sprint 5 Task 3: add-comment endpoint. New CommentService
(app/services/comment.py) wrapping post_with_attribution with
attach_journal=False. New POST /api/v1/devices/{id}/comments (role
dcinv-mobile-user, body {comment: str max_length=10000 extra=forbid}).
Returns 201 {"id": <journal_id>}. TDD, 100% coverage.
```

---

### Task 4 — Device decommission

**Goal:** `POST /api/v1/devices/{id}/decommission` (role `dcinv-admin`) sets
the device's NetBox status to `Decommissioning` and, if the device has a
bound QR, retires that QR in the same flow via Sprint 4's
`QRLifecycleService.retire(...)`. QR-first ordering (decision C) keeps the
failure modes recoverable.

**Steps:**

1. **Step 0 — apparatus prep: extend `QRLifecycleService.retire` return
   signature** so decommission's Step D compensation can capture the
   post-retire device version.
   - Current (Sprint 4): `retire(qr_token, expected_version, user) -> QR`
   - New: `retire(qr_token, expected_version, user) -> tuple[QR, dict[str, Any] | None]`
     - BOUND path: returns `(retired_qr, updated_device_dict)` —
       `updated_device_dict` is `patch_with_attribution`'s return value.
     - FREE path: returns `(retired_qr, None)` — no NetBox call, no device.
   - **Callers to update**: `app/api/v1/qr.py::retire_qr` (uses just the
     QR — destructure as `retired_qr, _ = await lifecycle.retire(...)`).
     The endpoint behavior doesn't change.
   - **Tests to update**: all retire unit + endpoint + integration tests
     that assert on the return value (currently expect `QR`; change to
     destructure). Mechanical churn, no behavior change.
   - Mirrors Sprint 4 `bind`'s `tuple[QR, dict[str, Any]]` return shape.

2. `app/services/device_decommission.py` — new `DeviceDecommissionService`
   + new exceptions:
   - `DeviceDecommissionRolledBackError(device_id, qr_id)` — Branch 2 from
     decision C: device PATCH failed after QR retire, re-bind compensation
     succeeded, system consistent.
   - `DeviceDecommissionInconsistencyError(device_id, qr_id)` — Branch 3:
     re-bind compensation also failed; manual cleanup required.
   - DI: `NetBoxClient`, `AsyncSession`, `QRCodeRepository`,
     `QRBatchRepository`, `NetBoxWriteService`, `QRLifecycleService`.
   - `decommission(device_id, expected_version, reason, user) ->
     DeviceResponse`:
     - **Defensive guard**: `if session.in_transaction(): raise RuntimeError(...)`
       (same pattern as `QRLifecycleService.bind/retire`).
     - **Step A — find bound QR** (read-only): query `qr_codes` WHERE
       `bound_to_device_id = device_id AND status = 'bound'`. Either one
       `QR` or `None` (the `qr_one_per_device` partial unique index
       guarantees ≤1).
     - **Step B — retire bound QR** (if found): call
       `lifecycle_service.retire(qr_id=bound_qr.id,
       expected_version=expected_version, user=user)`.
       - Most exceptions (WriteConflictError, NetBoxNotFound, etc.) propagate
         as-is. No device PATCH happened yet → nothing to undo. Endpoint maps
         the exception.
       - **`QRRetireRolledBackError`** (Sprint 4 Branch 2 — compensation
         succeeded, system consistent): propagate as-is.
       - **`QRRetireInconsistencyError`** (Sprint 4 Branch 3 — compensation
         failed, system semi-broken) — **Correction 4**: explicitly catch,
         log critical `device_decommission_aborted_qr_inconsistent` with
         `{device_id, qr_id, request_id}`, and re-raise. The decommission
         CANNOT continue (the QR is in an undefined state on NetBox); the
         endpoint translates this to a structured 500
         `QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT` telling the operator a
         manual cleanup is required before retrying.
     - **Step C — device status PATCH** via `patch_with_attribution`:
       - `netbox_path="/api/dcim/devices/{device_id}/"`
       - `entity_type="device"`, `entity_id=str(device_id)`
       - `operation="device.decommission"`
       - `expected_version=expected_version`
       - `changes={"status": "decommissioning"}` — **Correction 1**:
         lowercase slug is the assumed NetBox convention but is **not
         verified** against the deployed NetBox. Hardcoded for Sprint 5
         with a TODO comment in code referencing
         `docs/parking-lot.md`'s "Pending NetBox configuration" entry:
         when the admin adds the status, record the exact slug from
         `OPTIONS /api/dcim/devices/`'s `actions.POST.status.choices[].value`
         and update this constant if it differs. Production deploy gates
         on the verification.
       - **On failure when a QR was retired in Step B** (decision C
         compensation): catch the exception (WriteConflictError,
         NetBoxNotFound, NetBoxClientError, anything), invoke
         `_compensate_rebind_qr(qr_id, device_id, expected_version, user)`
         (Step D below), translate the outcome to one of two terminal
         exceptions:
         - **Branch 2 — re-bind succeeded**: log `error`
           `device_decommission_db_failed_qr_recompensated`; raise
           `DeviceDecommissionRolledBackError(device_id, qr_id)`.
         - **Branch 3 — re-bind failed**: log `critical`
           `device_decommission_inconsistency_unrecoverable`; best-effort
           NetBox journal entry on the device (`kind="danger"`, naming the
           inconsistency); raise
           `DeviceDecommissionInconsistencyError(device_id, qr_id)`.
       - **On failure when there was NO bound QR**: nothing to compensate;
         let the exception propagate (endpoint maps to 409/404/502 via
         global handlers + existing exception types).

     - **Step D — `_compensate_rebind_qr(qr_id, device_id,
       post_retire_version, user)`** (private helper on
       `DeviceDecommissionService`): calls
       `QRLifecycleService.bind(qr_id, device_id, post_retire_version, user)`
       to restore the BOUND state (both qr_codes row + NetBox
       `custom_fields.qr_id`). Returns the bound `QR` on success; lets any
       exception propagate to Step C's Branch 3 handler.
       - **`post_retire_version` is captured from Step B's QR retire
         response** — the NetBox PATCH that cleared `custom_fields.qr_id`
         returned an updated device dict with the new `last_updated`. We
         pass that exact version to the compensation re-bind so its
         optimistic-concurrency check is **deterministic**: if a third
         party modified the device between our retire and our compensation
         re-bind, the re-bind raises `WriteConflictError` (logged as
         critical, escalates to Branch 3 inconsistency) rather than
         silently skating past with `expected_version=None`. That
         WriteConflictError is exactly the operational signal we want —
         a concurrent edit during a decommission is unusual and worth
         human attention.
       - **Requires Step 0 (apparatus prep) below:** Sprint 4's
         `QRLifecycleService.retire` currently returns just `QR`. Extend
         it to return `tuple[QR, dict[str, Any] | None]` (mirroring
         `bind`'s return shape), where the dict is the updated device
         (BOUND path) or `None` (FREE path). Decommission captures
         `post_retire_version = retire_response[1]["last_updated"]`.
       - On any exception: the QR is already retired. Log
         `device_decommission_partial_failure` with `{device_id, qr_id,
         original_error}` and propagate. The mobile client gets a 5xx; an
         operator can re-bind a new QR if they need to roll back. Documented
         in the work-log as decision C's accepted failure mode.
     - Returns the updated `DeviceResponse`.
3. Add `QRCodeRepository.find_by_bound_device_id(device_id) -> QR | None`:
   - `SELECT * FROM qr_codes WHERE bound_to_device_id = :id AND status = 'bound'`
   - Returns `None` if no bound QR.
   - Defended by the `qr_one_per_device` partial unique index (≤1 result
     guaranteed).
4. `app/api/v1/devices.py` — add `POST /{device_id}/decommission`:
   - Role `dcinv-admin` (decision G).
   - Pydantic `DeviceDecommissionRequest {version: str, reason: str | None = None}`,
     `extra="forbid"`.
   - Build `DeviceDecommissionService` via a new `get_decommission_service`
     FastAPI dep (mirrors `get_lifecycle_service` from Sprint 4).
   - Error → HTTP mapping:
     - `QRStateConflictError` (from `retire` if bound QR is somehow not
       BOUND) → 409 `QR_STATE_CONFLICT` (mirrors retire endpoint).
     - `WriteConflictError` (from either retire OR device PATCH) → 409
       `DEVICE_CONFLICT` with `current_state` + `current_version` (mirrors
       Sprint 3 update + Sprint 4 retire).
     - `QRRetireRolledBackError` (retire's Branch 2 — system consistent) →
       500 `QR_RETIRE_ROLLED_BACK` (same code as Sprint 4 retire endpoint).
     - **`DeviceDecommissionRolledBackError`** (Q3 compensation Branch 2 —
       device PATCH failed, QR re-bound successfully, system consistent) →
       500 `DECOMMISSION_ROLLED_BACK` with `{device_id, qr_id, message:
       "Decommission failed (rolled back)"}`.
     - **`DeviceDecommissionInconsistencyError`** (Q3 compensation Branch
       3 — re-bind also failed) → 500 `DECOMMISSION_INCONSISTENCY` with
       `{device_id, qr_id, message: "Decommission failed, manual cleanup
       required"}`.
     - **`QRRetireInconsistencyError`** (Correction 4 — retire's Branch 3,
       compensation failed, system semi-broken) → **500
       `QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT`** with structured body:
       ```python
       except QRRetireInconsistencyError as exc:
           return JSONResponse(
               status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
               content={"error": {
                   "code": "QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT",
                   "qr_id": exc.qr_id,
                   "device_id": exc.device_id,
                   "message": "QR is in inconsistent state; manual cleanup "
                              "required before retrying decommission",
               }},
           )
       ```
       Distinct from the `QR_RETIRE_INCONSISTENCY` code retire's own
       endpoint uses — the decommission context is informative for ops
       (they need to know the cleanup must happen before re-attempting
       decommission, not just any retry).
     - `NetBoxNotFound` / `NetBoxClientError` → 404 / 502 via global
       handlers.
   - Returns `DeviceResponse` on success.
5. Tests:
   - `tests/integration/test_repositories.py` — add
     `test_find_by_bound_device_id_returns_qr_when_present` /
     `_returns_none_when_no_bound_qr` / `_returns_none_when_only_retired_qrs_for_device`.
   - `tests/unit/services/test_device_decommission.py`:
     - `test_decommission_with_no_bound_qr_only_patches_device`
     - `test_decommission_with_bound_qr_retires_first_then_patches_device`
     - `test_decommission_propagates_retire_failure_without_patching_device`
     - `test_decommission_propagates_patch_failure_after_qr_retired_logs_partial_failure`
     - `test_decommission_raises_runtime_error_when_called_in_active_transaction`
     - `test_decommission_propagates_write_conflict_from_retire`
     - `test_decommission_propagates_write_conflict_from_status_patch`
     - **Correction 4: `test_decommission_aborts_when_retire_raises_inconsistency_error`**
       — `QRLifecycleService.retire` raises `QRRetireInconsistencyError`;
       assert the service logs `device_decommission_aborted_qr_inconsistent`
       at critical level and re-raises; assert the device PATCH was NOT
       attempted (the system is in semi-broken state, decommission must
       not continue).
     - **Q3 Branch 2: `test_decommission_device_patch_fails_after_qr_retire_compensates_via_rebind`**
       — QR retire succeeds, device PATCH raises (e.g. NetBox 500); assert
       `_compensate_rebind_qr` is called with the right args; assert
       `QRLifecycleService.bind` is invoked; assert the
       `device_decommission_db_failed_qr_recompensated` log key emits at
       error level; assert `DeviceDecommissionRolledBackError(device_id,
       qr_id)` is raised.
     - **Q3 Branch 3: `test_decommission_compensation_rebind_also_fails_raises_inconsistency`**
       — QR retire succeeds, device PATCH raises, re-bind via
       `QRLifecycleService.bind` ALSO raises; assert critical log
       `device_decommission_inconsistency_unrecoverable`; assert a
       best-effort NetBox journal entry (`kind="danger"`) was posted on
       the device naming the inconsistency; assert
       `DeviceDecommissionInconsistencyError(device_id, qr_id)` is raised.
     - **Q3 no-QR path: `test_decommission_device_patch_failure_with_no_bound_qr_propagates_without_compensation`**
       — no bound QR; device PATCH fails; no compensation attempted
       (`_compensate_rebind_qr` never called); exception propagates.
   - `tests/unit/api/v1/test_device_decommission.py`:
     - Handler direct-await: happy (no QR) / happy (with QR) / 409 retire-state
       / 409 device-version / 500 rolled-back / 500 inconsistency / 404 / 502.
     - **Correction 4: `test_decommission_endpoint_returns_qr_inconsistent_error_on_inconsistency_path`**
       — service raises `QRRetireInconsistencyError`; endpoint returns 500
       with `error.code="QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT"`,
       `error.qr_id`, `error.device_id`, and the manual-cleanup message.
     - **Q3 Branch 2: `test_decommission_endpoint_returns_decommission_rolled_back_on_branch_2`**
       — service raises `DeviceDecommissionRolledBackError`; endpoint
       returns 500 `DECOMMISSION_ROLLED_BACK` with device_id + qr_id.
     - **Q3 Branch 3: `test_decommission_endpoint_returns_decommission_inconsistency_on_branch_3`**
       — service raises `DeviceDecommissionInconsistencyError`; endpoint
       returns 500 `DECOMMISSION_INCONSISTENCY` with device_id + qr_id +
       manual-cleanup message.
     - `AsyncClient`: 200, 403 without `dcinv-admin` (including
       `dcinv-mobile-user` → 403), 422 missing version / extra body field.
   - `tests/integration/test_device_decommission.py`:
     - `test_decommission_unbound_device_persists_decommissioning_status_and_audit`
     - `test_decommission_bound_device_retires_qr_and_decommissions_in_one_flow`
     - `test_decommission_returns_409_when_device_version_stale`
     - `test_decommission_endpoint_requires_admin_role`
6. Update `docs/parking-lot.md` if not already there: production deploy gated
   on NetBox admin adding the `Decommissioning` status (existing entry from
   Sprint 4 covers this).

**Acceptance criteria:**

- `pytest` passes; 100% line + branch coverage on changed files.
- ruff + black + mypy clean.
- `POST /api/v1/devices/{id}/decommission` → 200 on success with the
  decommissioned `DeviceResponse`; 409 on stale version or QR state mismatch;
  500 on compensation rolled-back / inconsistency; 404 on unknown device;
  502 on NetBox error.
- Unbound device decommission: 1 audit row (`device.decommission`/`success`).
- Bound device decommission: 2 audit rows (one `qr.retire`/`success`, one
  `device.decommission`/`success`), shared `request_id`.
- Bound QR's `qr_codes` row: BOUND → RETIRED with historical `bound_to_device_id`
  preserved (Sprint 2 domain design).
- `dcinv-mobile-user` alone gets 403 (decision G).
- Partial-failure path: QR retired, device PATCH fails → log
  `device_decommission_partial_failure` emitted, exception propagates,
  client gets 5xx.

**Anti-criteria:**

- Don't add device-side compensation (rolling the device status back on QR
  retire failure) — decision C's "QR-first" ordering makes this unnecessary,
  and the prior device status isn't always trivially recoverable.
- Don't bypass `QRLifecycleService.retire` to inline the QR retire — Sprint 4
  Task 2 designed the retire service for exactly this reuse.
- Don't open the decommission endpoint to `dcinv-mobile-user` (decision G).
- Don't try to make decommission idempotent — Sprint 5 leaves
  retry-on-failure to the mobile client + operator follow-up.

**Suggested prompt:**

```
Implement Sprint 5 Task 4: device decommission. New
DeviceDecommissionService (app/services/device_decommission.py) does
QR-first ordering: find bound QR via new
QRCodeRepository.find_by_bound_device_id, retire it via
QRLifecycleService.retire if present, then PATCH the device status to
"decommissioning" via patch_with_attribution. POST
/api/v1/devices/{id}/decommission (role dcinv-admin, body {version,
reason}) returns the updated DeviceResponse. Partial-failure (QR
retired, device PATCH fails) logs device_decommission_partial_failure
and propagates — no device-side compensation per decision C. TDD,
100% coverage.
```

---

### Task 5 — Acceptance and close-out

**Goal:** Sprint 5 done means tests green, quality gates clean, stack still
runs, the cross-cutting and per-task decisions are captured in the work-log,
and Sprint 6 has a clean handoff.

**Steps:**

1. **Full test run:** `uv run pytest --cov=app --cov-branch --cov-fail-under=100`.
2. **Quality gates:** `uv run ruff check`, `uv run black --check`,
   `uv run mypy app/`. All clean.
3. **Stack smoke:** `docker compose up -d --build`; `curl localhost:8000/health`;
   `docker compose down -v`. (Skip if no local Keycloak/NetBox; same as
   Sprints 1-4.)
4. **Work-log entry** in `docs/work-log.md` ("Sprint 5 — Device Write
   Completion"), mirroring Sprint 4's structure:
   - What shipped (per-task table).
   - Quality bar at close (test count, coverage, lint/types).
   - Pyproject deviations (expected: none).
   - Architectural decisions worth carrying forward — especially:
     `post_with_attribution` as the create-path peer of
     `patch_with_attribution`; QR-first decommission ordering; the
     decision C partial-failure log key.
   - Sprint 5 retrospective.
   - Discrepancies between ToR / Architecture and what shipped.
   - Deliberately deferred (Sprint 6 candidates: sessions + audit query +
     error-shape unification on `GET /qr/{id}`).
   - Files added / modified.
5. **CLAUDE.md** — update Repository Status paragraph: business surface now
   includes device create, decommission, add-comment; Sprint 6 next.
6. **Memory** — add a `project_sprint_5_status.md` entry mirroring
   `project_sprint_4_status.md`; update `MEMORY.md` index.
7. **Parking-lot update** — verify Sprint 4's "Pending NetBox configuration"
   entry still reflects reality (decommission's `Decommissioning` status
   requirement); flag if the NetBox admin has done the config so we can
   strike it from the deferred list.

**Acceptance criteria:**

- All four prior tasks' acceptance criteria still hold at end of sprint.
- The full test suite is green at 100% line + branch coverage with no
  `# pragma: no cover` additions.
- `docker compose up -d --build` + `/health` works (or skip documented).
- `docs/work-log.md`, `CLAUDE.md`, memory, and (if applicable)
  `docs/parking-lot.md` reflect the close.

**Anti-criteria:**

- Don't ship the close-out before the test/lint/type/stack gates pass.
- Don't drop coverage to "fix" a hard-to-test line — refactor or accept the
  test cost.
- Don't write Sprint 6 scope into the work-log entry — that lives in
  `docs/sprint-6.md` when it gets created.

---

## Working principles (carried from Sprints 1–4)

- **TDD discipline.** Tests first, including failure-mode counterparts. No
  happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get
  explicit "go", then code.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria
  are met. The gate is the user's, not the agent's.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–4.
- **No new dependencies** without explicit approval. Version bumps of existing
  deps are allowed when justified (record the reason in `docs/work-log.md`).
- **Endpoint handler tests:** test handler logic by direct `await` of the
  handler function; use `TestClient`/`AsyncClient` only for routing,
  role-gating, and `response_model` shaping. Same call as Sprints 2-4.
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 5
  exercises #2 (three-record write) on the create + add-comment paths and
  #3 (PATCH-not-PUT) on the decommission status change.
- **Reuse Sprint 3/4 apparatus.** `NetBoxClient` post/patch/get;
  `NetBoxWriteService.patch_with_attribution` for decommission's status PATCH;
  the new `post_with_attribution` for create + add-comment;
  `QRLifecycleService.retire` for decommission's QR side;
  `to_device_data` / `DeviceResponse` for response shaping; the global
  NetBox exception handlers in `main.py`. **Don't rewrite the apparatus.**
- **`RuntimeError` over `assert`** for defensive runtime guards (Sprint 1 M3
  / Sprint 4 Q2 pattern).

## Reference documents

- ToR §4.3 (device flows — read in detail-plan phase for Tasks 2 + 4 field-set
  + permissions grounding)
- `Architecture_Overview.md` §3 (NetBox interaction — three-record write,
  optimistic concurrency)
- `docs/sprint-3.md` — device read + update; cross-cutting decisions A/B/C
  Sprint 5 inherits
- `docs/sprint-4.md` — QR lifecycle apparatus; the compensation pattern;
  `QRLifecycleService.retire` (decommission's reuse target)
- `docs/work-log.md` — Sprint 4 retrospective; the deferred items Sprint 5
  picks up
- `docs/parking-lot.md` — NetBox status-config dependency (relevant to
  decommission's production deploy)
- CLAUDE.md cross-cutting rules #2, #3
