# Sprint 3 — Device Read & Update

> **Status:** Planned. Awaiting full task breakdown.
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

_TBD_

### Task 2 — Optimistic-concurrency + three-record write service

_TBD_

### Task 3 — Meta lookup endpoints + in-process caching layer

_TBD_

### Task 4 — Server-driven device-form config

_TBD_

### Task 5 — Device read endpoint

_TBD_

### Task 6 — Device update endpoint

_TBD_

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
