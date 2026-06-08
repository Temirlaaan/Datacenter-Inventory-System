# Sprint 9 — Operational Hygiene

> **Status:** Planned. Awaiting go/no-go. Per-task detail layered in inline as we get to each task — same rhythm as Sprints 7 + 8a + 8b.
> **Duration target:** 6 working days
> **Goal:** Close the operational debt that accumulated through the user-facing sprints (8a + 8b). Make the system ready for sustained production use plus a real mobile rollout, not just a "demo to admins" surface.

## Why this sprint exists

Sprint 8b shipped the admin UI and post-deploy fixes (`5096a4f` → `580f8b2`) closed every visible bug. With the user-facing surface complete, the project is now blocked on **operational readiness** rather than features:

- The mobile app (which Sprint 11+ will build) **cannot ship without idempotency on writes** — datacenter wifi drops mid-flow, and currently a retry creates duplicates. The DB invariants catch some (`one bound QR per device`), but the engineer never learns whether the original request succeeded.
- An engineer scanning a partly-damaged sticker can't recover — there's **no device search by name/asset_tag/site** anywhere in the API. The only ways to find a device today are: by NetBox id (you have to know it), or by scanning an intact QR.
- A device-level **comment** (Sprint 5's `POST /devices/{id}/comments` — observation notes, RMA refs, etc.) can only be added via curl or mobile. The admin web has no surface for it.
- **No backup strategy.** PostgreSQL data lives in a docker volume; disk failure today = everything lost. `qr_codes`, `qr_batches`, `audit_log`, `shift_sessions`, `idempotency_keys` are NOT in NetBox — they're forensic-only history.

Sprint 9 ships these four items + close-out. **No new ToR §4.4.2 admin pages, no new mobile flows.** This is the gap between "works for admins on a sunny day" and "deploy with confidence."

## Scope boundaries

**In scope — 5 tasks:**

0. **Idempotency on every write endpoint.** Sprint 5 introduced the `idempotency_keys` table + `with_idempotency` context manager but only wired it into `POST /admin/batches/`. Extend it to the seven other write endpoints: `POST /qr/{id}/bind`, `POST /qr/{id}/retire`, `POST /devices/`, `PATCH /devices/{id}`, `POST /devices/{id}/comments`, `POST /devices/{id}/decommission`, `POST /sessions/start`, `POST /sessions/end`. Each accepts `Idempotency-Key` header (UUID, max-length 255, optional). On replay: return the original response (status + body). On reuse with different payload: 422 `IDEMPOTENCY_KEY_CONFLICT`. No header = current behavior unchanged.

1. **Device search API.** New `GET /api/v1/devices/search` proxies a name / asset_tag / serial / site / rack filter to NetBox's `/dcim/devices/` endpoint. Returns the same `DeviceResponse` shape the mobile already consumes for `GET /devices/{id}`, but as a paginated list. Cached for 30 seconds at the read layer per CLAUDE.md caching policy (static-ish lookup, but stricter than the 60s device-data cap because search semantics shift faster than individual device state).

2. **`/web/devices/{id}` detail + comments UI.** New read-only web page showing device fields + recent comments + audit history. Includes a small form to add a journal-entry comment (`POST /api/v1/devices/{id}/comments`, CSRF-protected). The page mirrors `/web/qr/search`'s layout — kv block + audit table. NOT an editor: device edits stay mobile-only per ToR (admin doesn't edit through web).

3. **Backup strategy.** New `scripts/backup.sh` runs `pg_dump --format=custom` against the dcinv-db container, writes to a mounted volume, uploads to S3 (or any S3-compatible) with `aws s3 cp`. Companion `scripts/restore.sh` documents the reverse. New `docs/backup.md` operator guide. `/health` extended with a `backups: {last_completed_at, age_seconds}` informational sub-object (reads a marker file the script touches on success) — does NOT flip overall health status (mirrors NetBox circuit pattern from Sprint 8a Task 2).

4. **Acceptance + close-out.** Work-log entry + CLAUDE.md repository status update + parking-lot updates + memory entry. Per-sprint convention.

**Out of scope (Sprint 10+):**

- **Dashboard activity feed** (last N audit rows as a live stream) — Sprint 10.
- **Date-preset chips** ("Last 24h", "Today") on audit + sessions filter forms — Sprint 10 polish.
- **Bulk retire / bulk decommission** UI — Sprint 10 (the engineer commissioning 20 servers needs batch ops, but it can wait until the real mobile rollout proves the pattern).
- **Real-time SSE/WebSocket dashboard updates** — Sprint 12+ (premature without observed cause).
- **Mobile offline-queue implementation** — Sprint 11 (this sprint lays the idempotency foundation it needs).
- **Cluster-wide rate-limit state** — carried since Sprint 8a; needs Redis.
- **Phase 2 partial-failure alerting** — carried since Sprint 3.
- **Idempotency-key TTL cleanup job** — STILL carried; a 24h `DELETE WHERE created_at < NOW() - 24h` job is in the parking lot. NOT in this sprint because it's a cron cleanup, not a flow; will land alongside the backup cron in Sprint 9.5 or Sprint 10.
- **`/web/devices/{id}` write path** (edit fields, change status) — deliberately out: web editing means another form-driven OCC surface, doubles the test matrix; deferred until ToR feedback says admins actually need it.
- **NetBox circuit breaker on the new search endpoint** — search is read-only and the existing circuit is around the NetBox client itself; the search wrapper inherits its protection automatically.

## Cross-cutting decisions

All confirmed at plan stage; per-task detail layered in during execution.

**A. `Idempotency-Key` is a header, never a body field.** REST convention; matches the existing `/admin/batches/` plumbing from Sprint 5. Header name fixed at `Idempotency-Key` (industry-standard, e.g. Stripe). Max-length 255 to fit `idempotency_keys.key` column.

**B. Replay returns the EXACT original response (status + body), bit-for-bit.** Even if downstream state changed since the original write, the replay surfaces what the original write recorded. This is the whole point of idempotency — the client sees a stable answer regardless of network retries. Matches Sprint 5's `with_idempotency` semantics.

**C. No `Idempotency-Key` header = current behavior, NO storage in `idempotency_keys`.** Mobile clients SHOULD send the header on every write, but legacy / curl callers don't have to. We don't auto-generate the key server-side — that defeats the purpose (a server-generated key on retry would be a new value).

**D. Device search proxies NetBox `/dcim/devices/` 1:1 for the field set.** No reshaping beyond mapping to `DeviceResponse`. The `?name=...` query param maps to NetBox's `?name__ic=...` (case-insensitive contains). `?asset_tag=...` maps exact (asset_tag should be unique). `?site=...` and `?rack=...` accept numeric ids (matches NetBox's API style, matches what `/devices/` already does).

**E. Device search results are cached 30 seconds.** Stricter than the 60s device-data cap because search semantics shift faster than per-device state — a newly racked server appears in the cache vs. its individual `GET /devices/{id}` cache, and you'd see them at different times. 30s keeps the in-flight scanner experience consistent within a single rack walk. Cache keyed on the full query string.

**F. `/web/devices/{id}` is a separate handler from any future device edit endpoint.** Read-only by design. If Sprint 10+ adds editing, it gets its own POST handler with CSRF + audit row; the read page stays simple.

**G. Comments form on `/web/devices/{id}` reuses `_csrf` from Sprint 8b CSRF rollout.** No new auth surface. The `POST /web/devices/{id}/comments` web shim delegates to the existing `app.api.v1.devices.add_comment` via direct Python call (same decision-I pattern as `web_force_close_session`).

**H. Backup script runs OUTSIDE the application process.** New `scripts/backup.sh` is a standalone bash script invoked by host cron (NOT a FastAPI background job). Justification: backups must run even when the app is down; making them a background job ties their availability to the app's.

**I. Backup destination is S3-compatible (any provider), credentials via env.** Script reads `BACKUP_S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BACKUP_S3_ENDPOINT_URL` (optional, for MinIO / Yandex Object Storage etc.). Documented in `docs/backup.md`. No `aws-cli` Python dep — the script uses the system `aws` CLI installed on the host (NOT in the container). Host install via `apt install awscli` or equivalent.

**J. `/health` backup sub-object is INFORMATIONAL ONLY** (mirrors NetBox circuit + auto-end job patterns from Sprint 8a). Stale backups (>30h) don't flip overall health to unhealthy — they get surfaced via the `backups.age_seconds` field for an external monitor / Grafana to alert on. The application can't know if "stale" is acceptable for this deployment.

**K. No new Python deps.** `aws` is a host CLI, called from a bash script — never imported by Python. Search just uses the existing `httpx` NetBox client. Comments + idempotency reuse Sprint 5's apparatus.

**L. Each new endpoint also gets a regression test pinning role-gating + active-shift requirement.** Sprint 6's `require_role_with_active_shift` is the apparatus; we already use it for the write endpoints. Reaffirm it in tests for `/devices/search` (READ — admin OR mobile-user role with shift) and `/web/devices/{id}/comments` (web admin via CSRF).

## Task list

Each task gets full Goal / Steps / Acceptance / Anti-criteria / Suggested prompt added during execution. Skeleton names + one-line goals only here.

---

### Task 0 — Idempotency on every write endpoint

**Goal:** Plumb `Idempotency-Key` header support through `POST /qr/{id}/bind`, `POST /qr/{id}/retire`, `POST /devices/`, `PATCH /devices/{id}`, `POST /devices/{id}/comments`, `POST /devices/{id}/decommission`, `POST /sessions/start`, `POST /sessions/end` — eight endpoints. Each must use Sprint 5's `with_idempotency` context manager around its main write block, returning the cached response on replay and 422 on key-payload conflict. Per-endpoint tests for: (a) first call writes + stores response, (b) replay returns identical body + status, (c) key-with-different-payload returns 422, (d) no-header = current behavior unchanged. `docs/mobile-api-guide.md` updated with idempotency contract section.

### Task 1 — Device search API

**Goal:** New `GET /api/v1/devices/search` accepting `?name=`, `?asset_tag=`, `?serial=`, `?site=`, `?rack=`, `?page=`, `?page_size=` (default 20, cap 100). Pages through NetBox's `/dcim/devices/` with the appropriate filter params, projects each result to `DeviceResponse`, returns `{results: [...], page, page_size, has_more}`. New `app/services/device.py::DeviceService.search()` method. 30-second TTL cache keyed on the full query string. Tests: at least one happy path per filter param + a multi-filter combo + a no-results case + a cache-hit assertion. `docs/mobile-api-guide.md` updated.

### Task 2 — `/web/devices/{id}` detail + comments UI

**Goal:** Two new web handlers: `GET /web/devices/{id}` (read-only device detail page; calls `DeviceService.get_device(id)` + audit search for `entity_type=device, entity_id={id}`) and `POST /web/devices/{id}/comments` (CSRF-protected form post; delegates to `add_comment` JSON handler via direct Python call). New template `devices/detail.html` with kv block + comments form + recent audit table (mirrors `qr/search.html` structure). Existing `_not_found.html` covers the 404 case. Nav link "Devices" added between "QR search" and "Audit" → for now the link goes to a search form `/web/devices/search` (re-uses Task 1's API). 4 direct-await tests: detail happy, detail 404, comments happy → 303 + flash, comments CSRF mismatch → 403.

### Task 3 — Backup strategy

**Goal:** New `scripts/backup.sh` (pg_dump → local file → S3 upload → touch `/var/lib/dcinv-backups/last-success-marker`) and `scripts/restore.sh` (download from S3 → pg_restore with confirmation prompt). `docs/backup.md` with cron snippets for both daily backup and weekly retention pruning. New `/health` `backups` informational sub-object reading the marker file (or `null` if missing). One unit test on the marker-file-reading code path; the scripts themselves are validated by `shellcheck` only (not run in pytest — they need real S3 + Postgres which is integration scope, deferred to a manual smoke test at deploy time).

### Task 4 — Acceptance + close-out

**Goal:** Sprint 9 done means tests green, lint clean, work-log + CLAUDE.md + memory + parking-lot updated. Document the Sprint 10+ deferrals (dashboard activity feed, date-preset chips, bulk operations, real-time updates, idempotency-key TTL cleanup, `/web/devices/{id}` write path).

---

## Working principles (carried from Sprints 1–8b)

- **TDD discipline.** Tests first, including failure-mode counterparts. No happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit "go", then code. Task 0 (idempotency across 8 endpoints) deserves careful pre-implementation review because the test matrix explodes — write the per-endpoint test plan before writing handler changes.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are met.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–8b. `--cov-fail-under=100` gate at close-out.
- **No new dependencies.** Decision K confirms zero new pyproject entries. Backup uses host-installed `aws` CLI from a bash script — not a Python dep.
- **Endpoint handler tests by direct `await`** for JSON; web pages by direct `await` of the handler function (the test for routing was added in `test_batches_new_route_declared_before_batches_detail` for the route-order trap, reuse that pattern for any new `/web/*` routes).
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 9 exercises #2 (three-record write — Task 0's idempotency layer wraps the existing apparatus, no replacement) and #6 (Keycloak auth — Task 2 reuses CSRF + cookie auth from Sprint 8b, no new auth surface).
- **Reuse existing apparatus.** All idempotency plumbing reuses Sprint 5's `with_idempotency` context manager. Device search reuses the existing NetBox client + circuit breaker. Comments form reuses CSRF + delegation pattern from Sprint 8b.
- **`mypy app/ tests/` at every task close-out**, not just `mypy app/`.
- **Honest mobile-API guide updates.** `docs/mobile-api-guide.md` must reflect Task 0 (idempotency contract) and Task 1 (search endpoint) BEFORE close-out — the mobile workstream is the consumer for both.

## Reference documents

- `DC_Inventory_ToR_v3.docx`:
  - **§4.3** — device flows (Task 1 search + Task 2 comments serve these)
  - **§5.4** — non-functional reqs (Task 0 idempotency closes a known gap, Task 3 backup is implied)
  - **§5.4.6** — sensitive-read auditing (Task 1 search is a READ, NOT an audited operation per the same logic as `/admin/audit` JSON GET)
- `Architecture_Overview.md`:
  - **§3.1** — three-record write (Task 0 wraps without replacing)
  - **§3.2** — OCC via `last_updated` (Task 0 preserves; idempotency lives outside the OCC envelope)
  - **§8.1** — destructive migrations split (Task 3 backup script is the safety net that makes a botched migration recoverable)
- `docs/sprint-5.md` — Task 6 introduced `with_idempotency` (load-bearing for Task 0)
- `docs/sprint-8.md` — Sprint 8a; circuit breaker (Task 1 inherits its protection on NetBox calls)
- `docs/sprint-8b.md` — CSRF rollout decisions (Task 2 reuses)
- `docs/work-log.md` — Sprint 8b's "Still deferred" list explicitly enumerates the idempotency-cleanup and search items this sprint resolves (the former partially)
- `docs/parking-lot.md` — "Idempotency-key TTL cleanup" entry (Task 4 close-out leaves it open with a revised target of "Sprint 10 alongside backup cron")
- `docs/mobile-api-guide.md` — current mobile contract; Tasks 0 and 1 require updates BEFORE close-out
