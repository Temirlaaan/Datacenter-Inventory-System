# Sprint 10 — UX Polish + Ops Maturation

> **Status:** Planned. Awaiting go/no-go. Per-task detail layered in inline as we get to each task — same rhythm as Sprints 7–9.
> **Duration target:** 5 working days
> **Goal:** Finish the operational maturity story Sprint 9 started (idempotency-key TTL cron, restore-validation cron) AND make the admin UI feel less Spartan (dashboard activity feed, date-preset chips on filters, bulk retire on batches). No new feature surface — every task is on the "Sprint 10+" deferral list from Sprint 9's close-out.

## Why this sprint exists

Sprint 9 closed with the system production-ready in the strict sense — idempotency contract, device search, backup cron, comments UI. But the close-out's "Sprint 10+ deferrals" list has accumulated through Sprints 7-9 and is now load-bearing for two real concerns:

- **Operational:** `idempotency_keys` grows unbounded without a TTL sweep; `scripts/backup.sh` is wired but never validated end-to-end on a real restore. Both items were always going to land "in Sprint 10 alongside backup operationalisation" — that time is now.
- **UX:** the admin surface is functional but feels Spartan to the user (their words, paraphrased from late-Sprint 8b feedback). Dashboard has counters but no "what's happening right now" view. Audit / sessions filters require manual `datetime-local` typing for "last 24h". Batch detail with 50 FREE codes needs 50 button clicks to retire them.

Sprint 10 ships:

- TTL cleanup cron for `idempotency_keys` (24h sweep alongside `backup.sh`)
- Restore-validation cron: weekly fetch the latest dump, `pg_restore --schema-only` into a scratch postgres, assert success
- Dashboard activity feed (last 20 audit rows) as a server-rendered list below the counter grid
- Date-preset chips on `/web/audit/` + `/web/sessions/` filter forms ("Today", "Last 24h", "Last 7d") — client-side vanilla JS, no new backend
- Bulk retire on `/web/batches/{id}` (multi-select checkboxes on FREE rows + "Retire selected" form)

No new mobile API surface, no new business logic. The four code paths involved (`idempotency_keys` model already exists, `AuditLogRepository.query` already exists, retire service already exists, batch detail template already exists) all extend or wrap what's there.

## Scope boundaries

**In scope — 5 tasks:**

0. **Cleanup + restore-validation crons.** Two new host-cron scripts. `scripts/idempotency_cleanup.sh` runs `docker exec dcinv-db psql -c "DELETE FROM idempotency_keys WHERE created_at < NOW() - INTERVAL '24 hours';"` — pure SQL, idempotent, runs at 03:30 UTC after `backup.sh` at 03:00. `scripts/restore_validate.sh` weekly: download the most recent dump from S3 into a tmpfs, spin up an ephemeral postgres container (`postgres:15`, not the production one), `pg_restore --schema-only`, check exit code, tear down. Touch a marker file on success → new `/health.restore_validation` informational sub-object (same pattern as Task 3's `backups`).

1. **Dashboard activity feed.** Below the existing counter card grid on `/web/`. Last 20 audit rows server-rendered: timestamp / user / operation / entity / result-badge. Same `AuditLogRepository.query` as `/web/audit/` (decision I — direct call, no HTTP self-call). Click any row → `/web/audit/{audit_id}` detail. NO new endpoint, NO real-time / SSE / polling — page-refresh is the refresh model. Read is NOT audited (mirrors `/web/audit/` GET — Sprint 7 decision 8). Single-query, single round-trip alongside `DashboardRepository.snapshot()`.

2. **Date-preset chips on filter forms.** On `/web/audit/` and `/web/sessions/`, add three buttons before each filter form: **Today / Last 24h / Last 7d**. Vanilla `<script>` block at the page bottom; one click sets `from` + `to` `datetime-local` inputs and re-submits. Pure client-side — no backend change. JS is ~15 lines per page, inline (CSP-safe for our setup since we already inline scripts in batches/detail.html for the retire-confirm).

3. **Bulk retire on `/web/batches/{id}`.** Multi-select checkboxes added next to each FREE row in the batch detail table. New `POST /web/batches/{batch_id}/bulk-retire` handler accepts a list of `qr_id` form values, iterates calling `QRLifecycleService.retire` (same logic as the existing single-row retire), aggregates results, returns 303 to the batch detail with a flash like "Retired 12 of 14 — 2 failed (see audit log)". CSRF-protected like every other web POST. NO new API endpoint — this is web-form-only; mobile retires one QR at a time per scan.

4. **Acceptance + close-out.** Work-log entry + CLAUDE.md repository status + parking-lot updates + memory entry. Per-sprint convention.

**Out of scope (Sprint 11+):**

- **Mobile offline-queue implementation** — Sprint 11, separate workstream (needs real Kotlin code or at minimum a contract-tested stub).
- **Real-time SSE/WebSocket dashboard updates** — Sprint 12+ unless an incident forces it. Page-refresh is fine for one admin watching at a time.
- **Cluster-wide rate-limit state (Redis)** — Sprint 8a deferral; still no HA pressure.
- **Phase 2 partial-failure alerting** — carried since Sprint 3.
- **`/web/devices/{id}` write path** — admin web stays read-only unless ToR feedback says otherwise.
- **WAL archiving / point-in-time recovery** — Sprint 11+; daily backups + cleaner restore validation are enough for current RPO.
- **Bulk decommission** — bulk operations on devices touch NetBox individually and have OCC implications. If admins need it after Sprint 10's bulk-retire ships, Sprint 11 picks it up.
- **Bulk-retire mobile endpoint** — explicitly NOT shipped. The mobile UX is "scan one QR, retire one QR"; bulk only makes sense for the admin sweeping a tray of unprinted/damaged stock.

## Cross-cutting decisions

All confirmed at plan stage; per-task detail layered in during execution.

**A. Cleanup crons are bash + host-cron (decision H carried from Sprint 9).** Backup runs on the host; cleanups should too. Same justification: ops scripts must survive an app crash. No FastAPI background job, no Python entry point.

**B. Restore validation uses an ephemeral postgres container, NEVER the production `dcinv-db`.** The whole point is to assert "the dump can be restored cleanly somewhere" — running it against the live db would either overwrite data (bad) or just verify it doesn't error (insufficient). Pull `postgres:15` (matches our dev/prod), `docker run --rm` it on a tmpfs volume, `pg_restore --schema-only` (skip data — fast + we don't need data for schema correctness), check exit code, tear down.

**C. `/health.restore_validation` sub-object mirrors `backups`.** Same shape: `{configured, last_completed_at, age_seconds}`. INFORMATIONAL ONLY — does NOT flip overall status. External monitors alert if `age_seconds > 8 days` (running weekly, anything > 1 week means the last attempt failed).

**D. Activity feed is page-refresh, not streaming.** No new dep on Server-Sent Events or WebSockets. Admins refresh the dashboard when they want fresh data. If a future spec demands real-time, that's a Sprint 12+ scope question.

**E. Activity feed query is ONE additional SELECT alongside the dashboard counter snapshot.** `AuditLogRepository.query(filters=AuditLogQueryFilters(), page=1, page_size=20)` returns the most recent 20 rows. Total dashboard latency: counter-snapshot + activity-feed = 2 SQL round-trips, easily < 50ms on a single-DC load.

**F. Date-preset chips are client-side JS, not new query params.** Server keeps the existing `?from=&to=` flow; the chip just pre-fills the inputs and submits. Vanilla `<button type="button" onclick="setPreset('24h')">` style. CSP-safe — we already inline scripts in `batches/detail.html` (Sprint 9 review LOW 4 follow-up).

**G. Bulk retire iterates the existing `QRLifecycleService.retire` per-QR, not a new bulk SQL update.** Each retire is a three-record write (DB + NetBox + audit) and must keep its atomic semantics. Bulk is "loop over the list and aggregate." Trade-off: 50 retires = 50 NetBox round-trips for BOUND codes (FREE codes skip NetBox per Sprint 4). The expected use-case is admins retiring FREE stock, which is DB-only — fast.

**H. Bulk retire over a FREE-only constraint.** Submitted QR ids that aren't FREE at the moment of processing get an error row in the aggregate result but do NOT block the other items. The batch-detail template already hides the retire button on BOUND/RETIRED rows; this is belt-and-suspenders for race conditions.

**I. Bulk retire endpoint is web-only.** No `POST /api/v1/qr/bulk-retire`. The mobile flow is "scan, retire, repeat"; bulk only makes sense for the admin sweeping unprinted / damaged FREE stock from the office.

**J. Activity feed entries link to `/web/audit/{audit_id}` for the detail page.** Same as the audit list does. No truncation, no expansion-in-place — keeps the dashboard tile simple and the audit page is one click away.

**K. No new Python dependencies.** Activity feed uses existing `AuditLogRepository`. Bulk retire uses existing `QRLifecycleService`. Crons are bash + system `psql` (already on the host for backups) + `docker run postgres:15` for restore-validation.

**L. Cron scripts validated by `shellcheck` only — no pytest.** Same as backup.sh in Sprint 9. The scripts need real S3 + Postgres to run end-to-end; integration scope, deferred to a deploy-time smoke test.

## Task list

Each task gets full Goal / Steps / Acceptance / Anti-criteria / Suggested prompt added during execution. Skeleton names + one-line goals only here.

---

### Task 0 — Cleanup + restore-validation crons

**Goal:** Two new host-cron scripts. `scripts/idempotency_cleanup.sh` deletes `idempotency_keys` rows older than 24h via `docker exec ... psql -c "..."`. `scripts/restore_validate.sh` weekly fetches the latest dump from S3 into tmpfs, spins up an ephemeral `postgres:15` container, runs `pg_restore --schema-only`, touches a marker on success. New informational `/health.restore_validation` sub-object reading the marker mtime — same shape as Sprint 9 Task 3's `backups`. New `docs/cron.md` consolidating all three cron entries (backup, cleanup, restore-validate). 2 pure-unit tests for the `_restore_validation_sub_object` reader (config-unset, marker-mtime-driven).

### Task 1 — Dashboard activity feed

**Goal:** Below the existing counter card grid on `/web/`, render the last 20 audit rows as a server-rendered table. Same shape as `/web/audit/` list rows (timestamp, user, operation, entity, result-badge, "Details" link). Same `AuditLogRepository.query` call via direct Python (decision I — no HTTP self-call). Counter snapshot + activity feed land in one handler call, 2 SQL round-trips total. No real-time / streaming. 2-3 direct-await tests: feed renders, empty-state handled, links to detail page. Unit suite: 620 → ~623.

### Task 2 — Date-preset chips on audit + sessions filter forms

**Goal:** Three buttons (Today / Last 24h / Last 7d) above the filter forms on `/web/audit/` + `/web/sessions/`. One inline `<script>` block per page sets `from` + `to` `datetime-local` inputs and submits the form. Vanilla JS, ~15 lines per page. No backend change. 1 functional test per page asserting the buttons render with the right `data-preset` attributes (the actual click → form-submit chain is JS-only, exercised by manual browser smoke at deploy time).

### Task 3 — Bulk retire on `/web/batches/{id}`

**Goal:** Multi-select `<input type="checkbox" name="qr_ids" value="{{ c.id }}">` on each FREE row of the batch detail table. New `POST /web/batches/{batch_id}/bulk-retire` handler accepts a list of `qr_id` form values, CSRF-protected, iterates calling `QRLifecycleService.retire` per QR, aggregates `{succeeded, failed_by_reason}`, 303-redirects to the batch detail with a flash. NO new API endpoint (decision I). 5+ tests: empty selection, all-success, some-failure mix, CSRF mismatch → 403, no-FREE-codes-selected → flash banner, no work.

### Task 4 — Acceptance + close-out

**Goal:** Sprint 10 done means tests green, lint clean, work-log + CLAUDE.md + memory + parking-lot updated. Sprint 11 deferrals re-anchored: mobile offline-queue (the big next thing), real-time SSE/WebSocket, cluster-wide rate-limit state, WAL archiving, bulk decommission. Push to origin.

---

## Working principles (carried from Sprints 1–9)

- **TDD discipline.** Tests first, including failure-mode counterparts. No happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit "go", then code. Task 3 (bulk retire) in particular has a non-trivial error-aggregation contract worth reviewing pre-implementation.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are met.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–9. `--cov-fail-under=100` gate at close-out.
- **No new dependencies.** Decision K confirms zero new pyproject entries. Crons are bash + host-installed tools (psql via docker exec, postgres:15 via docker run for restore-validation).
- **Endpoint handler tests by direct `await`** for JSON; web pages by direct `await` of the handler function (route-ordering still pinned via the `test_*_route_declared_before_*` family of regression tests added through Sprints 7-9).
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 10 exercises #2 (three-record write — Task 3 bulk retire iterates the existing apparatus, no replacement) and #6 (Keycloak auth — Task 3 reuses CSRF + cookie auth, no new auth surface).
- **Reuse existing apparatus.** All four substantive tasks compose existing repos / services / templates. Zero new domain types.
- **`mypy app/ tests/` at every task close-out**, not just `mypy app/`.
- **Cron scripts validated by inspection + `shellcheck`**, not pytest — they need real S3 + Postgres to run end-to-end and that's integration scope.

## Reference documents

- `DC_Inventory_ToR_v3.docx`:
  - **§4.4.2** — Web Interface (Task 1 + 2 + 3 all extend existing pages)
  - **§5.4.6** — sensitive-read auditing (Task 1's activity feed is a READ, NOT audited; mirrors `/web/audit/` JSON GET)
- `Architecture_Overview.md`:
  - **§3.1** — three-record write (Task 3 bulk retire wraps the existing apparatus per-QR)
  - **§8.1** — destructive migrations (none in this sprint)
- `docs/sprint-9.md` — operational hygiene foundation (Task 0 builds on backup-cron pattern; restore-validation closes the loop)
- `docs/sprint-8b.md` — Sprint 8b's CSRF rollout decisions (Task 3 bulk retire reuses)
- `docs/work-log.md` — Sprint 9 close-out's "Sprint 10+ deferrals" list IS this sprint's task list
- `docs/parking-lot.md` — TTL cleanup + restore-validation entries (resolved by Task 0)
- `docs/backup.md` — operator guide pattern that Task 0's `docs/cron.md` extends
