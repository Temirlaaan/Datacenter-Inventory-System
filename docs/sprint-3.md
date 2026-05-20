# Sprint 3 — Device Read & Update

> **Status:** Closed 2026-05-20. All 7 tasks complete; see `docs/work-log.md`
> for the retrospective.
> **Duration target:** 7–10 working days
> **Goal:** Device read + update — the first NetBox *writes*. Build the write
> apparatus (three-record write, optimistic concurrency) and ship
> `GET`/`PATCH /api/v1/devices/{id}` on top of it, with a server-driven edit form.

## Why this sprint exists

Sprints 1–2 only ever *read* from NetBox; all writes were to the app DB. Sprint 3
is the first sprint that writes to NetBox, so it builds the machinery the
CLAUDE.md cross-cutting rules #2 (three-record write) and #3 (optimistic
concurrency, PATCH-not-PUT) describe — and proves it by shipping device read and
update. QR bind/retire and the rest of the device lifecycle ride on this same
apparatus and follow in Sprint 4.

## Scope boundaries

**In scope — 7 tasks (detailed breakdown TBD, see Task list below):**

1. NetBox write client
2. Optimistic-concurrency + three-record write service
3. Meta lookup endpoints + in-process caching layer
4. Server-driven device-form config
5. Device read endpoint
6. Device update endpoint
7. Acceptance + close-out

**Out of scope (deferred to Sprint 4+):**

- QR `bind` / `retire` operations — Sprint 4 (`free→bound` write path + the
  `free→bound` DB transaction; Sprint 2's plan loosely lumped these with device
  update, but they are a distinct write path and get their own sprint slot).
- Device creation (`POST /api/v1/devices/`) — Sprint 4.
- Device decommission (status → `Decommissioning` + QR retire) — Sprint 4;
  also gated on a NetBox config dependency (see `docs/parking-lot.md`).
- Add-comment (NetBox journal append endpoint) — Sprint 4.
- `shift_sessions` table + `POST /api/v1/sessions/{start,end}` — later sprint.
- NetBox circuit breaker (Architecture §3.3) — deferred hardening (decision D).
- Extending `GET /api/v1/qr/{id}` to fetch the bound device from NetBox — Sprint 4.
- PDF label generation, web admin pages — later sprints.

## Cross-cutting decisions

These apply across multiple tasks; capturing once so each task doesn't
re-litigate. Decided with the user during Sprint 2 close-out.

**A. Conflict detection — re-read and compare** (Architecture §3.2), *not*
`If-Unmodified-Since` passthrough to NetBox. NetBox's REST API does not reliably
honour HTTP conditional requests on PATCH; the backend re-reads `last_updated`
itself and compares. The mobile client still sends `If-Unmodified-Since` — the
backend treats it as the *expected* version. A small TOCTOU window between the
re-read and the PATCH is accepted (mobile-app write concurrency is low).

- **Addition:** the `audit_log` row records **both** timestamps — the
  `If-Unmodified-Since` value the client sent **and** the `last_updated` the
  backend saw on its re-read. This makes later "unexplained 409" reports
  debuggable. Both go in the audit row's existing `before_json`/`after_json`
  JSONB columns — no migration needed.

**B. Three-record write — NetBox-write-first, best-effort attribution.** Order:
(1) NetBox device PATCH, (2) NetBox journal POST, (3) app-DB `audit_log` row,
sharing one `request_id`. If the journal POST or the audit row write fails after
the device PATCH succeeded, the backend logs loudly and does **not** roll back
the NetBox change — no distributed transaction exists. Acceptable for the MVP.
Partial-failure alerting is parked: see `docs/parking-lot.md` ("Phase 2:
alerting on three-record partial failures").

**C. `session_id` source — the JWT `sid` claim.** Audit rows and journal entries
take `session_id` from `AuthUser.session_id` (extracted from the token in
Sprint 1), not from a `shift_sessions` table (that table and the `/sessions/*`
endpoints are deferred).

**D. NetBox circuit breaker deferred.** Architecture §3.3 specifies one; it is
deferred to a later hardening pass. Sprint 1's retry-with-backoff stays as the
only resilience layer for now.

**E. Server-driven form config location.** The device-edit form YAML lives at
`backend/app/services/forms/device_edit.yaml`. Adding/removing an editable field
means editing that YAML, not shipping a mobile build (CLAUDE.md #5).

**F. MVP editable field set** (ToR §4.3.4 — **no additions**): Status, Site,
Rack, Position, Name, Serial Number, Asset Tag, Comments.

## Task list

> **TBD — full per-task breakdown pending.** Each task will be detailed with
> Goal / Steps / Acceptance criteria / Anti-criteria / Suggested prompt,
> mirroring `docs/sprint-2.md`. Task names are fixed (see Scope boundaries);
> the detail lands before Task 1 starts.

---

### Task 1 — NetBox write client

**Goal:** Add write support (`patch`, `post`) to the existing read-only
`NetBoxClient` (`app/netbox/client.py`), so Task 2's three-record-write service
has a transport layer. The client stays *thin* (Sprint 1 decision): it returns
the raw `httpx.Response` for the caller to parse and holds no conflict logic.

**Steps:**

1. Generalize `_send()` to carry a JSON body and a per-request timeout:
   - Add `json: dict | None = None` and `timeout: float = _READ_TIMEOUT_SECONDS`
     parameters; pass both through to `self._client.request(...)`.
   - Add `_WRITE_TIMEOUT_SECONDS = 10.0` (Architecture §3.3: 5s reads, 10s
     writes). Reads keep the 5s default.
   - Make **501 non-retryable** (Architecture §3.3: "Don't retry 501"). The
     current loop retries every 5xx; 501 Not Implemented is permanent, so raise
     `NetBoxServerError` immediately without consuming retries.
2. Add two public write methods:
   - `patch(path, *, json) -> httpx.Response` — PATCH only, **never PUT**
     (CLAUDE.md cross-cutting #3). Used for `PATCH /api/dcim/devices/{id}/`.
   - `post(path, *, json) -> httpx.Response` — used for
     `POST /api/extras/journal-entries/`.
   - Both call `_send` with `timeout=_WRITE_TIMEOUT_SECONDS`.
   - No `headers` parameter and no 412 handling: per cross-cutting decision A
     the backend does **not** send `If-Unmodified-Since` to NetBox (conflict
     detection is re-read-and-compare in Task 2's service). `X-Request-ID` is
     already auto-injected by `_send`.
3. Writes share the existing unified retry path. PATCH is idempotent
   (re-applying the same field changes is safe); a retried journal POST risks at
   worst a duplicate journal *note* — cosmetic, and decision B already treats
   journal writes as best-effort. No `DELETE` method — no Sprint 3 call site
   needs one (anti-bloat; add when Sprint 4's decommission path lands, if ever).
4. Update the stale module docstring (it says writes "land in Sprint 2" and
   references `If-Unmodified-Since` passthrough — superseded by decision A).
5. Tests in `tests/unit/netbox/test_client.py` (respx-mocked, TDD, failure-mode
   counterparts):
   - `patch`/`post`: success returns the response; JSON body is sent; HTTP
     method is exactly `PATCH`/`POST` (guards against PUT).
   - Retry: 5xx retried 3× then `NetBoxServerError`; transient 5xx then success;
     `ConnectError` retried; 404 → `NetBoxNotFound` (no retry); 4xx →
     `NetBoxClientError` (no retry).
   - **501 → `NetBoxServerError`, `call_count == 1`** (new branch).
   - Writes use the 10s timeout (assert `request.extensions["timeout"]`).

**Acceptance criteria:**

- `pytest tests/unit/netbox/` passes; 100% line + branch coverage on
  `app/netbox/client.py`.
- ruff + black + mypy clean.
- `patch`/`post` return the raw `httpx.Response`; no parsing, no conflict logic
  in the client (thinness preserved).
- 501 is not retried; other 5xx still retried 3×.
- No `If-Unmodified-Since` header is ever sent to NetBox.

**Anti-criteria:**

- Don't add a `put()` method, ever (CLAUDE.md cross-cutting #3).
- Don't add `delete()` speculatively — no Sprint 3 call site needs it.
- Don't add conflict detection, `last_updated` comparison, or three-record-write
  logic here — that's Task 2's service layer.
- Don't add a `headers` parameter for `If-Unmodified-Since` passthrough
  (decision A).

**Suggested prompt:**

```
Implement the NetBox write client per Sprint 3 Task 1. Add patch()
and post() to app/netbox/client.py on top of a generalized _send()
that carries a JSON body and a 10s write timeout (Architecture §3.3).
Make 501 non-retryable. PATCH only, never PUT. Keep the client thin —
raw httpx.Response back, no conflict logic. TDD: respx-mocked tests
first, failure-mode counterparts, 100% coverage on app/netbox/.
```

### Task 2 — Optimistic-concurrency + three-record write service

**Goal:** A reusable `NetBoxWriteService` that performs a NetBox object PATCH with
optimistic-concurrency conflict detection (decision A) and the three-record write
— NetBox PATCH + journal POST + `audit_log` row, shared `request_id` (CLAUDE.md
cross-cutting #2, decision B). Tasks 5/6 ship device read/update on top; this task
ships the apparatus tested but unwired.

**Steps:**

1. Create `app/services/netbox_write.py`:
   - `WriteConflictError(Exception)` — carries `current_object: dict` and
     `current_version: str`; raised on version mismatch (no NetBox write
     happened). Task 6 maps it to the Architecture §3.2 `409 / DEVICE_CONFLICT`
     body.
   - `NetBoxWriteService`, DI: `NetBoxClient`, `AsyncSession`,
     `AuditLogRepository`.
   - `patch_with_attribution(*, netbox_path, netbox_object_type,
     netbox_object_id, entity_type, operation, expected_version, changes, user)
     -> dict` — returns the updated NetBox object.
2. Three execution zones inside `patch_with_attribution`:
   - **Conflict-checked PATCH** (try-wrapped): re-read `GET netbox_path` →
     `original`; compare `original["last_updated"]` to `expected_version`
     (opaque string token, never parsed/reformatted); on mismatch write a
     `CONFLICT` audit row and raise `WriteConflictError`; else `PATCH
     netbox_path` with `changes`. Any exception in this zone → `FAILURE` audit
     row + re-raise. Small TOCTOU window between re-read and PATCH accepted
     (decision A).
   - **Best-effort journal POST** (decision B): `POST
     /api/extras/journal-entries/` with the §3.1 comment (user, request_id,
     session, field-level diff). Failure logged (`netbox_journal_write_failed`),
     never fatal — the PATCH already succeeded. Journal entry only on success.
   - **Best-effort `SUCCESS` audit row** (decision B): failure logged
     (`audit_log_write_failed`), still returns the updated object.
3. Audit rows (decision A): `before_json`/`after_json` carry **both** version
   timestamps — `expected_version` and the re-read `observed_version` — plus the
   object state. `session_id` from `AuthUser.session_id` (decision C).
   `request_id` shared automatically (the NetBox client stamps `X-Request-ID`
   from the same contextvar).
4. The service owns the audit-row transaction (`async with session.begin()`) — a
   deliberate divergence from Sprint 2's caller-owns pattern, because the
   three-record write runs after a durable NetBox PATCH with no app-DB unit of
   work to compose with. Every audit write is best-effort (swallow + log).
5. Tests:
   - `tests/unit/services/test_netbox_write.py` — respx-mocked NetBox + fake
     `AuditLogRepository`/session: success, version-mismatch conflict, re-read
     failure, PATCH failure, journal best-effort failure, audit best-effort
     failure, `request_id` sharing, `session_id` sourcing, no-email fallback,
     diff/comment formatting, `WriteConflictError`.
   - `tests/integration/test_netbox_write.py` — respx + real Postgres:
     `SUCCESS`/`CONFLICT`/`FAILURE` audit rows land with the correct enum value,
     columns, and both-version JSONB.

**Acceptance criteria:**

- `pytest tests/unit/services/test_netbox_write.py
  tests/integration/test_netbox_write.py` passes; 100% line + branch coverage on
  `app/services/netbox_write.py`.
- ruff + black + mypy clean.
- Conflict path performs **no** NetBox write and raises `WriteConflictError`
  carrying current state.
- All three record types share one `request_id`.
- Journal/audit failures after a successful PATCH are logged, never fatal
  (decision B).
- Audit rows record both the client-expected and backend-observed versions
  (decision A).

**Anti-criteria:**

- No PUT — PATCH only (CLAUDE.md cross-cutting #3).
- Don't send `If-Unmodified-Since` to NetBox — conflict detection is
  re-read-and-compare (decision A).
- Don't roll back the NetBox PATCH on journal/audit failure — no distributed
  transaction exists (decision B).
- Don't add an idempotency layer — PATCH + optimistic concurrency already make a
  stale double-submit a clean 409.
- Don't build device-specific request/response schemas here — that's Task 6.
- Don't add a NetBox circuit breaker (deferred, decision D).

**Suggested prompt:**

```
Implement Sprint 3 Task 2: NetBoxWriteService in app/services/netbox_write.py.
patch_with_attribution does optimistic-concurrency re-read-and-compare
(decision A) then the three-record write — NetBox PATCH + journal POST +
audit_log row, shared request_id (decision B: NetBox-write-first, journal
and audit best-effort). WriteConflictError on version mismatch. TDD:
respx-mocked unit tests + real-Postgres integration tests, failure-mode
counterparts, 100% coverage.
```

### Task 3 — Meta lookup endpoints + in-process caching layer

**Goal:** Three `GET /api/v1/meta/*` endpoints proxying NetBox static lookups
behind a 5-minute in-process cache, so Task 4's server-driven form can populate
its `choice`/`reference` fields (Status, Site, Rack — the MVP editable fields,
decision F). Implements the Sprint 1 caching design note (in-process TTL only,
no Redis).

**Steps:**

1. `app/services/cache.py` — `TTLCache`: generic in-process time-based cache,
   `get_or_fetch(key, fetch)`. Injectable `clock` for deterministic TTL tests. A
   failed `fetch` is not cached. No per-key lock — a cold-cache double-fetch of
   static data is harmless. Generic so Task 5 can reuse it at ≤60s for device
   data (CLAUDE.md caching policy).
2. `app/netbox/client.py` — add `options(path) -> httpx.Response`. `OPTIONS` is
   idempotent, so it retries like `get` (read timeout, no body).
3. `app/services/meta.py` — `MetaLookupService` (DI: `NetBoxClient`, `TTLCache`)
   with `get_sites`/`get_racks`/`get_statuses`, each cached 5 min:
   - `sites` ← `GET /api/dcim/sites/?limit=0` → `MetaSite(id, name)`.
   - `racks` ← `GET /api/dcim/racks/?limit=0` → `MetaRack(id, name, site_id,
     u_height)` (carries `site_id` so the mobile app scopes racks to the chosen
     site client-side, and `u_height` to drive the Position field).
   - `statuses` ← `OPTIONS /api/dcim/devices/`, parse
     `actions.POST.status.choices` → `MetaStatus(value, label)`. Discovered
     dynamically from NetBox, never hardcoded (`parking-lot.md`). Depends on
     NetBox's OPTIONS exposing the choice set — standard for NetBox 3.x/4.x,
     unverified here against a live instance.
   - `MetaSite`/`MetaRack`/`MetaStatus` are Pydantic DTOs in `meta.py`.
4. `app/api/v1/meta.py` — router: `GET /api/v1/meta/{sites,racks,statuses}`,
   role `dcinv-mobile-user`, `response_model=list[...]`. Register in `main.py`
   at `/api/v1/meta`. Cache + client come from `lru_cache` singletons
   (`get_meta_cache`, existing `get_netbox_client`).
5. Endpoints return the **full list** — no query/filter/search params; the
   mobile app filters client-side (single-DC scale: 1 site, ~20 racks).
6. Tests:
   - `tests/unit/services/test_cache.py` — `TTLCache`: hit served without
     refetch, miss fetches, expiry refetches, distinct keys isolated, a failing
     fetch is not cached.
   - `tests/unit/services/test_meta.py` — `MetaLookupService` with respx: fetch
     + transform per lookup, second call served from cache (NetBox hit once),
     statuses OPTIONS parsing.
   - `tests/unit/api/v1/test_meta.py` — handler logic by direct `await`;
     `AsyncClient` for routing, role-gating (403 without `dcinv-mobile-user`),
     `response_model`.
   - `tests/unit/netbox/test_client.py` — `options()` success + method check.

**Acceptance criteria:**

- `pytest` passes for the new test files; 100% line + branch coverage on
  `app/services/cache.py`, `app/services/meta.py`, `app/api/v1/meta.py`, and the
  new `client.options()` line.
- ruff + black + mypy clean.
- A second call within the TTL hits NetBox zero times (cache proven).
- Meta endpoints 403 without the `dcinv-mobile-user` role.
- `/meta/statuses` derives the status set from NetBox, not a hardcoded list.

**Anti-criteria:**

- No Redis — in-process TTL only (Sprint 1 caching decision).
- Don't cache device data here — that's Task 5, with a ≤60s ceiling.
- Don't build `device-types`/`manufacturers`/`device-roles` endpoints — no MVP
  form field needs them; add when one does.
- No query/filter/search params on the meta endpoints — full lists only.
- Don't hardcode the device status set (`parking-lot.md`).

**Suggested prompt:**

```
Implement Sprint 3 Task 3: in-process TTLCache (app/services/cache.py),
MetaLookupService (app/services/meta.py), and GET /api/v1/meta/{sites,
racks,statuses} (app/api/v1/meta.py), role dcinv-mobile-user, cached 5
min. Statuses discovered via OPTIONS /api/dcim/devices/ (add
client.options()). No Redis, full lists, no query params. TDD:
respx-mocked, failure-mode counterparts, 100% coverage.
```

### Task 4 — Server-driven device-form config

**Goal:** `GET /api/v1/meta/device-form` serves the device-edit form config
(Architecture §5) from a YAML file packaged with the backend, so adding/removing
an editable field is a file edit + redeploy — never a mobile build (CLAUDE.md
#5). Ships the 8 MVP fields (decision F).

**Steps:**

1. Add the `pyyaml` dependency (Architecture §5 + decision E mandate YAML; the
   stack had no YAML parser). Pin in `pyproject.toml`, add a mypy `yaml.*`
   override (PyYAML ships no stubs — same pattern as `jose`), record in the
   work-log deviations.
2. `app/services/forms/device_edit.yaml` — the 8 decision-F fields (Status,
   Site, Rack, Position, Name, Serial Number, Asset Tag, Comments), structured
   per Architecture §5.1, with a `version` token (Architecture §5.3).
3. `app/services/device_form.py`:
   - `FieldType` — `StrEnum` of the six generic types (`choice`, `reference`,
     `integer`, `text`, `multiline_text`, `boolean`).
   - `FormField` — `key`/`label`/`type` typed + `required` (default false);
     `extra="allow"` so field-specific keys (`choices_endpoint`,
     `confirmation`, `depends_on`, …) pass through untouched — the backend
     never hardcodes field-specific knowledge (CLAUDE.md #5).
   - `DeviceFormConfig` — `version: str`, `fields: list[FormField]`.
   - `load_device_form_config(path)` — `yaml.safe_load` + `model_validate`,
     raises on malformed input. `get_device_form_config()` — `lru_cache`d load
     of the packaged file.
4. `app/api/v1/meta.py` — add `GET /api/v1/meta/device-form`, role
   `dcinv-mobile-user`, `response_model=DeviceFormConfig`.
5. Tests:
   - `tests/unit/services/test_device_form.py` — `load_device_form_config`
     parses a valid file; raises on malformed YAML and on an invalid field
     type; the real `device_edit.yaml` loads with the 8 decision-F keys; extra
     keys pass through; `get_device_form_config` is cached.
   - `tests/unit/api/v1/test_meta.py` — `device-form` handler by direct
     `await`; `AsyncClient` for routing, role-gating (403), and that a
     field-specific extra key survives serialization.

**Acceptance criteria:**

- `pytest` passes the new tests; 100% line + branch coverage on
  `app/services/device_form.py` and the new `meta.py` lines.
- ruff + black + mypy clean.
- `GET /api/v1/meta/device-form` returns the config with all 8 fields and their
  field-specific keys intact; 403 without `dcinv-mobile-user`.
- A malformed `device_edit.yaml` fails loudly (not a silent empty form).

**Anti-criteria:**

- Don't hardcode field names or field-specific structure in backend code — only
  the six generic types (CLAUDE.md #5).
- Don't add deep per-type validation (e.g. "a `choice` must have
  `choices_endpoint`) — skeleton validation only this task.
- Don't add fields beyond the decision-F set (ToR §4.3.4 — no additions).
- No PUT/POST on the form config — it is read-only, file-sourced.

**Suggested prompt:**

```
Implement Sprint 3 Task 4: server-driven device-form config. Add pyyaml,
author app/services/forms/device_edit.yaml with the 8 decision-F fields
(Architecture §5.1 structure), build app/services/device_form.py
(FieldType / FormField extra=allow / DeviceFormConfig + a cached loader),
and GET /api/v1/meta/device-form (role dcinv-mobile-user). TDD,
skeleton-only validation, 100% coverage.
```

### Task 5 — Device read endpoint

**Goal:** `GET /api/v1/devices/{device_id}` fetches a device from NetBox and
returns it with a `version` token (its `last_updated`) for optimistic
concurrency (Architecture §3.2). Scoped to the editable-field values Task 6's
form pre-fill needs; the full ToR §4.3.3 device-screen field set lands in
Sprint 4 with the combined QR+device response.

**Steps:**

1. `app/services/device.py` — `DeviceService(netbox_client)` with
   `get_device(device_id) -> DeviceResponse`. Parses the raw NetBox device JSON
   (consistent with Task 3's meta service). Models:
   - `StatusRef {value, label}`, `ObjectRef {id, name}`.
   - `DeviceData` — `id`, `name`, `status: StatusRef`, `site: ObjectRef`,
     `rack: ObjectRef | None`, `position: int | None`, `serial: str`,
     `asset_tag: str | None`, `comments: str`. `site` is non-optional (NetBox
     devices always have a site). `asset_tag` from `custom_fields.asset_tag`
     (same verify-against-NetBox caveat as Task 4).
   - `DeviceResponse {data: DeviceData, version: str}`.
2. `main.py` — global exception handlers: `NetBoxNotFound → 404`, other
   `NetBoxClientError → 502`. Covers the device read and retrofits the Task 3
   meta endpoints (a NetBox outage now returns 502, not 500).
3. `app/api/v1/devices.py` — `GET /api/v1/devices/{device_id}`, role
   `dcinv-mobile-user`, `response_model=DeviceResponse`. `device_id` is `int`
   (non-integer → 422). Registered in `main.py` at `/api/v1/devices`.
4. Tests:
   - `tests/unit/services/test_device.py` — respx: parse a full device; a
     device with no rack (the `rack=None` branch); `NetBoxNotFound` and
     `NetBoxServerError` propagate.
   - `tests/unit/api/v1/test_devices.py` — handler by direct `await`;
     `AsyncClient` for routing, role-gating (403), 404 (unknown id), 502 (NetBox
     error), 422 (non-integer id).

**Acceptance criteria:**

- `pytest` passes the new tests; 100% line + branch coverage on
  `app/services/device.py`, `app/api/v1/devices.py`, and the new `main.py`
  lines.
- ruff + black + mypy clean.
- Unknown device id → 404; NetBox 5xx/timeout → 502; missing `dcinv-mobile-user`
  → 403.
- The response carries a `version` equal to the NetBox `last_updated`.

**Anti-criteria:**

- Don't return the full §4.3.3 device-screen field set (device type, IPs,
  custom fields, QR ID) — Sprint 4's combined QR+device response owns that. Keep
  `DeviceData` additive so Sprint 4 extends it.
- Don't cache the device read (CLAUDE.md caps device caching at 60s; not added
  this task).
- No write paths — read only.
- Don't query the app DB for a bound QR (`qr_id`) — no QR is bound to any device
  until Sprint 4.

**Suggested prompt:**

```
Implement Sprint 3 Task 5: device read endpoint. DeviceService in
app/services/device.py parses a NetBox device into DeviceResponse
{data, version}; GET /api/v1/devices/{device_id} (role
dcinv-mobile-user) serves it. Add global NetBox exception handlers
in main.py (NetBoxNotFound -> 404, other NetBoxClientError -> 502).
TDD, respx-mocked, 100% coverage. Editable-field scope only — no
full device-screen set, no caching.
```

### Task 6 — Device update endpoint

**Goal:** `PATCH /api/v1/devices/{device_id}` accepts a `DeviceUpdateRequest`,
runs it through Task 2's `NetBoxWriteService.patch_with_attribution` (the
three-record-write apparatus), and returns the updated device (Architecture
§3.2). Optimistic concurrency via the client's `If-Unmodified-Since` header;
conflict → 409 with current state.

**Steps:**

1. **Code-review tidies first** (small, isolated):
   - M1: in `NetBoxWriteService.patch_with_attribution` move
     `observed_version = original["last_updated"]` inside the first `try` so a
     missing `last_updated` produces a FAILURE audit row (decision B's "every
     outcome produces an audit row"). Add the failure-mode test.
   - L1: wrap `_reset_netbox_client`'s `aclose()` in
     `contextlib.suppress(Exception)` (mirrors the root `clean_env`), so a
     client leaked from a now-closed loop can still be cleared.
2. `app/services/device.py` — add:
   - `DeviceUpdateRequest` (Pydantic, `extra="forbid"`): all 8 decision-F
     fields optional with the YAML bounds (name ≤64, serial ≤50, asset_tag ≤50,
     comments ≤1000). `site_id`/`rack_id` mirror the read response.
   - `to_netbox_changes(request) -> dict[str, Any]` — pure transform via
     `model_dump(exclude_unset=True)`, mapping `site_id → site`,
     `rack_id → rack`, `asset_tag → custom_fields.asset_tag`. Only explicitly
     set fields appear (PATCH semantics — the `exclude_unset` pattern
     CLAUDE.md #3 calls out).
   - Rename `_to_device_data` → `to_device_data` (now used by both the read
     endpoint and the update endpoint's 409 handler).
3. `app/api/v1/devices.py` — add:
   - `get_write_service(session) -> NetBoxWriteService` provider (so endpoint
     tests can override).
   - `PATCH /{device_id}` (role `dcinv-mobile-user`): builds `changes`, calls
     `patch_with_attribution`, returns `DeviceResponse` on success. Catches
     `WriteConflictError` locally → `JSONResponse(409, {"error": {"code":
     "DEVICE_CONFLICT", "message": ..., "current_state": <DeviceData>,
     "current_version": ...}})`. 404 / 502 still flow through `main.py`'s
     global `NetBoxClientError` handlers.
4. Tests:
   - `tests/unit/services/test_device.py` — `DeviceUpdateRequest` validation
     (length boundaries, `extra="forbid"`), `to_netbox_changes` per field +
     empty + multi-field + explicit-null-rack-unrack.
   - `tests/unit/services/test_netbox_write.py` — M1 failure-mode test.
   - `tests/unit/api/v1/test_devices.py` — handler direct-`await` (success +
     conflict); `AsyncClient` for 200, 409 (body shape), 404, 422 (missing
     header / bad payload), 403; `get_write_service` builds the service.
   - `tests/integration/test_devices.py` — respx + real Postgres: PATCH →
     `success` audit row; PATCH with stale version → 409 + `conflict` audit
     row.

**Acceptance criteria:**

- `pytest` passes the new tests; 100% line + branch coverage on
  `app/services/device.py`, `app/api/v1/devices.py`, and the M1-changed lines
  in `app/services/netbox_write.py`.
- ruff + black + mypy clean.
- PATCH → 200 on success with `DeviceResponse`; 409 on stale version with the
  §3.2 body shape; 404 on unknown device id; 422 on missing
  `If-Unmodified-Since` or over-length field; 403 without `dcinv-mobile-user`.
- The audit row for a successful update has `operation='device.update'`,
  `entity_type='device'`, `result='success'`, both versions in JSONB.

**Anti-criteria:**

- No PUT — PATCH only (CLAUDE.md #3).
- Don't send `If-Unmodified-Since` to NetBox — the backend re-reads and
  compares (decision A; handled in Task 2's service).
- Don't validate empty requests (`{}` PATCH) — let NetBox decide.
- Don't write a `DeviceUpdateService` class — orchestrate inline in the
  endpoint, like `batches.py::create_batch` does.

**Suggested prompt:**

```
Implement Sprint 3 Task 6: device update endpoint. Tidy up M1+L1 from
the code review first. Then add DeviceUpdateRequest + to_netbox_changes
to app/services/device.py and PATCH /api/v1/devices/{device_id} to
app/api/v1/devices.py — drives Task 2's NetBoxWriteService, returns
DeviceResponse on success and a 409 with current DeviceData on
WriteConflictError. TDD, respx + real Postgres for integration,
100% coverage.
```

### Task 7 — Acceptance and close-out

_TBD_

---

## Working principles (carried from Sprints 1–2)

- **TDD discipline.** Tests first, including failure-mode counterparts. No
  happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit
  "go", then code.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are
  met. The gate is the user's, not the agent's.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–2.
- **No new dependencies** without explicit approval. Version bumps of existing
  deps are allowed when justified (record the reason in `docs/work-log.md`).
- **Endpoint handler tests:** test handler logic by direct `await` of the
  handler function; use `TestClient`/`AsyncClient` only for routing, role-gating,
  and `response_model` shaping (CLAUDE.md "Test discipline", Sprint 2 finding).
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 3 is the
  first sprint to exercise #2 (three-record write) and #3 (optimistic
  concurrency, PATCH-not-PUT) against real NetBox writes.

## Reference documents

- ToR §4.3 (Device Operations — scan flow, device screen, editable fields,
  update flow, audit trail, status recommendation), §8.2 (mobile API endpoints)
- `Architecture_Overview.md` §3 (NetBox interaction — three-record write,
  optimistic concurrency, client resilience), §5 (server-driven form config)
- `docs/work-log.md` — Sprint 2 retrospective and decisions actually taken
- `docs/parking-lot.md` — NetBox status-config dependency; Phase 2 alerting
- CLAUDE.md cross-cutting rules #2, #3, #5, #6
