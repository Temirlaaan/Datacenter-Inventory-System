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

---

## Admin sessions surface — fully RESOLVED across Sprints 7 + 8a

**Sprint 7:**

- **`GET /api/v1/admin/sessions`** — shipped (Task 3).
- **`POST /api/v1/admin/sessions/{id}/force-close`** — shipped (Task 3).
- **Auto-end stale-shifts background job** — shipped (Task 1).

**Sprint 8a:**

- **`POST /api/v1/admin/sessions/start`** — shipped (Task 0) with body
  `{workstation_id: str(1..255)}`. Role `dcinv-admin` only (chicken-and-egg:
  can't require an active shift to open one). Distinct
  `AdminSessionStartRequest` Pydantic model from mobile's `SessionStartRequest`
  — the API layer renames `workstation_id` at the schema boundary while the
  DB column stays `tablet_id`. Unblocks live use of every Sprint 7 admin
  endpoint.
- **`POST /admin/batches/` + `GET /admin/batches/{id}` now gate on active
  shift** (Sprint 8a Task 0). `QRGenerationService` audit row's `session_id`
  now sources from `user.shift_session_id` instead of hardcoded `None` —
  Sprint 6 decision F is RESOLVED. Pre-Sprint-8a batch rows retain `NULL`
  per the "no historical migration" stance.

No residual; the admin sessions surface is complete.

---

## Multi-replica auto-end-job ownership — RESOLVED in Sprint 8a Task 1

Sprint 7 Task 1 shipped the auto-end loop as an asyncio task inside the
FastAPI lifespan with a documented single-replica caveat. Sprint 8a Task 1
resolved it by wrapping each iteration's body in a Postgres advisory lock:

- `app/services/auto_end_job.py:_AUTO_END_JOB_ADVISORY_LOCK_ID` — stable
  bigint derived from `sha256(b"dcinv:auto_end_job")`. The seed string is
  greppable so future maintainers can find the lock's purpose. Operators
  can inspect the current owner via
  `SELECT * FROM pg_locks WHERE locktype='advisory' AND objid=<id>`.
- `_run_iteration` acquires the lock via `pg_try_advisory_lock`; lock-loser
  replicas skip the work and return 0 (logged at INFO as
  `auto_end_job_lock_skip`). The outer loop's "bump `last_iteration_at` if
  no exception" semantic naturally treats lock-skip as a successful tick,
  so lock-loser replicas don't flip to `"stale"` on `/health`.
- `/health`'s `auto_end_job` sub-object shape is unchanged.

The choice between advisory lock vs k8s CronJob (the two options flagged
in Sprint 7) went to advisory lock: simpler, no new deployment surface,
no external dependency. k8s CronJob remains an option if a future
deployment scenario calls for it.

Locked in by `tests/integration/test_auto_end_job.py::test_concurrent_run_iterations_only_one_runs_the_work`
(two concurrent `_run_iteration` calls; assert exactly one ends the
seeded rows and the other returns 0).

---

## `audit_log.session_id` semantic change — RESOLVED in Sprint 7 Task 2

The Sprint 6 decision D semantic flip (JWT `sid` → `shift_sessions.id`) now
has a documented consumer: `GET /api/v1/admin/audit`. The endpoint's OpenAPI
description for the `session_id` filter carries the semantic note that pre-
2026-05-30 rows hold JWT sids; admins reading the schema (or the API docs UI
rendered from it) understand the era boundary. Ad-hoc consumers that filter
by `session_id` on historical rows are now warned via the schema.

---

## NetBox circuit breaker — RESOLVED in Sprint 8a Task 2

Architecture §3.3 deferral, carried since Sprint 3. Sprint 8a Task 2 shipped
it via the `circuitbreaker>=2.0,<3` PyPI dep (first pyproject deviation since
Sprint 1; pre-approved at Sprint 8 plan stage):

- Module-level `CircuitBreaker(expected_exception=(NetBoxServerError,
  NetBoxTimeout), name="netbox")` lazy-initialised in
  `app/netbox/client.py`. **`NetBoxNotFound` (404) and
  `NetBoxValidationError` (4xx) do NOT count** — they're "NetBox said your
  request is wrong," not "NetBox is broken." A flood of 404s won't open the
  circuit; that's a rate-limiting concern (now solved in Task 3).
- `_send` split into the public open-check wrapper + `_send_impl` (original
  retry loop). When OPEN, raises new `NetBoxCircuitOpenError(NetBoxClientError)`
  with `recovery_timeout_seconds` for the 503 body.
- `main.py` exception handler returns **503 + `Retry-After: N` header +
  structured `{"error":{"code":"NETBOX_CIRCUIT_OPEN","retry_after_seconds":N}}`**
  body. Distinct from the existing 502 `NetBoxClientError` path: 502 = "I
  asked NetBox and got a bad response"; 503 = "I'm refusing to call NetBox
  because it's been failing."
- `/health` extended with **informational** `netbox_circuit:{enabled,state,
  failure_count,open_until}` sub-object. Does NOT flip overall `/health`
  status; the existing `_check_netbox` probe (uses a fresh
  `httpx.AsyncClient`, bypasses the circuit) remains the 503 trigger.
- Three new `Settings` knobs: `NETBOX_CIRCUIT_ENABLED` (true),
  `NETBOX_CIRCUIT_FAILURE_THRESHOLD` (5),
  `NETBOX_CIRCUIT_RECOVERY_TIMEOUT_SECONDS` (30).
- `reset_netbox_circuit()` test helper added to `tests/conftest.py:clean_env`
  so each test starts CLOSED.

Locked in by `tests/integration/test_circuit_breaker.py` (end-to-end NetBox
503 trips circuit → next call returns 503 NETBOX_CIRCUIT_OPEN with
`Retry-After: 30`).

---

## Rate limiting — RESOLVED in Sprint 8a Task 3

ToR §5.4.7 requirement. Sprint 8a Task 3 shipped per-user fixed-window
rate limiting at the FastAPI middleware layer:

- `app/middleware/rate_limit.py` with three classes (READ 60/min default,
  WRITE 20/min, ADMIN 30/min) + UNLIMITED bypass (`/health`, `/docs`,
  `/openapi.json`, `/redoc`).
- Classification by path + method: `/api/v1/admin/*` → ADMIN regardless of
  method; GET/HEAD/OPTIONS → READ; POST/PATCH/PUT/DELETE → WRITE.
- User identity extracted via `jwt.get_unverified_claims()` —
  rate-limit keying does NOT need full signature verification (that
  happens later in `require_role`). A forged `sub` lets an attacker mess
  with their own bucket; real auth still rejects them.
- 429 + `Retry-After: <seconds>` header + structured body
  `{"error":{"code":"RATE_LIMIT_EXCEEDED","retry_after_seconds":N}}` (shape
  mirrors Task 2's 503).
- Middleware registered BEFORE `request_id_middleware` in source order so
  request_id ends up OUTER (Starlette: reverse-registration = outer) and
  structlog contextvars are bound for the 429 log.
- Four new `Settings` knobs (`RATE_LIMIT_ENABLED` + three per-class budgets).

**Residual deferred to Sprint 9+ (cluster-wide rate-limit state):** the
current implementation uses an in-process `dict[(sub, class,
window_index), int]` — per-replica enforcement, so cluster-wide total rate
is N × per-replica budget. Acceptable today (single-replica deployment);
the first multi-replica deployment will need to replace `_buckets` with
Redis or Postgres-backed counters behind the same `_consume(...)`
interface. The middleware itself won't need to change.

The contract is locked in by a test that introspects `app.openapi()` and
checks the description text — see
`tests/unit/api/v1/test_admin_audit.py:test_get_audit_includes_session_id_semantic_note_in_openapi`.

No further action required for this concern.

---

## PDF batch-label download — no audit row (Sprint 8b Task 2 decision 6, may revisit)

`GET /api/v1/admin/batches/{id}/labels.pdf` (Sprint 8b Task 2) deliberately
writes **no** `audit_log` row. Decision rationale at the time: the PDF
contents are the same QR codes already exposed by the audited
`GET /api/v1/admin/batches/{id}` JSON detail endpoint, and ToR §5.4.6
covers sensitive reads — batch contents aren't in that class. The local
code review of commit `3eb5a58` flagged it as a future-revisit candidate.

**Why this might matter post-rollout:**

- For physical-label inventory traceability ("who printed labels for
  batch X at time Y"), an audit row would answer the question; without
  one, only the upstream JSON detail call (if any) is traceable.
- Regulatory or compliance reviews may require a per-download trail
  even for data the admin already saw.

**How to fix if revisited:** add an audit-of-audits row inside
`get_batch_labels_pdf` in `app/api/v1/admin/batches.py` with
`operation="batch.labels_pdf"`, `entity_type="batch"`, `entity_id=str(batch_id)`,
`after_json={"label_count": len(codes)}`, `result=AuditResult.SUCCESS`. One
focused commit; no schema change. Sprint 7 Task 2's `/admin/audit`
endpoint will surface it via `entity_type=batch` filter.

**Owner:** Sprint 9+ (pending operator feedback / compliance review).
**Not a blocker for:** any current functionality; the PDF endpoint
ships as-is.

---

## CSRF token for `/web/*` form POSTs (Sprint 8b Task 4 decision 12, deferred)

`POST /web/sessions/{id}/force-close` (Sprint 8b Task 4) is the only
state-changing HTML form endpoint shipped this sprint. **It carries no
CSRF token.** Current defenses:

- Session cookie is set with `samesite=lax` (Sprint 8b Task 0), which
  blocks the cookie from being sent on third-party form submissions
  to the backend.
- The cookie-auth dep (`require_web_admin`) checks role + active shift;
  a foreign-origin form post would arrive without the cookie and 302
  to login.
- Deployment is **VPN-only** (CLAUDE.md "What the System Is"); the
  attack surface for cross-origin requests is bounded.

**Why this might matter post-rollout:** if a future security review
requires defense-in-depth beyond `SameSite=Lax`, or if the deploy
posture loosens (e.g. browser-extension access, internal proxy that
strips cookies), a per-session CSRF token becomes load-bearing.

**How to fix if required:**

- Mint a CSRF token at OIDC-callback time, store it in the encrypted
  cookie payload alongside `sub`/`email`/`roles`/`exp`.
- Render it as a hidden `<input type="hidden" name="_csrf_token">` in
  every `<form method="post">` on the web admin surface (currently:
  one form on `/web/sessions/`).
- Validate it in the web POST handler before delegating to the JSON
  handler. Mismatch → 403 + redirect to login.
- Add `_csrf_token: str` to `WebAdminUser` (this is one of the few
  fields that legitimately belongs in the cookie, since it's
  cookie-scoped per session).

**Owner:** Sprint 9+ (pending security review).
**Not a blocker for:** Sprint 8b's force-close UX shipping.
