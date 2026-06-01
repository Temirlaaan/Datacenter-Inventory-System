# Sprint 7 — Admin Surface + Polish

> **Status:** Planned. Awaiting Task 0 go/no-go. (Per-task detail will be added in the post-skeleton-review + `/clear` detail pass.)
> **Duration target:** 5–6 working days (user spec)
> **Goal:** Close out Sprint 5/6 carry-over debt and ship the JSON foundations for the ToR-mandated admin surface (`GET /api/v1/admin/audit`, `GET /api/v1/admin/sessions` + force-close, auto-end stale-sessions job), and finish the small Sprint 5 polish items that have been carried across sprints (device-create form endpoint, decommission `reason` plumbed into NetBox journal, specialised 422 translation across all write endpoints). Sprint 8a will harden for production; Sprint 8b will layer the HTML web pages on top.

## Why this sprint exists

Sprint 6 made shifts first-class but the *consumption* surface is incomplete:

- The auto-end behavior mandated by ToR §4.1.3 is half-shipped — mobile owns the 10-minute idle timer (Sprint 6 decision E), but the 12-hour-orphan fallback for crashed tablets has been a Sprint 7 deferral since Sprint 6 close. Without it, a stolen/crashed phone leaves a shift open indefinitely.
- ToR §8.3 enumerates `GET /api/v1/admin/audit` and `GET /api/v1/admin/sessions` as required admin endpoints. Without them, the Sprint 6 audit semantic change (decision D: `audit_log.session_id` → `shift_sessions.id`) has nothing to consume it — forensic queries still require raw SQL.
- ToR §7.2.4 specifies enum names `manual / auto_timeout / forced`; Sprint 6 shipped descriptive variants `manual / inactivity_timeout / admin_force_close`. The divergence is a known contract gap. Sprint 7 Task 0 aligns to ToR canon **before** the auto-end job and admin endpoints write any new rows under the new names, so we never have a third era of enum values.
- Sprint 5 left three polish items deferred across two sprints: `GET /api/v1/meta/device-create-form` (the YAML exists but no endpoint serves it), decommission `reason` field plumbed only to logs (not the NetBox journal), and specialised `NetBoxValidationError → 422` translation only on device-create (the other write endpoints still bounce 4xx through the global 502 handler). These are small and unblock no other work — but they've crossed two sprint boundaries, so they get closed here before they calcify into "permanent" gaps.

Sprint 7 ships:

- A backend auto-end safety net (asyncio loop in lifespan; explicit multi-replica single-owner constraint until Sprint 8a)
- `GET /api/v1/admin/audit` with filters + offset pagination
- `GET /api/v1/admin/sessions` + `POST /api/v1/admin/sessions/{id}/force-close`
- The three Sprint 5 polish items
- An apparatus prep step (Task 0) that aligns the `shift_end_reason` enum to ToR canon

This unlocks Sprint 8a (production hardening — circuit breaker, alerting, real Keycloak smoke, performance testing) and Sprint 8b (HTML admin web pages, PDF labels, CSV export) without further data-plane work.

## Scope boundaries

**In scope — 7 tasks:**

0. **Apparatus prep — `shift_end_reason` enum rename to ToR canon.** Postgres `ALTER TYPE ... RENAME VALUE` migration (`inactivity_timeout` → `auto_timeout`, `admin_force_close` → `forced`). Sweep ~6 code sites + tests. Done before Task 1 so the auto-end job writes ToR-canonical values from its first run.
1. **Auto-end stale-sessions background job.** ToR §4.1.3 backend fallback. Asyncio loop in app lifespan with cancellation + per-iteration try/except + startup grace + config knobs + `/health` sub-object reporting.
2. **`GET /api/v1/admin/audit` — audit log query endpoint.** Filters: `user_keycloak_id`, `from`, `to`, `entity_type`, `entity_id`, `operation`, `session_id`, `result`. Offset pagination (`page` 1-indexed + `page_size`, max 100), `has_more` via `LIMIT N+1`. Endpoint produces its own audit row per ToR §5.4.6.
3. **`GET /api/v1/admin/sessions` + `POST /api/v1/admin/sessions/{id}/force-close`.** Both role `dcinv-admin`. Force-close body carries a structured `reason: str` (admin-only; logged into audit + NetBox journal). Idempotent: second concurrent force-close is a no-op state-wise but writes its own audit row for forensic visibility.
4. **Sprint 5 polish bundle.** (a) `GET /api/v1/meta/device-create-form` — serves the existing `device_create.yaml`. (b) Decommission `reason` plumbed into the NetBox journal entry comment (currently structured-log only).
5. **Specialised 422 translation extended across all NetBox-write endpoints.** Currently only `POST /api/v1/devices/` catches `NetBoxValidationError` (Sprint 5 Correction 2). Extend the catch to `PATCH /api/v1/devices/{id}`, `POST /api/v1/devices/{id}/comments`, `POST /api/v1/devices/{id}/decommission`, `POST /api/v1/qr/{id}/{bind,retire}`. Same structured `{"error": {"code": "NETBOX_VALIDATION_ERROR", ...}}` body across all of them.
6. **Acceptance + close-out.** Work-log entry + CLAUDE.md repository status + parking-lot updates + memory.

**Out of scope (Sprint 8a — production hardening):**

- NetBox circuit breaker (Architecture §3.3 deferral, carried since Sprint 3)
- Phase 2 partial-failure alerting (Architecture §3.1, parking-lot)
- Manual smoke against real Keycloak / NetBox (deferred every sprint; environment-blocked)
- Performance testing against ToR §5.1 targets (QR lookup p95 ≤ 800ms, device update p95 ≤ 1500ms)
- Multi-replica auto-end-job ownership (Postgres advisory lock or k8s CronJob — see decision A)
- Rate limiting per ToR §5.4.7
- Idempotency-key TTL cleanup job (no use case yet; carried from Sprints 2-6)

**Out of scope (Sprint 8b — user-facing deliverables):**

- HTML admin web pages per ToR §4.4.2 — `/web/`, `/web/batches/`, `/web/qr/search`, `/web/audit/`, `/web/users/`, `/web/sessions/`. Sprint 7 ships the JSON foundations for `/web/audit/` and `/web/sessions/`; HTML lives on top.
- Dashboard counters endpoint backing ToR §4.4.2 `/web/` (total QR count, batches last 30 days, free/bound/retired counts, recent activity feed)
- `GET /api/v1/admin/qr/{id}/history` — backs ToR §4.4.2 `/web/qr/search`. Task 2's `entity_id` audit filter partially covers this use case (`?entity_type=qr&entity_id=DCQR-XXX` gives the change history).
- PDF batch label generation (Architecture §6 deliverable; ToR §4.4.2 `/web/batches/{id}` Download/Print buttons)
- CSV export for `GET /admin/audit` (ToR §4.4.2 `/web/audit/` "Export to CSV") — JSON-only Sprint 7; CSV is a Sprint 8b web-presentation concern, not content negotiation on the JSON endpoint.

**Out of scope (deferred indefinitely; no current consumer):**

- `GET /api/v1/admin/users` — backs ToR §4.4.2 `/web/users/`. Needs a Keycloak admin client + `KEYCLOAK_ADMIN_CLIENT_*` env vars, which Sprint 6 decision J deliberately avoided. Sprint 8+ when the admin surface justifies the new attack surface.
- Error-shape unification on `GET /api/v1/qr/{id}` — mobile handles the current shape; low value to churn.
- Standalone `GET /api/v1/devices/{id}` populating `qr_id` from the app DB — the combined `GET /api/v1/qr/{id}` already provides this for the QR-driven flow.
- Gate `POST /api/v1/admin/batches/` on an active shift — blocked on a future admin sessions surface (decision F).
- iOS, MDM, offline write queue, bulk labeling — all ToR §13 Phase 2 items.

## Cross-cutting decisions

All decisions confirmed in the skeleton-review pass (grounded in ToR §4.1.3 + §4.4 + §5.4.6 + §7.2.4 + §8.3, UC-5, plus the Sprint 6 retrospective).

**A. Auto-end job mechanism — asyncio loop in app lifespan, no new dependency.** Four guardrails + three additions:

*Guardrails:*
- **Per-iteration try/except** wraps the whole tick: any exception is logged at ERROR with `exc_info` and the loop continues to the next interval. A single bad tick does NOT kill the loop or the app.
- **Cancellation via `asyncio.Event`** set in lifespan teardown. The loop checks the event before each iteration AND inside the sleep via `asyncio.wait_for(event.wait(), timeout=interval)`. Shutdown drains in ≤1s regardless of where the loop was in its cycle.
- **Multi-replica caveat documented** in the Sprint 7 work-log: "Backend MUST run as single-replica until job ownership is solved (Postgres advisory lock OR k8s CronJob). The `shift_sessions_one_active_per_user` partial unique index prevents the worst-case double-firing outcome (cannot create two new actives), and `end_reason=auto_timeout` is idempotent under last-write-wins, but N replicas waste N× the DB scans. Sprint 8a may revisit."
- **`/health` reports job sub-object**: `auto_end_job: { enabled, last_iteration_at, status }` where `status` is `"healthy"` if `last_iteration_at` is within `3 × SHIFT_AUTO_END_INTERVAL_SECONDS`, else `"stale"`. A silently-dead loop surfaces in operational monitoring instead of vanishing.

*Additions:*
- **Config knobs via `Settings`** — `SHIFT_AUTO_END_ENABLED: bool = True`, `SHIFT_AUTO_END_INTERVAL_SECONDS: int = 300` (5 min), `SHIFT_AUTO_END_THRESHOLD_HOURS: int = 12`. Lets tests disable the loop (`ENABLED=False`) and lets production tune interval/threshold without code changes.
- **Startup grace period** — 60s sleep before first iteration, so the loop doesn't dogpile with startup-time housekeeping (Alembic upgrade in entrypoint, JWKS warmup, etc.).
- **`/health` sub-object** (already in guardrails — listed twice for emphasis: it is BOTH operational visibility AND the rest of the team's signal that the job is alive).

**B. All admin endpoints under `/api/v1/admin/*`, role `dcinv-admin`.** Matches ToR §8.3's path scheme + Sprint 2's existing `/api/v1/admin/batches/`. Sprint 7 adds `/audit`, `/sessions`, `/sessions/{id}/force-close`. Mobile-driven `/api/v1/sessions/*` from Sprint 6 stays unchanged (separate path tree).

**C. Audit query pagination — offset, 1-indexed, `has_more` over `total_count`.**
- `page: int = 1` (REST convention; 1-indexed surfaces "page 1 is the first page" to web admins without arithmetic)
- `page_size: int = 20` default, capped at `100` (caps memory + protects DB)
- Response uses `has_more: bool` computed via `LIMIT page_size + 1` and slicing off the extra row. Cheap (one query, no `COUNT(*)`).
- **NOT** `total_count` — at 2-year retention × ~50 ops/day = ~36k rows minimum (ToR §5.4.6), `COUNT(*)` over a filtered query becomes an extra DB roundtrip per request. Web admin doesn't need a precise total for an audit log; "next/prev" UX backed by `has_more` is sufficient.

**D. Force-close body carries a structured `reason: str` (max 500 chars).** Body shape: `{"reason": "Engineer left without ending shift; paper handover register signed"}`. Required (not optional) — admin must justify the action. The reason flows into:
- `audit_log.after_json.reason` for forensic record

No NetBox journal entry posted for force-close. Force-close is a shift event, not a device event; the `audit_log` row is the canonical record. (Compare to Sprint 6 mobile `/end` which also does not post to NetBox — no device target for an event about the shift itself.) If a future requirement emerges for cross-referencing force-close with NetBox device activity, that's a separate feature.

**E. Enum rename to ToR-canonical names (Task 0).** Migration uses `ALTER TYPE shift_end_reason RENAME VALUE 'inactivity_timeout' TO 'auto_timeout'` and `RENAME VALUE 'admin_force_close' TO 'forced'`. Postgres preserves enum sort order across renames, so the partial unique index + CHECK constraint are unaffected. Sprint 6 retrospective gets a one-line addendum: "Sprint 7 Task 0 renamed enum to ToR-canonical names; descriptive names were a Sprint 6 design choice but ToR §7.2.4 contract wins." Code sweep affects: `app/domain/shift_session.py` (`ShiftEndReason` StrEnum values), `app/api/v1/sessions.py` (`SessionEndRequest.end_reason` `Literal`), all test files seeding/asserting the enum (`tests/unit/services/test_shift_session.py`, `tests/unit/api/v1/test_sessions.py`, integration tests if any). Rename happens BEFORE Task 1 (auto-end job) so the job writes ToR-canonical values from day one and there's never a third era of enum values. Existing audit rows are unaffected — the enum lives only on `shift_sessions.end_reason`, not on `audit_log`.

**F. `POST /api/v1/admin/batches/` NOT gated on active shift** (carried from Sprint 6). Admin batch generation has no shift surface; gating it would brick the only working admin endpoint. Sprint 8+ when there's an admin sessions surface (we have read + force-close but no admin start/end equivalent — admins don't open shifts on tablets).

**G. HTML web pages out of scope, deferred to Sprint 8b.** Sprint 7 ships JSON foundations for `/web/audit/` and `/web/sessions/` (`GET /api/v1/admin/audit`, `GET /api/v1/admin/sessions`). The Jinja2 pages themselves, plus `/web/`, `/web/batches/`, `/web/qr/search`, `/web/users/` per ToR §4.4.2, land in Sprint 8b. JSON-first means web can be a thin presentation layer that any client (the planned admin site, future tools, ad-hoc curl) can consume identically.

**H. CSV export deferred to Sprint 8b.** ToR §4.4.2 `/web/audit/` mandates "Export to CSV". JSON-only Sprint 7. CSV will land as a SEPARATE endpoint (`GET /web/audit/export.csv` or similar) rather than content-negotiation on `GET /api/v1/admin/audit` — keeps the JSON contract pure and lets the web layer add streaming + filename + Content-Disposition handling without polluting the API.

**I. `GET /api/v1/admin/audit` produces its own audit row** per ToR §5.4.6 ("Read operations on sensitive endpoints (audit log, user list) are also logged"). Shape:
- `operation: "audit.query"`
- `entity_type: "audit"`
- `entity_id: "search"` (hard-coded, so audit-of-audits is queryable: `?entity_type=audit&entity_id=search`)
- `before_json: {}`
- `after_json: {"filters": {... as-passed ...}, "results_count": N}` — filter params recorded as-passed (NOT hashed). Admins know they're auditable; transparency over privacy. Pagination params (`page`, `page_size`) included in `filters`.
- `result: SUCCESS` on a successful query; `FAILURE` if the query itself errors. CONFLICT is not applicable here (read-only).

The audit row is written AFTER the query result is computed (so `results_count` is accurate) and uses the same `request_id` + `user_keycloak_id` + `shift_session_id` plumbing as every other audit row — `require_role_with_active_shift("dcinv-admin")` gates the endpoint, so the shift attribution is consistent with Sprint 6 decision D. (Note: this means an admin must have an active shift to query the audit log. Sprint 8+ admin-without-shift surface is a separate problem.)

**J. Pre-Sprint-6 audit rows included without flag in `session_id` filter queries.** A `?session_id=<uuid>` filter that matches a pre-2026-05-30 row will surface it identically to a post-2026-05-30 row. The semantic divergence (Sprint 6 decision D) is documented in the endpoint's OpenAPI description:

> `session_id: UUID` — Filter by shift session UUID. **Note:** audit rows before 2026-05-30 contain JWT session IDs (ephemeral; rotate within a shift). For historical data, query by `user_keycloak_id` + date range instead.

Web admins reading the description understand the semantic; ad-hoc consumers querying by `session_id` get a row that may not be the shift they think it is, but `user_keycloak_id` + `timestamp` are still authoritative. No code-level distinction between the two eras — the column type is identical.

## Task list

Each task gets full Goal / Steps / Acceptance / Anti-criteria / Suggested prompt added in the post-`/clear` detail pass. Skeleton names + one-line goals only here.

---

### Task 0 — `shift_end_reason` enum rename to ToR canon

**Goal:** Postgres `ALTER TYPE ... RENAME VALUE` migration (`inactivity_timeout` → `auto_timeout`, `admin_force_close` → `forced`) + mechanical sweep of `ShiftEndReason` StrEnum values + `Literal[...]` in `SessionEndRequest` + test fixture strings + Sprint 6 retrospective addendum in `docs/work-log.md`. Done before Task 1 (decision E).

### Task 1 — Auto-end stale-sessions background job

**Goal:** Asyncio loop in `app/main.py` lifespan that scans `shift_start_at < NOW() - SHIFT_AUTO_END_THRESHOLD_HOURS AND shift_end_at IS NULL` every `SHIFT_AUTO_END_INTERVAL_SECONDS` and ends each match with `end_reason='auto_timeout'`. Four guardrails + three additions per decision A. `/health` extended with `auto_end_job` sub-object. Multi-replica caveat documented in work-log.

### Task 2 — `GET /api/v1/admin/audit` query endpoint

**Goal:** Role `dcinv-admin`. Filters per decision C (`user_keycloak_id`, `from`, `to`, `entity_type`, `entity_id`, `operation`, `session_id`, `result`) + offset pagination + `has_more`. New `AuditLogRepository.query(...)` method. Endpoint produces its own audit row per decision I. OpenAPI description includes the decision J semantic note.

### Task 3 — `GET /api/v1/admin/sessions` + force-close

**Goal:** Both role `dcinv-admin`. `GET /api/v1/admin/sessions` — list shifts with filters (`user_keycloak_id`, `from`, `to`, `active_only: bool`) + offset pagination. `POST /api/v1/admin/sessions/{id}/force-close` — body `{"reason": str}` (required, max 500 chars), ends with `end_reason='forced'`, writes audit row, optionally posts NetBox journal entry per decision D detail. Idempotent: second concurrent force-close on an already-ended session is a no-op state-wise but writes its own audit row (decision 6 in skeleton-review).

### Task 4 — Sprint 5 polish bundle

**Goal:** (a) `GET /api/v1/meta/device-create-form` — serves the existing `app/services/forms/device_create.yaml` (the YAML shipped in Sprint 5; the endpoint was deferred). Role `dcinv-mobile-user`, cached client-side via the same version-field pattern as `/meta/device-form`. (b) Decommission `reason` field plumbed into the NetBox journal entry comment. Currently `DeviceDecommissionService` binds `reason` to a structured log field only; extend `NetBoxWriteService.patch_with_attribution`'s journal text to include it when present.

### Task 5 — Specialised 422 translation across all write endpoints

**Goal:** Extend Sprint 5 Correction 2's `NetBoxValidationError → 422 NETBOX_VALIDATION_ERROR` catch from `POST /api/v1/devices/` to five additional write endpoints:
- `PATCH /api/v1/devices/{id}`
- `POST /api/v1/devices/{id}/comments`
- `POST /api/v1/devices/{id}/decommission`
- `POST /api/v1/qr/{id}/bind`
- `POST /api/v1/qr/{id}/retire`

Same structured body shape across all of them. Carries the audit row's `result=FAILURE` path consistently.

Note: `NetBoxValidationError` on QR bind/retire is rare in practice (invariants caught by the `qr_one_per_device` index before the NetBox call, device 404 → `NetBoxNotFound`, version conflict → `WriteConflictError`). Included for consistency — if NetBox does return 4xx, 502 is misleading.

### Task 6 — Acceptance + close-out

**Goal:** Sprint 7 done means tests green, gates clean, work-log + CLAUDE.md + memory + parking-lot updated. Work-log entry must explicitly call out: decision A's multi-replica caveat for the auto-end job (single-replica until Sprint 8a); decision E's enum rename + Sprint 6 retrospective addendum; decision I's audit-of-audits row shape; decision J's pre-Sprint-6 row semantic in OpenAPI.

---

## Working principles (carried from Sprints 1–6)

- **TDD discipline.** Tests first, including failure-mode counterparts. No happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit "go", then code. Task 0 in particular — Postgres `ALTER TYPE` on a live enum touched by domain + Pydantic + tests — deserves careful pre-implementation review.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are met. The gate is the user's. Strict ordering: **Task 0 before Task 1** (decision E rationale).
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–6. `--cov-fail-under=100` gate at close-out.
- **No new dependencies** without explicit approval. Sprint 7's auto-end job is the natural place a scheduler dep (APScheduler, etc.) might be considered — decision A is explicit that we DON'T take one.
- **Endpoint handler tests:** test handler logic by direct `await`; use `TestClient`/`AsyncClient` only for routing, role-gating, and `response_model` shaping. Same call as Sprints 2-6.
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 7 exercises #2 (three-record write — admin audit query produces its own audit row per decision I; force-close produces its own audit row + optional NetBox journal per decision D) and #7 (destructive migrations split across releases — Task 0's `ALTER TYPE RENAME VALUE` is **non-destructive** under that rule because no column or table is dropped and no constraint is invalidated; flagged here so reviewers don't misread it).
- **Reuse existing apparatus.** `AuditLogRepository` pattern from Sprint 2; `ShiftSessionService` pattern from Sprint 6; `NetBoxWriteService.patch_with_attribution` reuse for Task 3's optional journal post; `require_role_with_active_shift` from Sprint 6 for the admin endpoints. **Don't rewrite the apparatus.**
- **`RuntimeError` over `assert`** for defensive runtime guards (Sprint 1 M3 / Sprint 4 Q2 / Sprint 6 dep-layer pattern). Assert only for mypy type narrowing.
- **`-> NoReturn` annotation** on helpers that always raise (Sprint 5 Task 4 polish lesson).
- **`mypy app/ tests/` at every task close-out**, not just `mypy app/` (Sprint 5 lesson, held through Sprint 6).
- **No new env vars without a `Settings` field + a test that exercises the default** (Sprint 1 lesson, reinforced by Task 1's three new config knobs).

## Reference documents

- `DC_Inventory_ToR_v3.docx`:
  - **§4.1.3** — shift start/end UX + 10-min inactivity auto-end (Task 1 contract; mobile-owned 10-min timer landed in Sprint 6, backend 12h fallback lands here)
  - **§4.4** — Web Interface section that motivates the JSON foundations (Tasks 2, 3); §4.4.2 explicitly lists `/web/audit/` and `/web/sessions/` as the consumers
  - **§5.1** — performance NFRs (no Task 2/3 explicit budget but audit-query latency should be snappy at 36k+ rows; flag for Task 2 detail)
  - **§5.4.6** — "Read operations on sensitive endpoints (audit log, user list) are also logged" — load-bearing for decision I; "Audit logs are retained for 2 years" — sizing input for decision C
  - **§7.2.4** — `shift_sessions` schema + enum (`manual / auto_timeout / forced`) — load-bearing for Task 0 / decision E
  - **§8.3** — Admin Endpoints table — load-bearing for Tasks 2, 3 (paths + role); does NOT enumerate force-close (decision D's URL is our design)
  - **UC-5** — paper handover register; "Engineer A's session is closed in shift_sessions with end_reason=manual" — supports admin force-close as recovery when Engineer A left without ending
- `Architecture_Overview.md`:
  - **§3.1** — three-record write apparatus (Tasks 2, 3 audit-row production)
  - **§3.3** — circuit breaker (out of scope; Sprint 8a)
  - **§4** — DB-enforced state machine (unchanged this sprint; Task 0 rename does not touch CHECK or partial unique index)
  - **§5** — server-driven device-edit form (Task 4a `device_create_form` endpoint mirrors `device_form` shape)
  - **§6** — PDF generation (out of scope; Sprint 8b)
  - **§8.1** — destructive migration policy (Task 0's `ALTER TYPE RENAME VALUE` is non-destructive under this rule; flagged in working principles)
- `docs/sprint-3.md` — three-record write decisions (Task 2 audit-row source-of-truth)
- `docs/sprint-5.md` — Correction 2 `NetBoxValidationError → 422` translation (Task 5 extension target); `device_create.yaml` form config split (Task 4a endpoint to serve)
- `docs/sprint-6.md` — decision E split (mobile owns 10-min timer, Sprint 7 owns 12h backend fallback — Task 1 contract); decision D audit-row semantic flip (Task 2 OpenAPI description); decision J Keycloak revoke split (Task 3 force-close does NOT revoke; mobile contract is unchanged)
- `docs/work-log.md` — Sprint 6 entry's "Deliberately deferred" list enumerates exactly the Sprint 7 in-scope items (auto-end job, admin sessions list + force-close, admin audit query); cross-references the Sprint 5 polish bundle (decommission reason + 422 translation extension)
- `docs/parking-lot.md` — "Admin sessions surface" entry (Sprint 6 close-out) — Tasks 1, 3 directly close it; "`audit_log.session_id` semantic change" entry — Task 2 implements the OpenAPI documentation called for there
