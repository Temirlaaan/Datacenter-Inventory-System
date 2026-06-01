# Sprint 6 — Shift Sessions

> **Status:** Skeleton awaiting user review. No per-task detail yet (added after skeleton-review + `/clear`).
> **Duration target:** as much as needed (user spec)
> **Goal:** Give the system a real notion of "which shift" beyond the ephemeral JWT `sid` claim. Add a `shift_sessions` table + endpoints to start/end/query the active shift, then re-source `audit_log.session_id` to that table so audit forensics tie operations to shifts, not transient tokens.

## Why this sprint exists

Sprint 3 wired audit rows with `session_id` from the JWT `sid` claim (decision C). That works for "which JWT issued this op" but is too ephemeral for shift-scoped forensics — JWTs rotate during a shift, so a single shift's operations end up scattered across multiple `session_id` values. The ToR's shift concept (engineer starts a shift, performs ops, ends the shift) isn't yet first-class anywhere.

Sprint 6 makes shifts first-class:

- A `shift_sessions` table the backend owns (not Keycloak)
- Three mobile endpoints (`/start`, `/end`, `/active`)
- All audit rows from Sprint 3/4/5 services switch their `session_id` source to the active `shift_sessions.id` for the JWT-identified user

This unlocks Sprint 7+ work: per-shift audit query (`GET /admin/audit?session_id=...`), per-engineer shift-history admin views, stale-session cleanup jobs.

## Scope boundaries

**In scope — 5 tasks:**

1. `shift_sessions` migration + domain types + repository (partial unique index `WHERE shift_end_at IS NULL` for "≤1 active per user" + CHECK constraint pairing `shift_end_at` with `end_reason`)
2. `ShiftSessionService` with start/end/get_active state machine
3. Three endpoints: `POST /api/v1/sessions/start`, `POST /api/v1/sessions/end`, `GET /api/v1/sessions/active`
4. Re-source `audit_log.session_id` across all existing services (`NetBoxWriteService`, `QRLifecycleService`, `DeviceDecommissionService`, `CommentService`) — new writes look up the active shift; no historical migration
5. Acceptance + close-out

**Out of scope (Sprint 7+):**

- **Auto-end stale sessions** (background job ending sessions older than 12h). Sprint 7 candidate.
- **Admin override** to end another user's session. Sprint 7+.
- **Session list / history endpoint** for admins.
- **`GET /api/v1/admin/audit` query endpoint** — needs Sprint 6 sessions to be useful, so deferred to Sprint 7.
- **Sprint 5 carry-over** — decommission `reason` plumbed into NetBox journal comment; error-shape unification on `GET /qr/{id}`; `GET /api/v1/meta/device-create-form`; standalone `/devices/{id}` populating `qr_id` from app DB; specialised 422 translation across all write endpoints. All carried into Sprint 7+.
- **PDF labels, web admin, NetBox circuit breaker, idempotency-key TTL cleanup** — pre-existing deferrals.

**Already done in Sprint 5 close-out (per the user's Task 5 spec — flagging so we don't redo):**

- **Backend submodule flatten into root repo** — completed in commits `6ba7db5` (gitlink removed) and `55e2710` (subtree merge preserving backend's 9-commit history).
- **Push to GitHub origin** — `c8eded2..16dfe73` published; `main` is current with all Sprint 1-5 work.

Task 5 therefore covers normal close-out only (work-log, CLAUDE.md, parking-lot, memory).

## Cross-cutting decisions

These apply across multiple tasks; capturing once so each task doesn't re-litigate. All decisions confirmed after the skeleton-review pass (ToR §4.1.3 + §7.2.4 + UC-5 grounding).

**A. Session model — server-side only.** JWT carries `sub` + `sid` for auth (Sprint 3 unchanged); the backend maintains `shift_sessions` as its own table for shift tracking. Mobile doesn't need to surface the JWT/shift distinction. JWT `sid` claim stops being used as the audit row's `session_id` after this sprint (new audit rows source `session_id` from `shift_sessions.id` for the JWT-identified user; old rows keep their JWT-derived value). ToR §4.3.1's NetBox journal text already references `Session: {session_id}` as the shift id, so this matches the documented contract.

**B. One active session per user at a time.** Hard constraint via partial unique index `WHERE shift_end_at IS NULL`. `start()` with an existing active session for the same `user_keycloak_id` → 409 `SESSION_ALREADY_ACTIVE` with the active session in the body so mobile can show "you already have a shift open since X — end it first?".

**C. `/end` acts on the JWT-identified user's active session, not a session UUID.** Mobile POSTs `{"end_reason": "manual" | "inactivity_timeout"}`; server resolves the active session for the user from the JWT `sub`. Avoids the client having to remember the session UUID across app restarts.

**D. `audit_log.session_id` column type unchanged.** Already `UUID`. `shift_sessions.id` is `UUID`. JWT `sid` is a UUID string. Only the source of the value changes (a service-layer lookup), not the schema. No migration on existing rows. **Semantic change documented in the Sprint 6 work-log entry**: pre-Sprint-6 rows hold JWT sid (ephemeral token UUID); post-Sprint-6 rows hold `shift_sessions.id` (shift UUID); audit-query consumers must handle both interpretations or filter by `created_at > <sprint-6-close-date>`.

**E. Auto-end after 10 min inactivity — SPLIT implementation.** ToR §4.1.3 mandates "After 10 minutes of inactivity ... the session is fully terminated". Split:
- **Mobile workstream**: detects user idle → calls `POST /sessions/end` with `{"end_reason": "inactivity_timeout"}`. Owns the 10-minute timer.
- **Sprint 7 background job**: scans `shift_start_at < NOW() - 12h AND shift_end_at IS NULL`, ends with `end_reason="inactivity_timeout"`. 12h is liberal so the mobile catches the 10-min case correctly; backend is the fallback for crashed/offline phones.
- **Sprint 6 API** supports both: `/sessions/end` accepts `end_reason` in body. `admin_force_close` is reserved for Sprint 7+'s admin endpoint, not exposed via `/sessions/end`.

**F. Active-session lookup happens at the dependency layer (F.a).** `get_current_user` (or a downstream `get_current_user_with_shift`) resolves the active shift by `user_keycloak_id` after JWT validation and binds `shift_session_id: UUID | None` onto `AuthUser`. Single SQL lookup per request, indexed (`user_keycloak_id WHERE shift_end_at IS NULL`), cheap. Services pulling `AuthUser` get the shift id free. Impossible to forget at a new write site (vs F.b's per-call-site lookup).

**G. No-active-shift writes are rejected (G.a).** Any op that writes to `audit_log` requires an active shift; the dep layer raises 409 `NO_ACTIVE_SHIFT` before the service runs. Confirmed by ToR §4.1.3: "During the shift, all operations are attributed to this session" — operations outside a shift are not permitted. Read endpoints (`GET`) stay unaffected (they don't write `audit_log`).

**H. `shift_sessions` schema** (ToR §7.2.4 + UC-5 grounded):

| Column | Type | Constraint |
|---|---|---|
| `id` | `UUID` | PK |
| `user_email` | `TEXT` | NOT NULL |
| `user_keycloak_id` | `UUID` | NOT NULL |
| `shift_start_at` | `TIMESTAMPTZ` | NOT NULL (ToR column name) |
| `shift_end_at` | `TIMESTAMPTZ` | NULL (ToR column name) |
| `tablet_id` | `TEXT` | **NOT NULL** (ToR §4.1.3 + UC-5 paper-handover cross-reference) |
| `end_reason` | `shift_end_reason` enum | NULL on active sessions |

`shift_end_reason` is a PostgreSQL enum (mirroring `qr_status` + `audit_result` from Sprint 2/3): values `'manual' | 'inactivity_timeout' | 'admin_force_close'`. `admin_force_close` is reserved for a Sprint 7+ admin endpoint; Sprint 6's `/end` accepts only `manual` and `inactivity_timeout`.

DB-enforced consistency CHECK (mirrors Sprint 2's `qr_state_consistency` pattern):
```sql
CHECK (
  (shift_end_at IS NULL AND end_reason IS NULL) OR
  (shift_end_at IS NOT NULL AND end_reason IS NOT NULL)
)
```
Active session has neither; closed session must have both. No "tablet_id nullable for non-mobile roles" carve-out — `/sessions/start` is mobile-only (decision I), and a future `/admin/sessions/start-for-user` (Sprint 7+) will define its own `tablet_id` semantic at that time.

**I. Endpoint roles.** All three session endpoints: `dcinv-mobile-user` (mobile-driven). Admin list (`/api/v1/admin/sessions`) and admin force-close are deferred to Sprint 7+ per the out-of-scope list.

**J. Keycloak refresh-token revocation: mobile self-revokes (J.a).** Backend's `/sessions/end` does NOT call Keycloak's admin API. Mobile is responsible for calling Keycloak's `/logout/revoke` endpoint with its refresh token after `/sessions/end` returns success. Preserves Sprint 1's "mobile owns its tokens" principle, avoids adding a Keycloak admin client + new env vars (`KEYCLOAK_ADMIN_CLIENT_*`) to the backend, and matches the way mobile already speaks to Keycloak for OIDC login/refresh. **Sprint 6 work-log will document this split explicitly so the mobile team has the contract in writing**: "Backend `/sessions/end` does NOT call Keycloak revoke. Mobile is responsible for calling Keycloak `/logout/revoke` after `/sessions/end` success. This split is intentional per Sprint 1 'mobile owns tokens' principle."

## Task list

Each task gets full Goal / Steps / Acceptance / Anti-criteria / Suggested prompt added in the post-`/clear` detail pass. Skeleton names + one-line goals only here.

---

### Task 1 — `shift_sessions` schema + domain + repository

**Goal:** Add the Alembic migration (table + `shift_end_reason` enum + partial unique index `WHERE shift_end_at IS NULL` + CHECK constraint pairing `shift_end_at` with `end_reason` per decision H), domain `ShiftSession` dataclass with start/end transition methods, and `ShiftSessionRepository` (start, end, get_active_for_user, get_by_id). Mirrors Sprint 2's `qr_codes` DB-enforced state pattern.

### Task 2 — `ShiftSessionService` state machine

**Goal:** Service-layer orchestration: `start(user, tablet_id) → ShiftSession` (409 if active exists, decision B), `end(user, end_reason) → ShiftSession` (409 if no active, decision C; accepts `end_reason ∈ {manual, inactivity_timeout}` per decision E), `get_active(user) → ShiftSession | None`. No NetBox interaction. Defensive `in_transaction()` guard per Sprint 4 Q2.

### Task 3 — Session endpoints

**Goal:** `POST /api/v1/sessions/start` (body `{tablet_id: str}`), `POST /api/v1/sessions/end` (body `{end_reason: "manual" | "inactivity_timeout"}` — decision E split), `GET /api/v1/sessions/active`. Role `dcinv-mobile-user` (decision I). Structured `{"error":{"code":...}}` body for 409s (`SESSION_ALREADY_ACTIVE`, `NO_ACTIVE_SHIFT`). 422 on Pydantic violations (missing `tablet_id`, invalid `end_reason`).

### Task 4 — Re-source `audit_log.session_id` across existing services

**Goal:** Plumb the active-session lookup per decision F.a — enrich `AuthUser` in the `get_current_user` dep with `shift_session_id`, then update every audit-row write site to read it from `AuthUser` (already plumbed). Touches `NetBoxWriteService`, `QRLifecycleService`, `DeviceDecommissionService`, `CommentService`. Also wires the dep-layer 409 `NO_ACTIVE_SHIFT` reject for write endpoints per decision G. No historical `audit_log` migration; the column-semantic change is documented per decision D.

**Existing-test fan-out (mechanical mass-update, but plan it — don't discover it mid-implementation):** every Sprint 3/4/5 integration test that asserts on `audit_log.session_id` currently expects the JWT `sid`; those assertions must be updated to expect the seeded `shift_sessions.id`. Test fixtures for write ops must seed an active shift via `ShiftSessionRepository.start()` (or a helper) BEFORE invoking the write endpoint — otherwise decision G's dep-layer 409 `NO_ACTIVE_SHIFT` fires and the test never reaches the audit-row assertion. Scope: `test_qr_bind.py`, `test_qr_retire.py`, `test_device_comments.py`, `test_device_create.py`, `test_device_decommission.py`, `test_devices.py` (PATCH path), `test_netbox_write.py` integration. Read-endpoint tests (`test_lookup.py`, `test_devices.py` GET, `test_meta.py`) need no changes.

**Implementation order** (each step must complete + tests green before the next; intermediate states leave the suite red):
1. **(a) Dep layer + AuthUser** — add `shift_session_id: UUID | None` field, enrich in `get_current_user`, add dep-layer 409 for write endpoints. Add dep-layer tests (auth tests for the enrichment + 409 paths). No services touched yet; suite stays green because no service reads the new field.
2. **(b) Service updates, one at a time** — `NetBoxWriteService` → `QRLifecycleService` → `DeviceDecommissionService` → `CommentService`. For each: switch the `AuditLogEntry.session_id` source from `user.session_id` (JWT sid path) to the new `user.shift_session_id`; update that service's unit tests' fake `AuthUser` to populate `shift_session_id`. Run full suite after each service to confirm only that service's integration tests are red.
3. **(c) Existing integration test fan-out** — apply the seed-active-shift fixture + update `session_id` assertions across the six integration test files listed above. Suite goes fully green again at end of this step.
4. **(d) End-to-end smoke** — verify a full request flow (`/sessions/start` → device write → `audit_log.session_id == shift_sessions.id` → `/sessions/end`) lands the right value end-to-end. One new integration test pinning the cross-component contract.

Don't reorder. Doing (b) before (a) means services try to read a field that doesn't exist on `AuthUser`. Doing (c) before (b) means the new fixtures seed shifts but services still write JWT sid, so the new assertions fail.

### Task 5 — Acceptance + close-out

**Goal:** Sprint 6 done means tests green, gates clean, work-log + CLAUDE.md + memory + parking-lot updated. Submodule flatten and initial push are already done (Sprint 5 close-out); Task 5 covers normal close-out only. **Work-log entry must explicitly call out**:
- Decision D's `audit_log.session_id` semantic change (pre-Sprint-6 = JWT sid; post-Sprint-6 = shift id).
- Decision J's mobile-side Keycloak revocation contract (backend `/sessions/end` does NOT call Keycloak; mobile owns the revoke).
- Decision E's split for auto-end: mobile owns the 10-min inactivity timer, Sprint 7 backend job is the 12h-orphan fallback.

---

## Working principles (carried from Sprints 1–5)

- **TDD discipline.** Tests first, including failure-mode counterparts. No happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit "go", then code.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are met. The gate is the user's.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–5. `--cov-fail-under=100` gate at close-out.
- **No new dependencies** without explicit approval. Version bumps allowed with justification (recorded in `docs/work-log.md`).
- **Endpoint handler tests:** test handler logic by direct `await`; use `TestClient`/`AsyncClient` only for routing, role-gating, and `response_model` shaping. Same call as Sprints 2-5.
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 6 exercises #4 (DB-enforced state via the partial unique index for the one-active-session invariant) and adds the session_id resolution to the three-record write apparatus from #2.
- **Reuse existing apparatus.** Repository pattern from Sprints 1-4 (`QRCodeRepository`, `AuditLogRepository`); service pattern from `QRLifecycleService` / `DeviceDecommissionService` (defensive `in_transaction()` guard with `RuntimeError`, FastAPI DI factory in the endpoint module). **Don't rewrite the apparatus.**
- **`RuntimeError` over `assert`** for defensive runtime guards (Sprint 1 M3 / Sprint 4 Q2 pattern). Assert only for mypy type narrowing.
- **`-> NoReturn` annotation** on helpers that always raise (Sprint 5 Task 4 polish lesson — surfaces the contract to mypy + future readers).
- **`mypy app/ tests/` at every task close-out**, not just `mypy app/` (Sprint 5 lesson — two pre-existing test-file errors slipped past Tasks 2/3 because only `app/` was checked).

## Reference documents

- `DC_Inventory_ToR_v3.docx` — §4.1.3 (shift start/end UX + 10-min inactivity auto-end), §4.4 (shift session lifecycle), §7.2.4 (shift_sessions schema columns), UC-5 (paper handover register cross-reference for tablet_id)
- `Architecture_Overview.md` §3 (audit_log structure) + §4 (DB-enforced state machine pattern — same pattern as `qr_codes` consistency CHECK + partial unique index)
- `docs/sprint-3.md` — decision C (audit row attribution; JWT `sid` was the original source)
- `docs/sprint-4.md` + `docs/sprint-5.md` — patterns to mirror (service class structure, FastAPI DI factory, defensive guards, three-branch handling where relevant)
- `docs/work-log.md` — Sprint 5 entry's "Sprint 6 candidates" list for context on which deferred items are NOT in this sprint
