# Work log

A running log of what was done, what was learned, and what was deliberately deferred.
Per-sprint retrospectives accumulate here. Sprint plans live in their own files
(`sprint-N.md`); this log records what actually happened.

---

## Sprint 1 — Foundation (closed 2026-05-14)

**Status:** Closed. Tasks 1–7 complete. Task 8 deferred (see below).

### What shipped

| Task | Deliverable |
|---|---|
| 1 | Project skeleton under `backend/`: uv, ruff, black, mypy, pytest, Architecture §7.1 layout |
| 2 | `app/config.py` (Settings + secrets dir), `app/observability/logging.py` (structlog JSON), request-id middleware |
| 3 | `app/db/session.py` (async engine + sessionmaker + dep), Alembic with async `env.py`, empty initial migration |
| 4 | `app/auth/jwks.py` (TTL + kid-rotation cache, lock-protected), `app/auth/dependencies.py` (`AuthUser`, `get_current_user`, `require_role`) |
| 5 | `app/netbox/client.py` (read-only, retry/backoff, request-id propagation), minimal pydantic models |
| 6 | `GET /health` (`app/api/v1/health.py`) — concurrent DB / NetBox / Keycloak checks with 2s per-check timeout |
| 7 | Multi-stage Dockerfile, `docker-compose.yml`, `.env.example`, entrypoint runs `alembic upgrade head` then uvicorn |

### Quality bar at close

- **91 tests** (unit + integration), **100% line + branch coverage** across `app/`
- ruff + black + mypy clean
- Stack verified: `docker compose up -d --build` → `/health` responds (with categorical detail strings) → `docker compose down -v` clean

### Pyproject deviations from the CLAUDE.md baseline

All approved by user during the sprint:

1. `respx` constraint bumped `>=0.21,<0.22` → `>=0.22,<0.24`. respx 0.21 has a bytes-vs-str method-matching bug under httpx 0.28 that broke every respx-based test. 0.22+ fixes it.
2. ruff `ignore` adds `B008`. FastAPI's `Depends()` in default args is the prescribed framework pattern; B008 was firing on every dependency.
3. mypy override: `module = "jose.*"` treated as untyped. python-jose has no first-party type stubs and the third-party `types-python-jose` is incomplete.

### Architectural decisions worth carrying forward

- **JWT audience NOT verified.** Keycloak access tokens carry `aud=account` by default, which is meaningless. Issuer + signature + exp are what actually protect us. Documented in `app/auth/dependencies.py`.
- **JWKS lazy fetch, not eager startup.** Boot stays independent of Keycloak; `/health` is the misconfig signal. See `app/auth/jwks.py:get_jwks_cache`.
- **`/health` mounted at root**, not `/api/v1/...`. Orchestrators expect unversioned probes. Code lives in `app/api/v1/health.py` (file layout) but the route is `GET /health`.
- **Three lru-cached singletons** (`get_engine`, `get_sessionmaker`, `get_netbox_client`, `get_jwks_cache`) bind to whichever event loop first uses them. `tests/conftest.py:clean_env` clears them between tests; `tests/integration/test_db.py` and `tests/integration/test_health.py` clear them explicitly because they need fresh engines per test loop. **Watch for this in Sprint 2** when more singletons land.
- **NetBox client is thin.** Returns raw `httpx.Response`; parsing into pydantic models happens at the call site. Lets us add typed wrappers per use case in Sprint 2 without refactoring the client.
- **Health checks bypass the NetBox client's retry loop.** A 2s budget can't survive 3 retries with backoff. Each `_check_*` opens its own short-timeout `httpx.AsyncClient`.

### Sprint 1 retrospective

**What went well:**
- TDD discipline held end-to-end. Every module landed with tests first, and the 100% coverage gate caught real gaps (the post-lock recheck branch in `JWKSCache`, the `_open_session` wrapper in `_check_db`).
- The plan-then-confirm rhythm (write up the approach, get explicit approval, then code) caught two design ambiguities before they hit code: lazy vs eager JWKS fetch (Task 4), test location for the NetBox client (Task 5).
- The respx 0.21/httpx 0.28 incompat surfaced fast and was resolved cleanly by bumping a single constraint, not by adding a new dep.
- Code review at sprint close caught real issues (dead `JWKSEndpointState` class, latent event-loop binding on `get_netbox_client`, raw exception messages leaking through unauthenticated `/health`) — the fixes added 3 small tests and tightened the security surface.

**What slowed us down:**
- The IDE's wrong Python interpreter path (`/usr/lib/python3` instead of `.venv/`) generated a constant stream of false "module not found" diagnostics on every edit. Real but harmless; flagging here so future contributors don't chase it.
- `docker compose up -d` reuses cached images even after a fresh `docker build` — needed `--build` to actually pick up new code. Worth noting in any future Sprint that touches the Dockerfile.

**Deliberately deferred:**
- **Task 8 — GitHub Actions CI.** The repo isn't a git repo yet (no `.git/`), and there's no GitHub remote. Task 8 is **deferred until the repo is published to GitHub**. The CI workflow will need to:
  - Run `docker compose -f docker-compose.test.yml up -d` for Postgres
  - Set `DATABASE_URL`, `NETBOX_URL`, `NETBOX_SERVICE_TOKEN`, `KEYCLOAK_BASE_URL`
  - Run `uv run ruff check`, `uv run black --check`, `uv run mypy`, `uv run pytest --cov=app --cov-fail-under=70` (raise to 100 once the team agrees)
  - Build the Docker image as a final step
- **Manual verification with real Keycloak / NetBox.** No live instances available in this environment.
- **Liveness vs readiness split** (`/healthz/live`). Add when k8s deployment requires it; compose doesn't need it.
- **Caching layer for static NetBox lookups.** Design note captured in `docs/sprint-1.md` Task 5.
- **Local review code-review LOW items:**
  - L2 (refactor convoluted "invalid signature" test) — acceptable as is
  - L3 (`/health` return type → TypedDict + response model) — would gain OpenAPI accuracy; defer to whenever we tighten the OpenAPI surface

### Files added in Sprint 1 (high-level)

- `backend/pyproject.toml`, `backend/uv.lock`
- `backend/app/{__init__,main,config}.py`
- `backend/app/api/__init__.py`, `backend/app/api/v1/{__init__,health}.py`
- `backend/app/auth/{__init__,jwks,dependencies}.py`
- `backend/app/db/{__init__,models,session}.py`
- `backend/app/netbox/{__init__,client,models,errors}.py`
- `backend/app/observability/{__init__,logging}.py`
- `backend/app/{domain,services,web}/__init__.py`
- `backend/alembic/{env.py,README.md,script.py.mako}`, `backend/alembic/versions/068437e38dd9_initial_empty.py`, `backend/alembic.ini`
- `backend/tests/{__init__,conftest}.py`
- `backend/tests/unit/...` (smoke, config, logging, request_id middleware, db_session, auth/, netbox/, api/v1/)
- `backend/tests/integration/{conftest,test_db,test_health}.py`
- `backend/Dockerfile`, `backend/docker-compose.yml`, `backend/docker-compose.test.yml`, `backend/.env.example`, `backend/.dockerignore`, `backend/scripts/entrypoint.sh`

### How to run locally (close-of-sprint snapshot)

```bash
cd backend
cp .env.example .env   # fill in real values
docker compose up -d --build
curl localhost:8000/health
docker compose down -v
```

Tests:
```bash
docker compose -f docker-compose.test.yml up -d
DATABASE_URL=postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test \
  NETBOX_URL=https://x NETBOX_SERVICE_TOKEN=x KEYCLOAK_BASE_URL=https://x \
  uv run pytest --cov=app
```

---

## Sprint 2 — QR Registry: Generation + Lookup (closed 2026-05-17)

**Status:** Closed. Tasks 1–8 complete.

### What shipped

| Task | Deliverable |
|---|---|
| 1 | `app/domain/qr.py` — `QRStatus`, frozen `QR`/`QRBatch` dataclasses, `bind`/`retire` transition methods, `IllegalQRTransition`; `__post_init__` mirrors the DB CHECK |
| 2 | Migration `a1b2c3d4e5f6` — `qr_batches`, `qr_codes`, `audit_log` with the `qr_state_consistency` CHECK + `qr_one_per_device` partial unique index; SQLAlchemy models; `app/db/models.py` became the `app/db/models/` package |
| 3 | `app/services/qr/token.py` — DCQR token generator (`secrets.choice`, 32-char alphabet) with collision-retry `generate_unique_token` |
| 4 | `app/db/repositories/` — `QRCodeRepository`, `QRBatchRepository`, `AuditLogRepository`; `RepositoryError` wraps `IntegrityError`; `AuditLogEntry`/`AuditResult` domain types |
| 5 | `app/services/idempotency.py` — PostgreSQL-backed `with_idempotency`; migration `b2c3d4e5f6a7` (`idempotency_keys`) |
| 6 | `app/services/qr/generation.py` — `QRGenerationService.generate_batch` + `GenerateBatchRequest`; `current_request_id()` extracted to `app/observability/request_id.py` |
| 7 | `QRLookupService`; `POST /api/v1/admin/batches/`, `GET /api/v1/admin/batches/{id}`, `GET /api/v1/qr/{id}`; routers registered |
| 8 | Acceptance + close-out (this entry) |

### Quality bar at close

- **219 tests** (unit + integration), **100% line + branch coverage** across `app/`
- ruff + black + mypy clean
- Docker image builds (`docker compose build`); the migration chain `068437e38dd9 → a1b2c3d4e5f6 → b2c3d4e5f6a7` applies via the entrypoint's `alembic upgrade head` (proven by `tests/integration/test_migrations.py` and `test_db.py::test_alembic_upgrade_head_succeeds`)

### Pyproject deviations from baseline

**None.** No new dependencies, no version bumps. Token generation used stdlib `secrets`; idempotency hashing used stdlib `hashlib`/`json`; the endpoint tests used `httpx` (already a production dep).

### Architectural decisions worth carrying forward

- **Domain owns the enums.** `QRStatus` (`app/domain/qr.py`) and `AuditResult` (`app/domain/audit.py`) live in the domain layer; SQLAlchemy models import them — mirrors the pure-Python-domain rule.
- **SQLAlchemy `Enum` columns need `values_callable`.** Without it, `sa.Enum(SomeStrEnum)` binds the member *name* (`FREE`) not the *value* (`free`), mismatching the Postgres enum literals. Caught by Task 4's round-trip tests — would have shipped if Sprint 2 had only model-level tests. Both `qr_status` and `audit_result` columns set `values_callable`.
- **The caller owns the transaction, not the service.** `QRGenerationService.generate_batch` issues writes without committing; the API endpoint commits batch + codes + audit + idempotency placeholder + recorded response in one transaction. (Task 6 first built the service owning its own transaction; Task 7 refactored it so idempotency could compose atomically.) The failure path still gets its own committed transaction for the `result='failure'` audit row.
- **Idempotency: the UNIQUE constraint is the serialization mechanism.** Concurrent same-key requests race to INSERT the `(user_keycloak_id, key)` placeholder; the loser blocks on the constraint until the winner commits, then reads the cached response. The placeholder lives in the caller's transaction, so a failed request rolls it back and the next attempt is fresh.
- **`current_request_id()` extracted** from `app/netbox/client.py` to `app/observability/request_id.py` — shared by the NetBox client and the audit-writing services.
- **QR domain `__post_init__` mirrors the DB CHECK.** An illegal `QR` can't be constructed in Python, so bugs surface at the call site instead of as opaque `IntegrityError`s deep in a transaction.

### Sprint 2 retrospective

**What went well:**
- TDD + plan-then-confirm held across all 8 tasks. Two bugs were caught by tests rather than reaching production: the `values_callable` enum-binding bug (Task 4) and the ToR-alphabet discrepancy (Task 3, below).
- The database invariants (CHECK + partial unique index) went into the schema from day one (Task 2), as the sprint plan intended — cheaper than retrofitting.
- The race-condition idempotency test (`asyncio.gather` of two same-key requests) ran 10× consecutively without flaking.

**What slowed us down:**
- **coverage.py + async-endpoint tracing (Task 7).** coverage.py does not reliably trace `return`/`raise` lines in async endpoint handlers when the request is driven through the ASGI stack (`TestClient` or `httpx.ASGITransport`) — `qr.py` traced fully, `batches.py` did not. A pure-ASGI-middleware experiment (to remove the `BaseHTTPMiddleware` child task) was tried and **reverted** — it didn't help; `main.py` is back to its Sprint 1 form. Resolution: endpoint tests `await` the handler functions **directly** on the test's own event loop (coverage traces that reliably, like every service test), paired with `httpx.AsyncClient` integration tests for routing / role-gating / `response_model_exclude_none` shaping. Genuine 100%, no `# pragma: no cover`. **Carry-forward for Sprint 3:** test endpoint handler logic by direct `await`; use HTTP-client tests only for wiring.
- The IDE's wrong interpreter path kept emitting false "module not found" diagnostics (same as Sprint 1) — harmless, ignored.

**ToR discrepancy found and recorded:**
- `docs/sprint-2.md` (Task 3) described the DCQR alphabet as excluding `0/O/1/I/L`. ToR §4.2.1 — the authoritative spec — excludes `I, O, 0, 1`, and the literal alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` contains `L`. The implementation followed the ToR (32 chars, excludes I/O/0/1). The sprint-2.md comment was a one-character error; corrected during this close-out.

**Deliberately deferred (carried into Sprint 3+):**
- `bind` / `retire` QR operations — require the NetBox PATCH + journal entry + audit row (three-record write). Sprint 3.
- Device read / update / decommission endpoints — Sprint 3, share the three-record write apparatus.
- PDF label generation (`GET /api/v1/admin/batches/{id}/pdf`) — blocked on Architecture §11.1 (reportlab vs weasyprint vs fpdf2).
- Web admin pages — separate sprint after the API stabilises.
- `shift_sessions` table — added when session-start/end endpoints are built; `audit_log.session_id` is already nullable for it.
- `GET /api/v1/admin/audit` query endpoint — Sprint 3+ when there's data worth querying.
- `GET /api/v1/admin/batches/` list endpoint with filters — add when the web admin needs it.
- Idempotency-key TTL cleanup job — the `idempotency_keys.created_at` index supports `DELETE WHERE created_at < NOW() - INTERVAL '24 hours'`; the background job itself is a separate sprint.
- **Manual smoke against real Keycloak / NetBox** — skipped: no reachable instances in this environment (same as Sprint 1). The integration tests (respx-mocked Keycloak + real Postgres) and the endpoint tests cover the generate → lookup → idempotency-replay round-trip.

### Files added in Sprint 2 (high-level)

- `backend/app/domain/{qr,audit}.py`
- `backend/app/db/models/` package — `{__init__,qr,audit,idempotency}.py` (replacing the single `models.py`)
- `backend/app/db/repositories/{__init__,errors,qr_code,qr_batch,audit_log}.py`
- `backend/app/services/idempotency.py`, `backend/app/services/qr/{__init__,token,generation,lookup}.py`
- `backend/app/observability/request_id.py`
- `backend/app/api/v1/qr.py`, `backend/app/api/v1/admin/{__init__,batches}.py`
- `backend/alembic/versions/{a1b2c3d4e5f6_qr_batches_qr_codes_audit_log,b2c3d4e5f6a7_idempotency_keys}.py`
- `backend/tests/unit/domain/{__init__,test_qr,test_audit}.py`
- `backend/tests/unit/services/{__init__,test_idempotency}.py`, `backend/tests/unit/services/qr/{__init__,test_token,test_generation}.py`
- `backend/tests/unit/test_request_id.py`
- `backend/tests/unit/api/v1/{conftest,test_batches,test_qr_lookup,test_batches_contract}.py`
- `backend/tests/integration/{test_migrations,test_repositories,test_idempotency,test_generation,test_lookup}.py`

---

## Sprint 3 — Device Read & Update (closed 2026-05-20)

**Status:** Closed. Tasks 1–7 complete.

### What shipped

| Task | Deliverable |
|---|---|
| 1 | NetBox write client: `patch`/`post`/`options` + 10s write timeout + 501 non-retry; `_send` generalized to carry JSON body + per-request timeout |
| 2 | `NetBoxWriteService.patch_with_attribution`: re-read + compare + PATCH + journal POST + audit row, all sharing one `request_id`; `WriteConflictError` carrying current state |
| 3 | `TTLCache` (generic in-process TTL, injectable clock); `MetaLookupService` + `GET /api/v1/meta/{sites,racks,statuses}` cached 5 min; statuses discovered via `OPTIONS /api/dcim/devices/` |
| 4 | Server-driven form: `app/services/forms/device_edit.yaml` + `DeviceFormConfig` (skeleton-typed, `extra="allow"`); `GET /api/v1/meta/device-form` |
| 5 | `DeviceService.get_device` + `GET /api/v1/devices/{id}` returning `DeviceResponse {data, version}`; global `NetBoxNotFound → 404` and `NetBoxClientError → 502` exception handlers in `main.py` |
| 6 | `DeviceUpdateRequest` + `to_netbox_changes`; `PATCH /api/v1/devices/{id}` driven by Task 2's `NetBoxWriteService`; local `WriteConflictError → 409` with `current_state` as `DeviceData` |
| 7 | Acceptance + close-out (this entry) |

### Quality bar at close

- **336 tests** (unit + integration), **100% line + branch coverage** across `app/` (1168 statements, 118 branches)
- ruff + black + mypy clean
- Stack runs unchanged via `docker compose up -d --build`; the migration chain from Sprint 2 is unchanged (Sprint 3 added no migrations — the device read/update use NetBox as the source of truth and only write to the existing `audit_log` table)

### Pyproject deviations from baseline

1. **New dependency `pyyaml` (`>=6.0,<7`)** — added in Task 4. Architecture §5
   and Sprint 3 decision E specify the server-driven device-edit form as a YAML
   file; the stack had no YAML parser. User approved adding `pyyaml` rather than
   switching the format to stdlib TOML. No first-party type stubs, so a mypy
   `module = "yaml.*"` override treats it as untyped — same call as `jose.*`
   (Sprint 1 deviation 3).

### Architectural decisions worth carrying forward

- **Global NetBox exception handlers in `main.py`.** `@app.exception_handler(NetBoxNotFound) → 404` and `@app.exception_handler(NetBoxClientError) → 502`. FastAPI's MRO dispatch picks the right one. Retrofits every NetBox-calling endpoint (meta + devices) uniformly — handlers stay thin, no per-endpoint try/except.
- **`endpoint-orchestrates-inline` for writes; thin service classes for reads.** `update_device` builds `NetBoxWriteService` per-request from the session (mirroring `batches.py::create_batch`); `read_device` delegates to a `DeviceService` built from the singleton client. Avoids inventing a `DeviceUpdateService` class.
- **`to_device_data` + `to_netbox_changes` as paired public module-level transforms** in `app/services/device.py`. Used by the read endpoint (response shaping), the update endpoint (request → NetBox PATCH body), and the 409 handler (current state shaping).
- **`extra="forbid"` on `DeviceUpdateRequest`** — typos like `serial_number` vs `serial` 422 instead of being silently dropped.
- **`extra="allow"` on `FormField`** — server-driven form's field-specific keys (`choices_endpoint`, `confirmation`, `depends_on`, …) pass through to the mobile client untouched; the backend never hardcodes field-level structure (CLAUDE.md #5).
- **Decisions A/B/C captured in `sprint-3.md` and applied uniformly.** A: re-read-and-compare for conflict detection (no `If-Unmodified-Since` to NetBox); B: NetBox-write-first, journal + audit best-effort with loud logs; C: `session_id` from the JWT `sid` claim. The Sprint 4 bind/retire/decommission writes inherit these.
- **`NetBoxWriteService.patch_with_attribution` is generic over any NetBox object PATCH** — Sprint 4's bind/retire/decommission writes reuse it without modification.
- **`_reset_netbox_client` in `tests/unit/api/v1/conftest.py`.** Closes the singleton-event-loop leak Sprint 1's work log warned about ("Watch for this in Sprint 2 when more singletons land"). The api/v1 `_truncate` now aclose+clears `get_netbox_client` (with `contextlib.suppress(Exception)` for robustness) at setup and teardown, so any future api/v1 test that touches NetBox stays clean.
- **`clean_env` clears `get_meta_cache` too** — added in Task 3 when the meta cache singleton landed. Same pattern Sprint 1 used for the other lru-cached singletons.
- **`OPTIONS /api/dcim/devices/`** for statuses discovery (per `parking-lot.md`'s "discovered dynamically from NetBox"). Required adding `client.options()` to the NetBox client (Task 3); reuses `_send`'s retry + 5s read timeout.

### Sprint 3 retrospective

**What went well:**
- Plan-then-confirm rhythm held across all 7 tasks — every plan was presented, approved, executed, and reviewed without scope drift.
- TDD with failure-mode counterparts caught real issues. The mid-sprint `/code-review` after Task 5 flagged M1 (a missing `last_updated` would have bypassed the FAILURE audit row, contradicting decision B's "every outcome produces an audit row" guarantee) and L1 (a leaked-from-dead-loop `aclose` could leave the conftest's cache populated). Both folded cleanly into Task 6 with a failure-mode test for M1.
- The `endpoint-tests-by-direct-await` memory from Sprint 2 was applied immediately. Tasks 3, 5, 6 all needed direct-`await` handler tests for coverage — and Task 5 surfaced that `main.py`'s NetBox exception handlers themselves need direct-`await` tests (coverage.py doesn't trace handler bodies run through Starlette's exception middleware, exactly like the Sprint 2 finding for endpoint handlers driven through the ASGI stack).
- The singleton-pollution failure (a `test_get_device_service_builds_a_device_service` leaking `get_netbox_client` → next test's `clean_env` doing `asyncio.run(aclose())` → pytest-asyncio's loop machinery breaking on "no current event loop") was diagnosed end-to-end and fixed in the right place — the api/v1 conftest, not the individual test — closing the latent issue Task 3's `test_meta.py` had quietly avoided through alphabetical test ordering.
- 100% line + branch coverage held across all 6 work tasks (1–6). No `# pragma: no cover`.

**What slowed us down:**
- The test Postgres container kept stopping (tmpfs ephemeral on Docker daemon restarts) — had to recreate it multiple times during the sprint. Friction, not a blocker.
- The command sandbox blocked TCP connections to localhost, so every DB-touching test run needed `dangerouslyDisableSandbox`. This also corrected a wrong assertion in the Task 1 close-out: 35 `tests/unit/api/v1/` "errors" I'd flagged as pre-existing were actually sandbox + a junk `DATABASE_URL` I'd passed; the full 336-test suite passes cleanly with the real DB.
- The pytest-asyncio "no current event loop" trace took a few minutes to thread back through `clean_env`'s `asyncio.run`. Worth keeping in mind: `asyncio.run` in 3.12 sets the current event loop to `None` on exit, and any subsequent `asyncio.get_event_loop()` (including pytest-asyncio's internal `_get_event_loop_no_warn`) raises. The Sprint 1 work-log warning about lru-cached singleton event-loop binding was concretely correct.
- The IDE wrong-interpreter-path issue from Sprints 1/2 continued — false "module not found" diagnostics on every edit. Harmless, ignored as before.

**Discrepancies between ToR / Architecture and what we shipped:**
- `Architecture_Overview.md` §5.1's example shows rack `depends_on: ["site", "location"]`, but decision F's MVP field set has no Location field. The shipped YAML uses `depends_on: [site]` only. The Architecture example is illustrative, not normative — flag for a docs sweep alongside any future ToR edit.
- `asset_tag` mapping: the YAML and the read parser use `custom_fields.asset_tag` per Architecture §5.1's example. NetBox devices also have a *native* `asset_tag` field. **Operations must confirm which one is canonical against the deployed NetBox before Task 6's PATCH writes to the wrong field at runtime.** Flagged in the YAML comment.
- `/meta/statuses` OPTIONS-based discovery (parsing `actions.POST.status.choices` with `value`/`display` keys) is unverified against the deployed NetBox version. Standard NetBox 3.x/4.x shape; respx tests confirm the parsing.
- ToR §4.3.3's device-screen field set (Identity / Location / Operational / Custom Fields / QR ID) is richer than Task 5's `GET /api/v1/devices/{id}` returns. Sprint 3 scope-limited the response to the editable fields + version (which is what Task 6's update needs to pre-fill the form); the full device-screen set ships with Sprint 4's combined QR+device response, consistent with `sprint-3.md`'s scope boundary.

**Deliberately deferred (carried into Sprint 4+):**
- QR `bind` / `retire` (free→bound transition in the same transaction as the NetBox write; bound→retired on device decommission) — Sprint 4.
- Device decommission (status → `Decommissioning`, retire bound QR) — Sprint 4; **also gated on a NetBox config dependency** (the additional device statuses) per `docs/parking-lot.md`.
- Device creation (`POST /api/v1/devices/`) — Sprint 4.
- Add-comment endpoint (`POST /api/v1/devices/{id}/comments` — a NetBox journal POST without a device PATCH) — Sprint 4.
- Combined QR+device response — extending `GET /api/v1/qr/{id}` to fetch the bound device from NetBox and return the full ToR §4.3.3 device-screen field set in one call — Sprint 4.
- `shift_sessions` table + `POST /api/v1/sessions/{start,end}` — later sprint.
- NetBox circuit breaker (Architecture §3.3) — deferred (decision D).
- PDF label generation + web admin — later sprints.
- `GET /api/v1/admin/audit` query endpoint — Sprint 4+ when there's a use case.
- Idempotency-key TTL cleanup job — pre-existing deferral from Sprint 2.
- Architecture §5.1 example update (drop `location` from rack `depends_on`) — docs sweep.
- Phase 2 alerting on three-record partial failures — already in `docs/parking-lot.md` (added in Task 2). Decision B accepts journal/audit failures as best-effort-logged; production must surface them.
- **Manual smoke against real Keycloak / NetBox** — skipped (no reachable instances in this environment, same as Sprints 1/2). The respx-mocked unit tests + real-Postgres integration tests cover the device read + update round-trip including conflict detection and audit-row landing.

### Files added in Sprint 3 (high-level)

- `backend/app/services/{cache,meta,netbox_write,device_form,device}.py`
- `backend/app/services/forms/device_edit.yaml`
- `backend/app/api/v1/{meta,devices}.py`
- `backend/tests/unit/services/{test_cache,test_meta,test_netbox_write,test_device_form,test_device}.py`
- `backend/tests/unit/api/v1/{test_meta,test_devices}.py`
- `backend/tests/integration/{test_netbox_write,test_devices}.py`

### Files modified in Sprint 3

- `backend/app/netbox/client.py` — added `patch`/`post`/`options`; generalized `_send` to carry JSON body + per-request timeout; 501 non-retry
- `backend/app/main.py` — meta + devices router registrations; `NetBoxNotFound`/`NetBoxClientError` exception handlers
- `backend/app/api/v1/meta.py` — `device-form` endpoint added in Task 4 (it was a Task 3 file extended in Task 4)
- `backend/tests/conftest.py` — `clean_env` clears `get_meta_cache` too
- `backend/tests/unit/api/v1/conftest.py` — `_reset_netbox_client` aclose+clear in `_truncate` (Task 5), `contextlib.suppress(Exception)` (Task 6 tidy)
- `backend/tests/unit/netbox/test_client.py` — write-method tests + `options` tests
- `backend/pyproject.toml` — `pyyaml` dep + mypy `yaml.*` override
- `backend/uv.lock` — auto-updated by `uv add pyyaml`
- `docs/sprint-3.md` — per-task plans filled in (was "TBD" at sprint start)
- `docs/parking-lot.md` — Phase 2 alerting on three-record partial failures (added in Task 2 context)

### How to run locally (close-of-sprint snapshot)

Same as Sprint 1/2:

```bash
cd backend
cp .env.example .env   # fill in real values
docker compose up -d --build
curl localhost:8000/health
docker compose down -v
```

Tests:
```bash
docker compose -f docker-compose.test.yml up -d
DATABASE_URL=postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test \
  NETBOX_URL=https://netbox.example.com NETBOX_SERVICE_TOKEN=x KEYCLOAK_BASE_URL=https://sso.example.com \
  uv run pytest --cov=app --cov-branch
```

---

## Sprint 4 — QR Lifecycle Completion (closed 2026-05-24)

**Status:** Closed. Tasks 1–4 complete.

### What shipped

| Task | Deliverable |
|---|---|
| 1 | QR bind: `QRLifecycleService.bind` with **sequential** transactions (pre-check tx → `patch_with_attribution` (audit tx) → FOR UPDATE + qr_codes UPDATE tx) and explicit **three-branch compensation** (Branch 1 happy / Branch 2 rolled-back / Branch 3 inconsistency). Compensation is **conditional + idempotent** (GET the device first; only PATCH if it shows our token). Compensation events land in `audit_log` with `after_json.failure_stage ∈ {"db_commit","compensation"}` and `compensation_outcome ∈ {"cleared","noop_different_qr","failed"}` — no `AuditResult` enum expansion, no migration. `POST /api/v1/qr/{qr_id}/bind` (role `dcinv-mobile-user`), returns combined `QRLookupResponse`. New repo methods: `QRCodeRepository.get_by_id_for_update` + `update` (non-wrapped IntegrityError so the orchestration distinguishes `qr_one_per_device` races) |
| 2 | QR retire: `QRLifecycleService.retire` with branched FREE / BOUND paths. FREE→RETIRED is pure DB (atomic SUCCESS audit row). BOUND→RETIRED clears `custom_fields.qr_id` via `patch_with_attribution` and reuses the same three-branch compensation in reverse (`_compensate_restore_qr`). `POST /api/v1/qr/{qr_id}/retire` (role `dcinv-admin` — decision I; destructive op, safer default than mobile-user). Compensation helpers (`_run_compensation`, `_best_effort_compensation_audit`, `_best_effort_inconsistency_journal`) parameterized to accept `operation` — one set serves both bind and retire (decision B uniform — "don't fragment the apparatus") |
| 3 | Combined QR+device response: `DeviceData` extended additively (`device_type`, `manufacturer`, `device_role`, `u_height`, `primary_ip4`, `primary_ip6`, `last_updated`, `qr_id` from app DB per decision H, filtered `custom_fields`). `to_device_data(device, *, qr_id=None)` with defensive `.get()` extraction. New `DeviceService.get_device_raw` returns raw NetBox dict so the combined response can inject the app-DB `qr_id`. `QRLookupService.get_by_id` returns `QRLookupResponse` (was `QRLookupResult`); soft-fails NetBox device fetch (`device_error="device_unavailable"`) per decision D. `GET /api/v1/qr/{qr_id}` switched to `response_model=QRLookupResponse` via the new `get_lookup_service` FastAPI dependency. **Wire-format breaking change** — see "Discrepancies" below |
| 4 | Acceptance + close-out (this entry) |

### Quality bar at close

- **460 tests** (Sprint 3 closed at 336; Sprint 4 added 124 net new), **100% line + branch coverage** across `app/` (1461 statements, 158 branches)
- ruff + black + mypy clean
- Migration chain unchanged (Sprint 4 added no migrations — every NetBox state transition rides on the existing `audit_log` table; the `failure_stage` discriminator lives in `after_json` JSONB)

### Pyproject deviations from baseline

**None.** No new dependencies, no version bumps.

### Architectural decisions worth carrying forward

- **Three-branch compensation pattern** (Architecture §4 + cross-cutting decision A): GET-then-conditionally-PATCH compensation, with three explicit outcomes (cleared/restored / no-op / failed). Captured in audit_log via `after_json.failure_stage`, avoiding enum expansion. Symmetric for bind (clear) and retire (restore); the same `_run_compensation` helper serves both via the `compensate_fn` + `operation` parameters.
- **Sequential transactions, not nested.** `patch_with_attribution` already owns its audit-row `session.begin()` (Sprint 3 decision). SQLAlchemy 2.0 async sessions can't nest `session.begin()`, and the user's "don't fragment the apparatus" call rules out refactoring `patch_with_attribution`. Solution: `bind` and `retire` open a Step A pre-check tx (read-only), then call `patch_with_attribution` (which opens its own audit tx), then open a Step C tx for the FOR UPDATE + qr_codes UPDATE. Three sequential txs, no nesting, no apparatus fragmentation.
- **`patch_with_attribution` gained an `entity_id: str | None = None` parameter.** Sprint 3 hardcoded `entity_id = str(netbox_object_id)`, which broke for `qr.bind` / `qr.retire` (where the audited entity is the QR token, not the device ID). Backward-compatible default preserves Sprint 3 behaviour.
- **`patch_with_attribution`'s journal + audit row stay best-effort, uniformly.** Decision B applies across bind and retire — no per-call-site override. The QR row in `qr_codes` is the source of truth for the binding; the regular-flow audit row duplicates it; an audit-write failure must not break the primary op.
- **Compensation never goes through `patch_with_attribution`.** The compensation PATCH is a direct `netbox_client.patch()` — routing it through the apparatus would write a misleading second journal entry attributing the compensation as a regular bind/retire. Branch 3 writes its own structured "INCONSISTENCY: ..." journal entry with `kind="danger"`.
- **`RuntimeError` over `assert` for defensive guards** (Sprint 1 M3 pattern, extended to Sprint 4): `session.in_transaction()` check, BOUND-without-`bound_to_device_id` check. Asserts get stripped by `python -O`; runtime checks survive.
- **`QRCodeRepository.update` does NOT wrap `IntegrityError`** (deliberate divergence from `bulk_insert`). The bind orchestration needs to catch the `qr_one_per_device` partial unique index violation specifically — wrapping in `RepositoryError` would erase the type information.
- **`bound_to_device_id` is preserved on BOUND→RETIRED** (Sprint 2 domain design — "Historical bound_* fields are preserved on a BOUND -> RETIRED transition so audit/forensics can trace prior ownership"). The `qr_one_per_device` partial unique index only constrains `status='bound'` rows, so this is safe. Caught by an integration-test assertion that initially asserted the wrong thing.
- **`get_lookup_service` FastAPI dependency** (Task 3, mirrors Sprint 3's `get_write_service` / Task 1's `get_lifecycle_service`). Endpoint tests stub the whole service via `app.dependency_overrides[get_lookup_service]` instead of seeding a real Postgres+respx scenario for every assertion.
- **Defensive `.get()` chains in `to_device_data`** for the Task 3 additions, so Sprint 3 test fixtures (which carry only the editable-field subset) continue to work additively. NetBox shape assumptions flagged in `docs/parking-lot.md` for production verification.
- **Compensation audit row uses `result=AuditResult.FAILURE`** with `after_json.failure_stage` as the discriminator. Avoids expanding the `AuditResult` Postgres enum (which would require a migration). Compensation event counts can be queried later via the JSONB column.
- **Two real bugs caught by integration tests that unit-test fakes didn't model:**
  - SQLAlchemy 2.0 **autobegin**: the pre-check `get_by_id` autobegins a transaction that conflicts with `patch_with_attribution`'s explicit `session.begin()`. Fix: wrap the pre-check in its own explicit `session.begin()` (closes the autobegun tx cleanly). Fake session updated to model the pre-check commit via `commits_to_succeed_first`.
  - **Cross-test pollution** via cached `get_settings()` with a junk `DATABASE_URL` from `test_lifecycle.py`'s `netbox_env` fixture. Fix: add `get_settings.cache_clear()` to `tests/unit/api/v1/conftest.py:_truncate`. Same pattern Sprint 3's `tests/integration/test_devices.py` conftest already used — extended to the api/v1 conftest.

### Sprint 4 retrospective

**What went well:**
- Plan-then-confirm rhythm held across all 3 work tasks. Each task got an upfront plan with explicit pseudocode for the highest-risk code (three-branch compensation) and corrections from the user before any code landed.
- TDD with failure-mode counterparts caught the right things: the autobegin conflict (Task 1 integration), the `bound_to_device_id` preservation behaviour (Task 2 integration), the cross-test pollution (Task 1 endpoint test triage). All were caught at test time, not in production.
- The compensation helpers parameterization (Task 2 step 2) was a clean refactor: lifted `operation` from hardcoded `"qr.bind"` to a parameter, derived structured-log event names from it (`qr_bind_*` → `qr_retire_*`), and Task 1's 27 tests passed without modification. Validation that the design generalises.
- Conditional compensation (Correction 3 from Task 1) made retire's "restore" path trivial — same GET-then-PATCH-if-matching pattern, just inverted. The shared `_run_compensation` orchestrator absorbed the compensate function as a callable.
- 100% line + branch coverage held across all tasks, including the unreachable defensive branches (RuntimeError for BOUND-without-device-id is provably dead under correct DB state — tested via `object.__setattr__` bypass of the frozen dataclass invariant).

**What slowed us down:**
- The autobegin discovery (Task 1 integration). The unit-test fake session was simpler than the real one; the integration test exposed the real session behaviour and forced a structural rethink of the bind flow (pre-check now wraps its own tx). Worth keeping in mind: SQLAlchemy 2.0 async session autobegins on first query, and `session.begin()` raises `InvalidRequestError` if a transaction is already pending (autobegun or explicit). Sprint 5+ orchestrations that mix DB reads and `patch_with_attribution` should follow the same explicit-pre-check-tx pattern.
- The test DB container disappeared mid-sprint (tmpfs ephemeral) — needed `docker compose -f docker-compose.test.yml up -d` to bring back. Same friction Sprint 3 noted; permanent fix would be a non-tmpfs volume, but that's an ops question for production.
- The `patch_with_attribution` `entity_id` discovery during Task 1 integration. Sprint 3's design hardcoded `entity_id = str(netbox_object_id)`, which is correct for `device.update` (the audited entity IS the device) but wrong for `qr.bind` (entity is the QR token, NetBox object is the device). Caught by an audit-row assertion; fixed by adding the optional `entity_id` kwarg. Backward compatible — Sprint 3 callers unchanged.
- The QR token alphabet caveat from Sprint 2 (ToR §4.2.1 excludes I/O/0/1) is in DB tests not enforced — my initial test IDs were 14 chars (`DCQR-UPDATE001`) and tripped the `VARCHAR(13)` check. Renamed to 13 chars. The DB doesn't enforce the alphabet either; the ToR-compliant generator does.

**Discrepancies between ToR / Architecture and what shipped (or breaking changes):**
- **`GET /api/v1/qr/{qr_id}` response shape changed.** Sprint 2 shipped the flat `{id, status, batch, bound_to_device_id, ...}`. Sprint 4 Task 3 returns the nested `{qr: {id, status, batch, ...}, device: {...} | omitted, device_error: "..." | omitted}`. Mobile clients **must adapt**. Documented in the endpoint docstring; flagged here so the mobile team picks it up before release.
- **Sprint 3's `GET /api/v1/devices/{id}` response shape expanded.** `DeviceData` now carries the Task 3 additions (`device_type`, `manufacturer`, `device_role`, `u_height`, `primary_ip4/6`, `last_updated`, `qr_id`, `custom_fields`). For the standalone read, `qr_id` is always `None` (no app-DB lookup in that path); the rest reflect NetBox data. Field additions are non-breaking for clients that ignore unknown fields, but a strict deserializer would reject. Mobile team take note.
- **NetBox response shape assumptions** parked in `docs/parking-lot.md` ("NetBox response shape verification"): three defensive code paths in `to_device_data` assume specific shapes (role key, u_height location, primary_ip address field). Verify against the deployed NetBox.
- The `_compensate_clear_qr` and `_compensate_restore_qr` no-op path is documented as MVP-acceptable (Sprint 4 Task 1 plan): if a concurrent winner overwrote our PATCH, we don't clobber their state. The "noop_different_qr" / "noop_already_restored" outcomes land in `audit_log.after_json.compensation_outcome` so we can quantify the rate later.
- Retire endpoint role is `dcinv-admin` (decision I), not `dcinv-mobile-user`. Open to mobile-user later if the business asks — tightening a role after release is harder than loosening one.
- `retired_reason` field on the retire endpoint payload: deferred (YAGNI). Domain `QR.retire(reason=...)` supports it; endpoint passes `reason=None`. Add when a UI consumer asks.

**Deliberately deferred (carried into Sprint 5+):**
- **Device decommission** (status → `Decommissioning`, reuses Task 2's `QRLifecycleService.retire` for the QR side) — Sprint 5; gated on NetBox status-config dependency (parking-lot).
- **Device creation** (`POST /api/v1/devices/`) — Sprint 5.
- **Add-comment endpoint** (`POST /api/v1/devices/{id}/comments` — journal POST without a device PATCH) — Sprint 5.
- **Error-shape unification on GET `/qr/{qr_id}`** — Sprint 2's `HTTPException(detail=...)` stays. The bind/retire structured `{"error": {"code"}}` shape is for new endpoints; normalising the GET would compound the wire-format break Task 3 already introduces. Sprint 5 candidate if we want consistency.
- **Standalone `/devices/{id}` populating `qr_id`** from the app DB (currently always `None` on that path). Sprint 5+ when a UI consumer needs it.
- **`shift_sessions` table + `POST /api/v1/sessions/{start,end}`** — later sprint.
- **NetBox circuit breaker** (Architecture §3.3) — deferred (Sprint 3 decision D).
- **PDF label generation, web admin pages** — later sprints.
- **`GET /api/v1/admin/audit` query endpoint** — Sprint 5+ when there's a use case.
- **Idempotency-key TTL cleanup job** — pre-existing deferral.
- **Manual smoke against real Keycloak / NetBox** — skipped (no reachable instances in this environment, same as Sprints 1-3). The respx-mocked unit + real-Postgres integration tests cover all branches incl. compensation.

### Files added in Sprint 4 (high-level)

- `backend/app/services/qr/lifecycle.py` — `QRLifecycleService` (bind + retire + shared compensation helpers + 5 new exception classes + private `_PostNetBoxStateRace` sentinel)
- `backend/tests/unit/services/qr/test_lifecycle.py` (50 tests)
- `backend/tests/unit/services/qr/test_lookup.py` (10 tests)
- `backend/tests/unit/api/v1/test_qr_bind.py` (14 tests)
- `backend/tests/unit/api/v1/test_qr_retire.py` (12 tests)
- `backend/tests/integration/test_qr_bind.py` (4 tests)
- `backend/tests/integration/test_qr_retire.py` (4 tests)

### Files modified in Sprint 4

- `backend/app/db/repositories/qr_code.py` — `+get_by_id_for_update`, `+update` (non-wrapping IntegrityError)
- `backend/app/services/netbox_write.py` — `+entity_id: str | None = None` parameter on `patch_with_attribution` (backward compatible default)
- `backend/app/services/device.py` — `DeviceData` extended (Task 3); `to_device_data` gains `qr_id` kwarg + defensive extraction; `+DeviceService.get_device_raw`
- `backend/app/services/qr/lookup.py` — `QRLookupResult` removed; `+QRInfo`, `+QRLookupResponse`, `+to_qr_info`; `QRLookupService.get_by_id` returns `QRLookupResponse`, DeviceService injection
- `backend/app/services/qr/__init__.py` — drop `QRLookupResult` export
- `backend/app/api/v1/qr.py` — `+get_lifecycle_service`, `+get_lookup_service`, `+bind_qr`, `+retire_qr`, GET endpoint rewired to return `QRLookupResponse`
- `backend/tests/unit/services/test_device.py` — +21 tests for Task 3 additions
- `backend/tests/unit/api/v1/test_qr_lookup.py` — rewritten for new combined-response shape; +1 dependency-builder test
- `backend/tests/unit/api/v1/conftest.py` — `+get_settings.cache_clear()` in `_truncate` (fix cross-test pollution caught during Task 1)
- `backend/tests/integration/test_repositories.py` — +6 tests for new repo methods
- `backend/tests/integration/test_lookup.py` — rewritten for new service signature
- `docs/sprint-4.md` — per-task detail filled in (was "TBD" at sprint start); all three tasks documented with Goal/Steps/Acceptance/Anti-criteria/Suggested prompt
- `docs/parking-lot.md` — NetBox response shape verification entry (Sprint 4 Task 3)

### How to run locally (close-of-sprint snapshot)

Same as Sprint 3:

```bash
cd backend
cp .env.example .env   # fill in real values
docker compose up -d --build
curl localhost:8000/health
docker compose down -v
```

Tests:
```bash
docker compose -f docker-compose.test.yml up -d
DATABASE_URL=postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test \
  NETBOX_URL=https://netbox.example.com NETBOX_SERVICE_TOKEN=x KEYCLOAK_BASE_URL=https://sso.example.com \
  uv run pytest --cov=app --cov-branch
```
