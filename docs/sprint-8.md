# Sprint 8a — Production Hardening

> **Status:** Planned. Awaiting Task 0 go/no-go. (Per-task detail added as we get to each task — same rhythm as Sprint 7.)
> **Duration target:** 5–6 working days
> **Goal:** Close the production-readiness gap left after Sprint 7. The admin endpoints work in tests but are 409-bricked in production; the auto-end job requires single-replica deployment; NetBox has no resilience guarantees; rate limiting is unimplemented. Sprint 8a unblocks live admin use and removes the single-replica + no-circuit-breaker + no-rate-limit caveats.

## Why this sprint exists

Sprint 7 shipped admin endpoints + the auto-end background job + extended NBV translation. But three structural gaps remain:

- **Admin endpoints can't be used live.** `GET /admin/audit`, `GET /admin/sessions`, `POST /admin/sessions/{id}/force-close` all gate on active shift (decision I from Sprint 7); there's no API for admins to open one. Live admin use returns 409 `NO_ACTIVE_SHIFT`. `POST /admin/batches/` is the lone admin endpoint that works today, but it's intentionally un-gated and inconsistent with the rest of the surface.
- **Backend is single-replica-only.** Sprint 7's auto-end loop runs in the FastAPI lifespan with no job-ownership guard. Documented caveat; needs a solution before horizontal scaling.
- **No NetBox resilience.** Architecture §3.3 deferral, carried since Sprint 3. A NetBox blip (timeout, 503, rolling restart) currently surfaces as a wave of 502s. ToR §5.1 implies "reasonable uptime"; without a circuit breaker, repeated retries during an outage compound load on a stuck NetBox.
- **No rate limiting.** ToR §5.4.7 explicitly requires it. Today a misbehaving mobile client can hammer the backend without restraint.

Sprint 8a ships:

- Admin-shift-open API + `POST /admin/batches/` gating + `QRGenerationService` audit-row session_id source swap
- Multi-replica auto-end-job ownership via Postgres advisory lock
- NetBox circuit breaker (using `circuitbreaker` PyPI package — first pyproject deviation since Sprint 1)
- Rate limiting per ToR §5.4.7
- Close-out with a one-time performance baseline against ToR §5.1 targets

This unlocks **horizontal scaling**, **live admin operations**, **NetBox-outage resilience**, and **abuse-resistance**, leaving Sprint 8b free to focus on user-facing deliverables (HTML admin pages, PDF labels, CSV export, dashboard counters) on top of a hardened foundation.

## Scope boundaries

**In scope — 5 tasks:**

0. **Admin-shift-open API + `POST /admin/batches/` gating.** New `POST /api/v1/admin/sessions/start` (admin variant of mobile's start — body `{workstation_id}` instead of `{tablet_id}` to make the surface semantically distinct). Reuses `ShiftSessionService.start` primitive. Switch `POST /admin/batches/` to `require_role_with_active_shift("dcinv-admin")` + change `QRGenerationService` audit row's `session_id` from hardcoded `None` to `user.shift_session_id`. Unblocks live admin use of Sprint 7's three admin endpoints.

1. **Multi-replica auto-end-job ownership via Postgres advisory lock.** Each iteration wraps in `pg_try_advisory_lock(<auto_end_job_lock_id>)`; if not acquired, skip + log at INFO. N replicas safe — only one runs the work per interval; the others wake, fail the lock acquisition, and sleep. Remove the single-replica caveat from `app/main.py`'s code comment, `docs/parking-lot.md`, and the Sprint 7 work-log entry.

2. **NetBox circuit breaker (Architecture §3.3).** Wrap the NetBox HTTP client with a circuit that opens on N consecutive failures + half-opens after M seconds. Surface state on `/health` as a sub-object (mirrors Sprint 7 Task 1's `auto_end_job` pattern). **Uses the `circuitbreaker` PyPI package** (small, well-tested, no transitive deps) — first pyproject deviation since Sprint 1; justification recorded in the deviations section of the Sprint 8a work-log entry.

3. **Rate limiting per ToR §5.4.7.** Per-user (keycloak_id) + per-endpoint-class (read/write/admin) sliding-window. **In-process state** initially — cross-replica state lives behind a separate decision. Returns 429 + `Retry-After` header on exhaustion; structured body. Documents the in-process trade-off + the lift-to-Postgres-or-Redis decision deferred to the first multi-replica deployment.

4. **Acceptance + close-out.** Work-log entry + CLAUDE.md repository status + parking-lot updates + memory. **Performance baselines** folded in here as a one-time measure-and-document step (NOT added to CI): QR lookup p95 vs ToR §5.1's ≤ 800ms target, device update p95 vs ≤ 1500ms target, against the test DB + respx-mocked NetBox. Recorded numbers go in the work-log under a "Performance baselines" subsection.

**Out of scope (Sprint 8b — user-facing deliverables):**

- HTML admin web pages per ToR §4.4.2 — `/web/`, `/web/batches/`, `/web/qr/search`, `/web/audit/`, `/web/users/`, `/web/sessions/`
- Dashboard counters endpoint backing `/web/` (total QR count, batches last 30 days, free/bound/retired counts, recent activity feed)
- `GET /api/v1/admin/qr/{id}/history` — partly covered by Sprint 7 Task 2's `entity_id` filter (`?entity_type=qr&entity_id=DCQR-XXX`); dedicated endpoint can land here or stay deferred
- PDF batch label generation (Architecture §6 deliverable)
- CSV export for `GET /admin/audit` (Sprint 7 decision H)
- `GET /api/v1/admin/users` — needs Keycloak admin client + `KEYCLOAK_ADMIN_CLIENT_*` env vars (Sprint 6 decision J deliberately avoided)

**Out of scope (Sprint 9+):**

- **Phase 2 partial-failure alerting** (Architecture §3.1 parking-lot) — depends on operational monitoring infrastructure not yet in place (Prometheus / Loki / etc.). Sprint 8a surfaces enough state on `/health` for an external monitor to scrape; alerting rules are an operations concern, not a backend deliverable.
- **Idempotency-key TTL cleanup job** — carried from Sprints 2-7, no consumer yet
- iOS, MDM, offline write queue, bulk labeling — ToR §13 Phase 2

## Cross-cutting decisions

**A. Admin-shift-open uses `workstation_id`, NOT `tablet_id`.** Semantically distinct from the mobile path even though both write to the `shift_sessions` table. Engineers identify their physical scanner by tablet asset tag; admins identify their workstation by hostname or admin-portal session id (TBD in Task 0 detail). Two separate body schemas (`SessionStartRequest` for mobile, `AdminSessionStartRequest` for admin) avoid confusing the two flows at the API layer. The DB column stays `tablet_id` (re-purposed as "originating-device-id"); the wire field is what the API layer renames.

**B. `POST /admin/batches/` gating + `QRGenerationService` audit-row source swap ship in the same commit as Task 0.** They're paired: gating with no session_id source swap would still write `session_id=None` audit rows, half-fixing the problem.

**C. Admin-shift-open does NOT require an `Idempotency-Key` header.** Mobile shifts also don't (they rely on `SessionAlreadyActive` 409 with the existing-shift payload for retry safety). Consistency wins.

**D. Advisory lock id is a constant in `app/config.py`.** `_AUTO_END_JOB_ADVISORY_LOCK_ID: int = ` deterministic value derived from a hash of `"auto_end_job"` or just a chosen integer. Postgres `pg_advisory_lock(bigint)` takes any `bigint`; the value just needs to not collide with other advisory-lock users. Single advisory-lock user today, so any non-zero `bigint` works. Detailed plan picks the literal.

**E. Circuit breaker uses `circuitbreaker` PyPI package (confirmed).** First `pyproject.toml` dependency added since Sprint 1. Justification (recorded in Sprint 8a work-log deviations section): rolling our own would be ~150 lines of state-machine code with subtle timing concerns (open → half-open transition under concurrency); the PyPI package is ~200 LOC, no transitive deps, well-tested, and a one-line `from circuitbreaker import circuit` integration into the NetBox client wrapper. The "no new deps" Sprint 1 stance was about avoiding heavy frameworks (APScheduler, Celery); a small focused package for a specific resilience pattern is the kind of dep that pays for itself.

**F. Rate-limit state is in-process for Sprint 8a.** Cross-replica state needs Postgres- or Redis-backed storage; both are larger decisions deferred to the first multi-replica deployment. In-process means each replica enforces its own budget; total cluster-wide rate is N × the per-replica budget. Acceptable for single-replica today; documented loud caveat for when Task 1 + the future multi-replica rollout meet rate limiting.

**G. Rate limits are per-`user_keycloak_id` + per-endpoint-class.** NOT per-IP (mobile clients are behind a VPN and share IPs). Three classes: read (60 req/min), write (20 req/min), admin (30 req/min) — defaults to be refined in Task 3's detailed plan. Configurable via three `Settings` knobs.

**H. 429 response shape: `{"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "...", "retry_after_seconds": N}}` + standard `Retry-After: N` header.** Mobile clients can read either; the header is conventional, the body is consistent with our other error shapes.

**I. Performance baselines (Task 4) are measure-and-document, NOT CI-recurring.** ToR §5.1 targets exist for product acceptance, not regression. Adding CI perf tests against a respx-mocked NetBox + a tmpfs test DB would lock in numbers that don't reflect production. A one-shot run during close-out establishes "we measured, this is what we got, here are the conditions" — operators run their own perf tests against production-like infra.

**J. NetBox circuit-breaker state on `/health` mirrors Sprint 7 Task 1's `auto_end_job` pattern.** Informational, NOT a 503 trigger. A circuit in `OPEN` state means NetBox is hosed; that already shows up in the per-downstream `netbox: {status: ...}` sub-field. Adding `netbox_circuit: {state, opened_at, failure_count}` gives operators the explicit reason without changing the 503 contract.

## Task list

Each task gets full Goal / Steps / Acceptance / Anti-criteria / Suggested prompt added in the per-task plan-then-confirm pass. Skeleton names + one-line goals only here.

---

### Task 0 — Admin-shift-open API + `POST /admin/batches/` gating

**Goal:** Ship `POST /api/v1/admin/sessions/start` (role `dcinv-admin`, body `{workstation_id}`) reusing `ShiftSessionService.start`; switch `POST /admin/batches/` to `require_role_with_active_shift("dcinv-admin")`; switch `QRGenerationService`'s audit row source from `session_id=None` to `user.shift_session_id`. Unblocks live admin use of Sprint 7's three admin endpoints.

### Task 1 — Multi-replica auto-end-job ownership via Postgres advisory lock

**Goal:** Wrap `_run_iteration` body in `pg_try_advisory_lock(<id>)`; skip + log if not acquired. Remove single-replica caveat from `app/main.py` code comment, parking-lot, and Sprint 7 work-log entry. Integration test: spawn two job tasks against the same DB, assert only one writes the audit rows per interval.

### Task 2 — NetBox circuit breaker

**Goal:** Wrap NetBox HTTP client with `circuitbreaker` PyPI package's `@circuit` decorator (or programmatic equivalent for the async paths). Three settings: `NETBOX_CIRCUIT_FAILURE_THRESHOLD`, `NETBOX_CIRCUIT_RECOVERY_TIMEOUT_SECONDS`, `NETBOX_CIRCUIT_ENABLED`. `/health` extended with `netbox_circuit: {state, opened_at, failure_count}` sub-object (informational per decision J). Pyproject deviation recorded.

### Task 3 — Rate limiting per ToR §5.4.7

**Goal:** Per-user (keycloak_id) + per-endpoint-class sliding-window or token-bucket in-process middleware. Three classes (read / write / admin) with three default-rate `Settings` knobs. 429 + `Retry-After` + structured body. Multi-replica caveat documented (in-process state means N × per-replica budget cluster-wide).

### Task 4 — Acceptance + close-out + performance baselines

**Goal:** Sprint 8a done means tests green, gates clean, work-log + CLAUDE.md + memory + parking-lot updated. Performance baseline measure-and-document: QR lookup p95 + device update p95 against test DB + respx-mocked NetBox, recorded in the work-log under "Performance baselines". Parking-lot entries for the single-replica caveat (Task 1), NetBox circuit breaker (Task 2), and rate limiting (Task 3) marked RESOLVED.

---

## Working principles (carried from Sprints 1–7)

- **TDD discipline.** Tests first, including failure-mode counterparts. No happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit "go", then code.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are met.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–7. `--cov-fail-under=100` gate at close-out.
- **No new dependencies without explicit approval.** Sprint 8a Task 2 adds `circuitbreaker` PyPI — pre-approved at sprint plan stage (decision E); this is the only deviation expected.
- **Endpoint handler tests:** test handler logic by direct `await`; use `TestClient`/`AsyncClient` only for routing, role-gating, and `response_model` shaping. Same call as Sprints 2-7.
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 8a Task 0 exercises #2 (three-record write — `QRGenerationService` audit row source swap brings batch generation into the three-record-write apparatus); Task 1 exercises #4 (DB-enforced state — advisory lock is an additional Postgres mechanism on top of the existing CHECK + partial unique index); Task 2 exercises Architecture §3.3 (circuit breaker is the long-deferred resilience guarantee).
- **Reuse existing apparatus.** `ShiftSessionService.start` (Task 0); `_run_iteration` from Sprint 7 Task 1 (Task 1); the FastAPI middleware pattern (Task 3); `/health` sub-object pattern from Sprint 7 Task 1 (Task 2).
- **`RuntimeError` over `assert`** for defensive runtime guards.
- **`mypy app/ tests/` at every task close-out**, not just `mypy app/`.
- **No new env vars without a `Settings` field + a test that exercises the default** (Sprint 1 lesson, reinforced by every sprint since).

## Reference documents

- `DC_Inventory_ToR_v3.docx`:
  - **§5.1** — performance NFRs (Task 4 baseline targets: QR lookup p95 ≤ 800ms, device update p95 ≤ 1500ms)
  - **§5.4.7** — rate limiting requirement (Task 3 contract)
  - **§8.3** — Admin Endpoints table (Task 0 path `/api/v1/admin/sessions/start` is consistent with the existing `/api/v1/admin/sessions/` + `/api/v1/admin/sessions/{id}/force-close` from Sprint 7)
- `Architecture_Overview.md`:
  - **§3.1** — three-record write apparatus (Task 0 `QRGenerationService` source swap)
  - **§3.3** — circuit breaker (Task 2 — long-deferred since Sprint 3)
  - **§4** — DB-enforced state machine (Task 1 advisory lock is an additional Postgres mechanism on top of CHECK + partial unique index)
- `docs/sprint-3.md` — NetBox circuit breaker was first deferred here; Sprint 8a Task 2 closes the loop
- `docs/sprint-6.md` — `ShiftSessionService.start` apparatus (Task 0 reuses 1:1)
- `docs/sprint-7.md` — admin endpoints decision I (active-shift gate); Task 1 single-replica caveat; Task 2's `/health` sub-object pattern
- `docs/work-log.md` — Sprint 7 entry's "Deliberately deferred" + "Architectural decisions worth carrying forward" sections enumerate exactly Sprint 8a's in-scope items
- `docs/parking-lot.md` — "Admin sessions surface" entry (residual: admin-shift-open API → Task 0), "Multi-replica auto-end-job ownership" entry (→ Task 1), pre-Sprint-7 entries for NetBox circuit breaker + rate limiting (→ Tasks 2 + 3)
