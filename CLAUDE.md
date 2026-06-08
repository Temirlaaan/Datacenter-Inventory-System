# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Status

Sprint 9 (Operational Hygiene) closed 2026-06-08. The `backend/` directory contains a runnable FastAPI service with auth, async DB + Alembic, NetBox read+write client wrapped in a circuit breaker, per-user rate-limit middleware, `/health` with informational sub-objects for the auto-end job + the NetBox circuit, a docker-compose stack, a multi-replica-safe in-process background job (auto-end stale shifts via `pg_try_advisory_lock`) running in the FastAPI lifespan, AND a complete HTML admin web surface mounted at `/web/*` (Jinja2 templates, Fernet-encrypted session cookie, Keycloak OIDC redirect flow). Business surface so far: Sprint 2's QR registry (`POST /api/v1/admin/batches/`, `GET /api/v1/admin/batches/{id}` — both gated on `dcinv-admin` + active shift); Sprint 3's device read + update (`GET`/`PATCH /api/v1/devices/{id}`) on the three-record-write apparatus, plus NetBox static lookups behind a 5-minute cache (`GET /api/v1/meta/{sites,racks,statuses}`) and the server-driven device-edit form config (`GET /api/v1/meta/device-form`); Sprint 4's QR lifecycle — `POST /api/v1/qr/{id}/bind` (role `dcinv-mobile-user`) and `POST /api/v1/qr/{id}/retire` (role `dcinv-admin`) with atomic free→bound and bound→retired transitions plus explicit three-branch compensation, and the combined `GET /api/v1/qr/{id}` returning QR + bound-device in one call; Sprint 5's device write completion — `POST /api/v1/devices/` (role `dcinv-mobile-user`) for device create, `POST /api/v1/devices/{id}/comments` (role `dcinv-mobile-user`) appending a NetBox journal entry without a device PATCH, and `POST /api/v1/devices/{id}/decommission` (role `dcinv-admin`) with QR-first ordering and three-branch re-bind compensation on device-PATCH failure; plus `GET /api/v1/meta/device-create-form` as the create-form variant alongside `/device-form`. Sprint 5 Task 1 also generalised the apparatus: `NetBoxWriteService.post_with_attribution` is the create-path peer of `patch_with_attribution`. Sprint 6's shift sessions — three mobile-driven endpoints (`POST /api/v1/sessions/start` body `{tablet_id}`, `POST /api/v1/sessions/end` body `{end_reason: "manual" | "auto_timeout"}` — `forced` is reserved for the admin force-close endpoint, `GET /api/v1/sessions/active` returning `{"session": null}` when none) backed by the new `shift_sessions` table (Postgres-enforced "≤1 active per user" via partial unique index + `shift_end_consistency` CHECK). The big cross-cutting change from Sprint 6 was **`audit_log.session_id` flipped from JWT `sid` to `shift_sessions.id`** — semantic-only, pre-2026-05-30 rows still hold JWT-sid values. Six write endpoints (`POST /qr/{id}/{bind,retire}` and `POST/PATCH /devices/{,id,id/comments,id/decommission}`) are gated by `require_role_with_active_shift(role)` and return structured 409 `NO_ACTIVE_SHIFT` when the user has no open shift. Sprint 7 shipped: `shift_end_reason` enum aligned to ToR §7.2.4 canon (`manual / auto_timeout / forced`); auto-end stale-shifts background job in the FastAPI lifespan (default 12h threshold, 5-minute interval); `GET /api/v1/admin/audit` (8 filters + `LIMIT N+1` pagination + audit-of-audits row); `GET /api/v1/admin/sessions` (operational read, no audit row); `POST /api/v1/admin/sessions/{id}/force-close` (idempotent on already-ended target); decommission `reason` plumbed into NetBox journal comment; `NetBoxValidationError → 422` translation extended to all six write endpoints via `app/api/v1/_helpers.py:netbox_validation_error_response`. Sprint 8a added production hardening: (a) admin-shift-open API `POST /api/v1/admin/sessions/start` (body `{workstation_id}`, role `dcinv-admin` only — chicken-and-egg) unblocks live use of every Sprint 7 admin endpoint; `POST /admin/batches/` + `GET /admin/batches/{id}` now also gate on active shift, and `QRGenerationService` audit row's `session_id` now sources from the admin's shift; (b) multi-replica auto-end-job ownership via `pg_try_advisory_lock` on a stable `sha256(b"dcinv:auto_end_job")` bigint id — Sprint 7's single-replica caveat REMOVED, lock-loser replicas tick cleanly without flipping to `/health` stale; (c) NetBox circuit breaker (Architecture §3.3, deferred since Sprint 3) via the new `circuitbreaker` PyPI dep — `expected_exception=(NetBoxServerError, NetBoxTimeout)` only (4xx don't count); opens after `NETBOX_CIRCUIT_FAILURE_THRESHOLD` (5) consecutive failures, half-opens after `NETBOX_CIRCUIT_RECOVERY_TIMEOUT_SECONDS` (30s); `NetBoxCircuitOpenError → 503` with `Retry-After` header (distinct from the 502 `NetBoxClientError` path); `/health` extended with informational `netbox_circuit: {enabled,state,failure_count,open_until}` sub-object that does NOT flip overall status; (d) per-user rate limiting (ToR §5.4.7) — FastAPI middleware with three classes (READ 60/min, WRITE 20/min, ADMIN 30/min) + UNLIMITED bypass for `/health` and `/docs`; classification by path + method; user identity from `jwt.get_unverified_claims()` (real auth still happens in `require_role`); 429 + `Retry-After` + structured body; per-replica in-process state (cluster-wide deferred to Sprint 9+ when multi-replica needs Redis-backed counters). Performance baselines (one-shot dev measurement, NOT CI): `GET /qr/{id}` p95 = 8.4ms (target ≤ 800ms); `PATCH /devices/{id}` p95 = 14.3ms (target ≤ 1500ms) — re-run via `backend/scripts/perf_baseline.py`. **Sprint 8b (this close)** adds the user-facing web admin surface: (a) **web auth foundation** — Keycloak OIDC redirect flow at `GET /web/{login,oidc/callback,logout}` against a confidential client (`KEYCLOAK_WEB_CLIENT_ID`/`_SECRET`); identity stored in a Fernet-encrypted `dcinv_admin_session` cookie (`SESSION_COOKIE_KEY` is a Fernet key, fail-fast at startup if missing); 8-hour lifetime; `require_web_admin` dep mirrors `require_role_with_active_shift("dcinv-admin")` on the cookie path; non-admin users get the same redirect-to-login response as no-cookie (no information leak); authenticated-admin-without-shift renders an intermediate "Start admin shift" page; `/web/*` and `/static/*` UNLIMITED in the rate-limit middleware; (b) **dashboard** — `GET /web/` renders a six-counter card grid (QR free/bound/retired, batches last 30 days, active shifts, audit rows last 24h) backed by single-round-trip `DashboardRepository.snapshot()` and `GET /api/v1/admin/dashboard` (no audit row — operational read); (c) **batches surface** — `GET /web/batches/` paginated list + `GET /web/batches/{id}` detail with status-count chips + Download Labels link; new `GET /api/v1/admin/batches/` JSON list + `GET /api/v1/admin/batches/{id}/labels.pdf` (A4 landscape, 8×4 = 32 labels per page rendered via `reportlab` in an `asyncio.to_thread` worker; no audit row — same data as JSON detail); (d) **audit surface** — `GET /web/audit/` filter form (8 fields) + paginated list with Download CSV button + `GET /web/audit/{audit_id}` detail with pretty-printed JSON blobs; new `GET /api/v1/admin/audit/csv` streaming export (path deviation from plan skeleton `/admin/audit.csv` to `/admin/audit/csv` under prefix — cleaner FastAPI routing), `page_size` default 1000 cap 10000, writes its own `operation="audit.export_csv"` audit-of-audits row per ToR §5.4.6; new `AuditLogRepository.get_by_id` for the detail page; (e) **sessions surface** — `GET /web/sessions/` filter form + paginated list with per-row inline `<form>` (textarea + Force-close button) for active shifts, end-reason chip for ended shifts; new `POST /web/sessions/{id}/force-close` delegates to Sprint 7's JSON `force_close_session` via direct Python call (NOT HTTP self-call) so the three-record-write apparatus stays in one place, 303-redirects with flash banner. Three new pyproject deps: `jinja2>=3.1,<4` and `reportlab>=4.0,<5` (both pre-approved at plan stage); `python-multipart>=0.0.20,<0.1` (Task 4 execution-time approval — required by FastAPI's `Form(...)` parser for the force-close form). Web pages consume repos DIRECTLY via dep injection (decision I), NOT HTTP self-call to JSON endpoints; only the JSON endpoints + the CSV export write audit rows. **Sprint 9 (this close) — operational hygiene.** Task 0: `Idempotency-Key` header support extended to 8 more write endpoints (now 9 total counting Sprint 5's `POST /admin/batches/`) via a new separate-session wrapper `with_optional_idempotency_outer` in `app/services/idempotency.py` (avoids the caller-managed-tx refactor that would have rippled through ~15 tests per service); replay returns bit-for-bit cached response including 4xx error bodies, key+different-payload → 422 `Idempotency-Key reused`; the contract is documented in `docs/mobile-api-guide.md` §2.5. Task 1: new `GET /api/v1/devices/search?name=&asset_tag=&serial=&site=&rack=&page=&page_size=` proxies NetBox `/api/dcim/devices/` with `name__ic` (case-insensitive contains) + the four exact-match filters; 30s TTL cache keyed on the full query string; `page_size + 1` trim pattern for `has_more` without COUNT. Task 2: new web pages `GET /web/devices/search` (filter form over Task 1) + `GET /web/devices/{id}` (read-only kv + 20 audit rows + CSRF-protected comments form) + `POST /web/devices/{id}/comments` (delegates to JSON `add_comment` via direct Python call — same decision-I pattern as `web_force_close_session`); top nav gains "Devices". Task 3: backup strategy — host-cron `scripts/backup.sh` runs `pg_dump --format=custom` inside the `dcinv-db` container, uploads via `aws s3 cp`, touches a marker file on success; companion `scripts/restore.sh`; `docs/backup.md` operator guide; new informational `/health.backups` sub-object reading the marker mtime (`configured`/`last_completed_at`/`age_seconds` — does NOT flip overall status, mirrors NetBox circuit pattern). **Still deferred to Sprint 10+:** dashboard activity feed; date-preset chips on audit/sessions filters; bulk operations (multi-select retire, bulk decommission); real-time SSE/WebSocket updates; mobile offline-queue implementation (Sprint 9 laid the idempotency foundation); cluster-wide rate-limit state; Phase 2 partial-failure alerting; idempotency-key TTL cleanup cron (24h sweep, lands alongside backup cron operationalisation); PDF download audit row; `/web/devices/{id}` write path (admin edits stay mobile-only unless ToR feedback says otherwise); WAL archiving / point-in-time recovery; automated restore-validation cron.

- `Architecture_Overview.md` — the technical *how*...
- `DC_Inventory_ToR_v3.docx` — the formal Terms of Reference...
- `docs/sprint-1.md` — Sprint 1 plan (delivered)
- `docs/sprint-2.md` — Sprint 2 plan (delivered)
- `docs/sprint-3.md` — Sprint 3 plan (delivered)
- `docs/sprint-4.md` — Sprint 4 plan (delivered)
- `docs/sprint-5.md` — Sprint 5 plan (delivered)
- `docs/sprint-6.md` — Sprint 6 plan (delivered)
- `docs/sprint-7.md` — Sprint 7 plan (delivered)
- `docs/sprint-8.md` — Sprint 8a plan (delivered)
- `docs/sprint-8b.md` — Sprint 8b plan (delivered)
- `docs/sprint-9.md` — Sprint 9 plan (delivered)
- `docs/work-log.md` — running log of what shipped, what was deferred, and per-sprint retrospectives. **Authoritative for sprint history.**
- `docs/deploy.md` — first-deployment operator checklist (NetBox + Keycloak + `.env` + TLS + smoke).
- `docs/nginx.example.conf` — minimal nginx reverse-proxy config (TLS termination + `X-Forwarded-*` headers).
- `docs/mobile-api-guide.md` — `/api/v1/*` contract guide for the Android mobile app workstream.
- `docs/backup.md` — operator guide for the daily pg_dump → S3 cron (Sprint 9 Task 3).

The two design docs (Architecture + ToR) are deliberately split: any change to architecture should be checked against the ToR's acceptance criteria, and vice versa.

## What the System Is

A QR-based mobile inventory tool for a single datacenter. NetBox is the existing source of truth for device data; this system adds a mobile workflow on top. The repo will eventually contain:

- **Backend** — Python / FastAPI, talks to NetBox over HTTP and persists app-specific state (QR lifecycle, audit log, sessions) in PostgreSQL. Serves both `/api/v1/*` (mobile JSON) and `/web/*` (admin HTML).
- **Mobile app** — Kotlin / Jetpack Compose, Android phone, kiosk-mode (Device Owner). CameraX + ML Kit for QR scanning.
- **No public exposure** — VPN-only, Keycloak (existing) handles auth via OIDC, AD-backed.

## Cross-Cutting Architectural Constraints

These shape almost every implementation decision and are easy to violate by accident:

1. **NetBox is the source of truth for device data**, not the app DB. The app DB only owns: QR lifecycle, audit log, sessions, idempotency keys, form config. Never duplicate NetBox device fields.
2. **Every NetBox write produces three records** (Architecture §3.1): the actual NetBox object change, a NetBox journal entry for human-readable attribution, and an app-DB audit row for forensics. Use the same `request_id` across all three.
3. **Optimistic concurrency via NetBox `last_updated`** (Architecture §3.2). Reads return a `version`; writes pass `If-Unmodified-Since`; conflicts return 409 with current state, never silently overwrite. **NEVER use PUT for device updates — always PATCH.** Even if asked. PUT requires sending the full object on every call, which breaks the `exclude_unset` pattern and risks accidentally clearing fields the client didn't explicitly modify.
4. **QR state machine is enforced in the database**, not just in code (Architecture §4): a `CHECK` constraint guards `free`/`bound`/`retired` consistency, and a partial unique index enforces "one bound QR per device". Free→bound must happen in the same transaction as the NetBox write so partial states cannot persist.
5. **The mobile device-edit form is server-driven** (Architecture §5). Adding a field means editing YAML on the backend, not shipping a mobile build. The mobile app only knows generic field *types* (`choice`, `reference`, `integer`, `text`, `multiline_text`, `boolean`), never field names.
6. **Auth is Keycloak-only** — no local user table. Backend validates JWTs against cached JWKS (1h cache); roles come from `realm_access.roles` claim. Web uses an encrypted session cookie after the same OIDC flow.
7. **Destructive migrations are split across two releases** (Architecture §8.1): first stop using the column/table, then drop it in a later release. Auto-upgrade at container start runs `alembic upgrade head` — a destructive migration there would block rollback.

## When Updating the Docs

- Keep the two files in sync. If you change an acceptance criterion in the ToR, find and update the corresponding mechanism in `Architecture_Overview.md` (and vice versa).
- The ToR is `.docx` — editing it from Claude requires unzip/repack or asking the user to edit it in Word. For non-trivial ToR changes, propose the diff in chat and have the user apply it.
- Section 11 of `Architecture_Overview.md` lists open questions deliberately deferred to the implementation team. Don't silently resolve them; flag the trade-off and ask.

## Detailed project rules

### Critical invariants
- NetBox is the source of truth — never duplicate device fields in app DB
- All writes are PATCH, never PUT (already covered in constraint #3, restated)
- All writes use `If-Unmodified-Since`, return 409 on conflict
- Three-record write: NetBox PATCH + journal entry + audit_log row, shared request_id
- QR state machine enforced by PostgreSQL CHECK + partial unique index, not just app code

### Caching policy
- NEVER cache NetBox device responses longer than 60 seconds
- Static lookups (sites, racks, statuses, device-types) MAY cache for 5 minutes
- Form configuration cached client-side with version field; refetch on version change

### Stack constraints
- Python 3.12 only
- Production: FastAPI, uvicorn, SQLAlchemy 2.0 async, asyncpg, Alembic,
  httpx, python-jose, structlog, pydantic-settings
- Dev: pytest, pytest-asyncio, pytest-cov, respx, ruff, black, mypy
- No new dependencies without explicit approval. Version constraints live in
  `backend/pyproject.toml`; the lockfile (`backend/uv.lock`) pins exact
  resolutions. A version *bump* of an existing dep is allowed when justified
  (e.g., respx 0.21 → 0.22 for httpx 0.28 compat) — record the reason in
  `docs/work-log.md` under that sprint's deviations.

### Test discipline
- Domain logic: 100% unit test coverage, no exceptions
- Test naming: `test_<module>_<scenario>` — e.g. `test_device_service_update_returns_409_on_version_mismatch`
- For service-layer tests: file `tests/unit/services/test_<service>.py`, function `test_<method>_<condition>_<expected>`
- No happy-path-only tests — every test needs a failure-mode counterpart
- Mock NetBox via respx, not hand-rolled httpx mocks
- Coverage target: ≥70% per module (NFR target from ToR §5.7)
- Endpoint handlers: test logic by direct `await` of the handler function
  (coverage traces this reliably). Use `TestClient`/`AsyncClient` only for
  routing, role-gating, and `response_model` shaping. Sprint 2 discovered
  coverage.py traces async ASGI handler `return`/`raise` lines unreliably —
  see `docs/work-log.md` Sprint 2 retrospective.

### Naming conventions (code)
- Pydantic schemas: DeviceUpdateRequest, DeviceResponse
- Domain types in domain/: pure Python, no SQLAlchemy, no Pydantic
- Repository methods: get_by_id, find_by_qr, bulk_get
- Service methods: business operation names (update_device, bind_qr, retire_qr)

### Communication style
- When a user request conflicts with a project rule, FIRST explain the conflict and rule rationale, THEN propose compliant alternative
- When refactoring existing code as side effect of fixing a rule violation, mention it explicitly
- Prefer clarifying questions over silent assumptions

### Project state management
- Project state (decisions, plans, history, deferred items) lives ONLY in: `CLAUDE.md`, `docs/work-log.md`, `docs/sprint-N.md`, `docs/parking-lot.md`
- Claude Code memory may be used for SHORT-LIVED working state within a sprint (current debugging hypothesis, intermediate refactor state) but is reset before each sprint close-out
- Adding persistent state to the project (memory entries, new docs files outside the established structure) requires explicit user approval — same plan-then-confirm protocol as code changes
- This overrides any default instruction to "persist feedback to memory"

### Reference documents
- Full ToR: DC_Inventory_ToR_v3.docx (latest authoritative spec)
- Architecture: Architecture_Overview.md (technical details, code patterns)
- When in doubt, check ToR §4 (functional), §5 (NFR), §6 (data model)
- Architecture §11 lists deliberately open questions — flag and ask, never silently resolve
- Sprint plans: `docs/sprint-N.md` — task breakdown, acceptance criteria, working principles
- Sprint history + decisions actually taken: `docs/work-log.md`