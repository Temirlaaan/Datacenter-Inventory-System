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

---

## Sprint 5 — Device Write Completion (closed 2026-05-28)

**Status:** Closed. Tasks 1–4 complete, close-out done.

### What shipped

| Task | Deliverable |
|---|---|
| 1 | `NetBoxWriteService.post_with_attribution` — create-path peer of `patch_with_attribution`. Handles two attribution shapes via the same call: pre-existing target (add-comment passes `netbox_object_id=device_id` + `entity_id=str(device_id)`) and self-attributing create (device create passes `netbox_object_id=None` + `entity_id=None`, both derived from `created["id"]`). `attach_journal=False` for add-comment where the POST IS the journal entry. Shared `_record_audit` extracted from `patch_with_attribution`'s inline closure so both apparatus methods reuse the same best-effort audit logic (decision B uniform). New `NetBoxValidationError(NetBoxClientError)` raised by `NetBoxClient._send` for non-404 4xx, backward-compatible subclass so Sprint 3/4 tests pass unchanged |
| 2 | Device create: `POST /api/v1/devices/` (role `dcinv-mobile-user`). `DeviceCreateRequest` Pydantic schema with `extra='forbid'` mirrors the ToR §4.3.2 mandatory-on-create field set (name, status_id, site_id, rack_id, role_id, device_type_id, position, serial, comments, asset_tag). `to_netbox_create_payload(req)` renames `*_id` → NetBox FK keys (`site_id` → `site`, `device_type_id` → `device_type`, `role_id` → `role`). `device_create.yaml` form config split from `device_edit.yaml` (decision E — avoids the creation-only flag complexity in a single YAML; the mobile app picks the right form per screen). **Correction 2:** `NetBoxValidationError` translated to a structured 422 with `error.code="NETBOX_VALIDATION_ERROR"` carrying NetBox's actual message + status — so the mobile client can surface validation failures (duplicate name, position collision, invalid status) rather than the global 502. Other NetBox errors (404 on referenced FK, 5xx) flow through `main.py`'s global handlers unchanged |
| 3 | Add-comment: `POST /api/v1/devices/{id}/comments` (role `dcinv-mobile-user`). New `CommentService` is a thin wrapper over `post_with_attribution` with `attach_journal=False`. Audit attribution `entity_type="device"`, `entity_id=str(device_id)` (caller-provided, not derived). 201 response is just `{"id": journal_entry_id}` — minimal surface. **Correction 3:** `max_length=2000` on `comment` field — bounds audit_log JSONB growth (50 ops/day * 2k chars = 100k/day vs 500k at 10k); NetBox journal `comments` supports more, but 2k is the policy cap |
| 4 | Device decommission: `POST /api/v1/devices/{id}/decommission` (role `dcinv-admin`, decision G). New `DeviceDecommissionService` with QR-first ordering (decision C — keeps failure modes recoverable). Flow: lookup bound QR via new `QRCodeRepository.find_by_bound_device_id` → if present, retire via `QRLifecycleService.retire` (Sprint 4 reuse) → PATCH device `status` to `"decommissioning"`. **Three-branch compensation on the device PATCH** when a QR was retired in Step B: re-bind the QR via `QRLifecycleService.bind` using the captured post-retire device version. Branch 2 (re-bind ok) → `DeviceDecommissionRolledBackError` → 500 `DECOMMISSION_ROLLED_BACK`. Branch 3 (re-bind fails) → `DeviceDecommissionInconsistencyError` → 500 `DECOMMISSION_INCONSISTENCY` + best-effort danger journal on the device. **Correction 4:** when `retire` itself raises `QRRetireInconsistencyError` (Sprint 4 Branch 3), decommission aborts (no device PATCH) and maps to the distinct 500 `QR_INCONSISTENT_AT_DECOMMISSION_ATTEMPT` — the QR is in an undefined state and changing the device status would compound the inconsistency. **Step 0 prerequisite** (apparatus change): `QRLifecycleService.retire` return signature extended to `tuple[QR, dict[str, Any] | None]` so decommission can capture the post-retire `last_updated` for the deterministic OCC token used in re-bind. Endpoint callers destructure `retired_qr, _ = await lifecycle.retire(...)` |
| 5 | Acceptance + close-out (this entry) |

### Quality bar at close

- **589 tests** (Sprint 4 closed at 460; Sprint 5 added 129 net new), **100% line + branch coverage** across `app/` (1691 statements, 178 branches) — `--cov-fail-under=100` gate passes
- ruff + black + mypy clean (drive-by fix in this close-out: two pre-existing mypy errors from Tasks 2/3 close-outs — intentional-typo arg in `test_device.py:534`, now-unused `# type: ignore[no-untyped-def]` in `test_repositories.py` — addressed in the Task 4 commit)
- Migration chain unchanged (Sprint 5 added no migrations; `device.decommission` and `device.add_comment` audit rows ride on the existing `audit_log` table without schema change)

### Pyproject deviations from baseline

**None.** No new dependencies, no version bumps.

### Architectural decisions worth carrying forward

- **`post_with_attribution` as the create-path peer of `patch_with_attribution`** (Task 1, decision Q1 from skeleton). Separate method, not a flag on the patch method — POST has no optimistic-concurrency check, no re-read, and the journal target derivation differs (caller-provided ID vs derived-from-response). Shared `_record_audit` keeps the best-effort audit logic in one place. **Two attribution shapes** through the same signature: pre-existing target (`netbox_object_id=int` + `entity_id=str(...)`) for add-comment, self-attributing (`netbox_object_id=None` + `entity_id=None`, both derived from `created["id"]`) for device create. The "don't fragment the apparatus per call-site" rule held.
- **`NetBoxValidationError(NetBoxClientError)` as a backward-compatible subclass.** Raised by `NetBoxClient._send` for non-404 4xx. Sprint 3/4 tests that catch the base `NetBoxClientError` still pass; Sprint 5's create endpoint catches the subclass specifically to emit the structured 422. Add-comment doesn't catch it (Sprint 5 deliberate scope decision — narrow failure mode, the generic 502 is appropriate; Sprint 6 candidate to extend specialised 422 translation across all write endpoints).
- **QR-first decommission ordering with re-bind compensation** (Task 4, decision C). Retiring the QR before the device-status PATCH means a stuck "QR retired, device still active" outcome is more recoverable than "device decommissioning, QR still bound". The re-bind compensation uses the **captured post-retire `last_updated`** as the OCC token — deterministic: if a third party modified the device between our retire and our re-bind, the re-bind raises `WriteConflictError` (escalates to Branch 3 inconsistency) rather than silently skating past with `expected_version=None`. That `WriteConflictError` IS the operational signal we want — concurrent edits during a decommission are unusual and worth human attention.
- **The decommission service's `device_decommission_db_failed_qr_recompensated` log key** (Task 4). Branch 2 log key for "device PATCH failed but compensation re-bind succeeded" — paired with the `DeviceDecommissionRolledBackError`-coded 500 response so operators can grep for the partial-failure pattern without correlating across audit rows. Branch 3 emits `device_decommission_inconsistency_unrecoverable` at critical level; Correction 4 emits `device_decommission_aborted_qr_inconsistent` at critical level.
- **`QRLifecycleService.retire` return extended additively** (Task 4 Step 0). New shape `tuple[QR, dict[str, Any] | None]` mirrors `bind`'s `tuple[QR, dict[str, Any]]`; FREE path returns `(qr, None)`, BOUND path returns `(qr, updated_device_dict)`. Endpoint callers destructure `retired_qr, _ = ...`; behavior unchanged at the endpoint. Mechanical churn (one endpoint + a handful of test destructures) but enables decommission's OCC chain.
- **Decommission service's compensation never goes through `patch_with_attribution`** — same pattern as Sprint 4's bind/retire compensation. The Branch 3 danger journal is posted via the raw `NetBoxClient.post` to avoid writing a misleading second journal entry attributing the compensation as a regular bind.
- **`QRCodeRepository.find_by_bound_device_id(device_id) -> QR | None`** — single-row lookup defended by the `qr_one_per_device` partial unique index (≤1 BOUND row per device, guaranteed by schema). Returns `None` cleanly when the device has no bound QR (or has only historical RETIRED rows with the device_id preserved per Sprint 2 design).
- **Decommission DI deliberately omits `QRBatchRepository`** despite the task plan listing it. The decommission flow has no use for the batch repo; injecting it would be dead code. Plan list was a hand-rolled DI menu, not a behavioural spec. Documented here so the plan-vs-code deviation isn't mysterious.
- **Decommission accepts `reason: str | None` but currently only binds it to a structured log field.** Plumbing it into the NetBox journal entry's comments is a Sprint 6 candidate; the forward-compatible signature avoids needing a body-shape change later.

### Sprint 5 retrospective

**What went well:**
- Plan-then-confirm rhythm held across all 4 work tasks plus the skeleton review. The Task 4 skeleton review (Q1-Q5) caught architectural ambiguities up front (post_with_attribution as separate method, decommission as its own service, QR-first ordering with compensation logic required, add-comment as a separate endpoint, device_create.yaml split from device_edit.yaml) — none of these were obvious from the sprint plan alone, and a code-first approach would have rewritten them.
- TDD discipline plus failure-mode counterparts caught real bugs at test time: Task 1's happy-path miscount (pre-check commit caused commits=2 not 1; fixed via the fake session's `commits_to_succeed_first`); Task 1's H1 from code review (bind endpoint forgot `qr_id=bound_qr.id` kwarg in `to_device_data` → device.qr_id was None); Task 2's RUF002 ambiguous character + the `_post_call` helper's hardcoded `netbox_path`.
- 100% line + branch coverage held through 4 implementation tasks. The `--cov-fail-under=100` gate ran clean on the close-out.
- The "don't fragment the apparatus per call-site" rule held under pressure. Task 1 considered extending `patch_with_attribution` with a `method` flag; the skeleton review rejected that and committed to `post_with_attribution` as a separate method. Task 4 considered branching `patch_with_attribution`'s journal logic for decommission's danger entry; instead it posts via the raw `NetBoxClient.post`, same pattern as Sprint 4 compensation. Apparatus stayed clean.
- Captured `post_retire_version` from `retire`'s response (Task 4 Step 0) instead of passing `expected_version=None` on re-bind. Deterministic OCC tokens make the compensation behaviour predictable; the `WriteConflictError` it produces on concurrent edits is informative, not annoying.

**What slowed us down:**
- Cross-task fixture pollution intermittently produced 27 failures when running `tests/integration/test_repositories.py` alongside `tests/unit/services/test_device.py` standalone. Full-suite ordering avoids it; just an artifact of ad-hoc test selection during diagnostics. Not a real regression.
- The test DB container went stale twice during the sprint (tmpfs ephemeral) — needed `docker compose -f docker-compose.test.yml up -d` to bring back. Same friction Sprint 3-4 noted; permanent fix is a non-tmpfs volume.
- `_device_dict` test fixtures in service tests were missing required fields (`serial`, `comments`) that `to_device_data` reads. Surfaced as `KeyError` on first run of the new Task 4 service tests; fixed by adding the keys. Will revisit Sprint 6 — a shared `_device_factory` helper would prevent the recurrence.
- Two pre-existing mypy errors slipped past Tasks 2/3 close-outs: intentional-typo `device_type` kwarg in `test_device.py:534` (Pydantic ValidationError test) and a now-unused `# type: ignore[no-untyped-def]` in `test_repositories.py`. Surfaced when Task 4 ran the mypy gate against the full tree; fixed inline in the Task 4 commit. Task close-outs going forward should re-run `mypy app/ tests/` (not just `app/`) before declaring clean.

**Discrepancies between ToR / Architecture and what shipped:**
- **`decommissioning` status slug is unverified.** Sprint 5 Task 4 hardcodes `changes={"status": "decommissioning"}` as the assumed NetBox-convention lowercase slug. Production deploy gates on `OPTIONS /api/dcim/devices/`'s `actions.POST.status.choices[].value` matching — if it differs (some NetBox installs use display-cased slugs), update the constant in `device_decommission.py`. One focused call site, isolated by design. Carried in `docs/parking-lot.md` under "Pending NetBox configuration".
- **Add-comment endpoint doesn't have specialised 422 translation** for `NetBoxValidationError`. Decision: add-comment's failure mode is much narrower than device-create (no FK constraints, no uniqueness rules), so the generic 502 is appropriate. Sprint 6 candidate to extend the catch across all write endpoints once we know which 4xx shapes NetBox actually returns in production.
- **Decommission `reason` field plumbed only to logs**, not to the NetBox journal entry's comments. Sprint 6 work — the forward-compatible signature avoids needing a body-shape change later.
- **`device_create.yaml` form config exists but isn't served by an endpoint yet.** Sprint 3 shipped `GET /api/v1/meta/device-form` returning the edit form; Sprint 5 splits the create form into its own YAML but doesn't add a sibling endpoint. The split was the right architectural move (avoids the creation-only flag complexity), but the mobile-facing endpoint is Sprint 6 work.
- **RBAC for device create:** decision G allows any `dcinv-mobile-user`. Flagged in `docs/parking-lot.md` for post-rollout reconsideration — device create is not a routine mobile op (existing devices are scanned, not created), and Option A (`dcinv-mobile-power-user` role) or Option B (admin-only) may fit better.

**Deliberately deferred (carried into Sprint 6+):**
- **Error-shape unification on `GET /qr/{qr_id}`** — Sprint 2's `HTTPException(detail=...)` shape stays. Same Sprint 5 candidate noted in Sprint 4 close-out; not picked up this sprint.
- **Sessions: `shift_sessions` table + `POST /api/v1/sessions/{start,end}`** — Sprint 6+.
- **`GET /api/v1/admin/audit` query endpoint** — Sprint 6+ when there's a use case.
- **`GET /api/v1/meta/device-create-form`** — endpoint to serve the new `device_create.yaml` to mobile (Sprint 5 shipped the YAML split but not the endpoint).
- **Standalone `/devices/{id}` populating `qr_id`** from the app DB — still `None` on that path. Sprint 6+ when a UI consumer needs it (the combined `GET /qr/{id}` already provides it).
- **Plumbing decommission's `reason` into the NetBox journal comment** — Sprint 6.
- **Specialised 422 translation across all NetBox-write endpoints** (decision C extension from Task 3) — Sprint 6.
- **NetBox circuit breaker** (Architecture §3.3) — deferred (Sprint 3 decision D).
- **PDF label generation, web admin pages** — later sprints.
- **Idempotency-key TTL cleanup job** — pre-existing deferral.
- **Manual smoke against real Keycloak / NetBox** — skipped (no reachable instances in this environment, same as Sprints 1-4). The respx-mocked unit + real-Postgres integration tests cover all branches incl. three-branch compensation.

### Files added in Sprint 5 (high-level)

- `backend/app/services/comment.py` (Task 3) — `CommentService`
- `backend/app/services/device_decommission.py` (Task 4) — `DeviceDecommissionService` + 2 new exception classes
- `backend/app/services/forms/device_create.yaml` (Task 2) — create form config split from `device_edit.yaml`
- `backend/tests/unit/services/test_comment.py` (Task 3)
- `backend/tests/unit/services/test_device_decommission.py` (Task 4, 11 tests)
- `backend/tests/unit/api/v1/test_device_create.py` (Task 2)
- `backend/tests/unit/api/v1/test_device_comments.py` (Task 3)
- `backend/tests/unit/api/v1/test_device_decommission.py` (Task 4, 13 tests)
- `backend/tests/integration/test_device_create.py` (Task 2)
- `backend/tests/integration/test_device_comments.py` (Task 3)
- `backend/tests/integration/test_device_decommission.py` (Task 4, 4 tests)

### Files modified in Sprint 5

- `backend/app/services/netbox_write.py` (Task 1) — `+post_with_attribution`, `+_record_audit` (extracted from `patch_with_attribution`'s inline closure), `+_post_create_journal_entry`
- `backend/app/netbox/errors.py` (Task 1) — `+NetBoxValidationError(NetBoxClientError)`
- `backend/app/netbox/client.py` (Task 1) — `_send` raises `NetBoxValidationError` for non-404 4xx
- `backend/app/services/device.py` (Task 2) — `+DeviceCreateRequest`, `+to_netbox_create_payload`
- `backend/app/db/repositories/qr_code.py` (Task 4) — `+find_by_bound_device_id`
- `backend/app/services/qr/lifecycle.py` (Task 4 Step 0) — `retire` return shape `QR` → `tuple[QR, dict[str, Any] | None]`; `_retire_bound` captures and threads through the `patch_with_attribution` return dict
- `backend/app/api/v1/qr.py` (Task 4 Step 0) — `retire_qr` destructures the new tuple
- `backend/app/api/v1/devices.py` (Tasks 2, 3, 4) — `+create_device`, `+add_comment`, `+decommission_device` endpoints + 3 new FastAPI DI factories + 3 new Pydantic request schemas
- `backend/tests/unit/services/qr/test_lifecycle.py` (Task 4 Step 0) — retire test return-value assertions updated for tuple
- `backend/tests/unit/api/v1/test_qr_retire.py` (Task 4 Step 0) — stub `retire` signature updated for tuple
- `backend/tests/integration/test_repositories.py` (Task 4) — +3 tests for `find_by_bound_device_id`; drive-by drop of now-unused `# type: ignore[no-untyped-def]`
- `backend/tests/unit/services/test_device.py` (Task 4 drive-by) — `extra='forbid'` test fixed to pass typo via `**dict` (was tripping mypy on the intentional bad kwarg)
- `docs/sprint-5.md` — per-task detail filled in across the sprint; 4 corrections incorporated (Correction 1 status slug verification gate, Correction 2 NetBoxValidationError translation, Correction 3 max_length=2000, Correction 4 QRRetireInconsistencyError abort path)
- `docs/parking-lot.md` — Sprint 4's "Pending NetBox configuration" entry extended with the Sprint 5 Task 4 slug-verification gate; RBAC decision G (device-create permission level) added as post-rollout revisit

---

## Sprint 6 — Shift Sessions (closed 2026-05-30)

**Status:** Closed. Tasks 1–5 complete.

### What shipped

| Task | Deliverable |
|---|---|
| 1 | `shift_sessions` storage layer: Alembic migration `c3d4e5f6a7b8` adds the table + `shift_end_reason` Postgres enum (`manual` / `inactivity_timeout` / `admin_force_close`) + `shift_end_consistency` CHECK (pairs `shift_end_at` with `end_reason`) + `shift_sessions_one_active_per_user` partial unique index (`WHERE shift_end_at IS NULL`). `app/domain/shift_session.py` adds `ShiftEndReason`, `IllegalShiftTransition`, frozen `ShiftSession` dataclass with `is_active` property + `end()` transition; `__post_init__` mirrors the CHECK so violations surface at construction. `app/db/models/shift_session.py` + `app/db/repositories/shift_session.py` (`get_by_id`, `get_active_for_user`, `insert`, `update`). `insert` deliberately does NOT wrap `IntegrityError` (unlike `AuditLogRepository`) — Task 2 catches the partial-unique-index race specifically to emit `SESSION_ALREADY_ACTIVE`. Mirrors Sprint 2's `qr_codes` DB-enforced-state pattern. |
| 2 | `ShiftSessionService` (`app/services/shift_session.py`) with `start`, `end`, `get_active`. Pure app-DB orchestration — no NetBox, no audit row (shifts are session metadata, not "operations on entities" — Architecture §3.1's three-record write doesn't apply). Exceptions: `SessionAlreadyActive(active)` (carries the existing shift for the 409 body) and `NoActiveShift(user_keycloak_id)`. `start()` catches the `IntegrityError` race, re-reads in a fresh tx, and raises `SessionAlreadyActive(winner)`; triple race (winner ended between the IntegrityError and re-read) lets the original `IntegrityError` propagate (practically impossible, accepted trade-off). Defensive `in_transaction()` guard mirrors `QRLifecycleService`. Service-layer `end` accepts any `ShiftEndReason` — wire-format restriction to `{manual, inactivity_timeout}` is enforced at the endpoint layer (decision E). |
| 3 | Three session endpoints in `app/api/v1/sessions.py`, all role `dcinv-mobile-user` (decision I): `POST /api/v1/sessions/start` (body `{tablet_id}`, `min_length=1`, 409 `SESSION_ALREADY_ACTIVE` carries the existing shift per decision B), `POST /api/v1/sessions/end` (body `{end_reason: Literal["manual","inactivity_timeout"]}` — `admin_force_close` rejected as 422 per decision E), `GET /api/v1/sessions/active` (200 + `{"session": null}` when none — chose explicit null over 404 so mobile uses a single null-check, not a try/except). The null path returns a raw `JSONResponse` to bypass `response_model_exclude_none` collapsing it to `{}`. |
| 4 | **Re-source `audit_log.session_id` across all four write services + dep-layer 409 gate.** Step (a): `AuthUser` gets `shift_session_id: UUID \| None` (default None — backward compatible); new `NoActiveShiftError` + `require_role_with_active_shift(role)` factory composes role gate + active-shift DB lookup; `app/main.py` registers the structured 409 handler. Step (b): `NetBoxWriteService._record_audit` + both `_format_journal_comment` / `_format_create_journal_comment` (the "Session:" text in NetBox journal entries) + `QRLifecycleService._retire_free` + `_best_effort_compensation_audit` all switched from `user.session_id` (JWT sid) to `user.shift_session_id`. Step (c): 6 write endpoints (`POST /qr/{id}/{bind,retire}`, `POST/PATCH /devices/{,id,id/comments,id/decommission}`) switched from `require_role` to `require_role_with_active_shift`; integration test fixtures + `tests/unit/api/v1/conftest.py` extended to seed a canonical active shift before each test and TRUNCATE `shift_sessions` after. Step (d): new `tests/integration/test_active_shift_gate.py` pins the 409 gate on all 6 endpoints + the end-to-end smoke (`POST /sessions/start` → `PATCH /devices/{id}` → assert `audit_log.session_id == shift.id` → `POST /sessions/end`). **`QRGenerationService` was deliberately NOT changed** — the admin-only batch path hardcodes `session_id=None` and is out of plan scope; admins can't open a shift via the current API. |
| 5 | Acceptance + close-out (this entry). |

### Quality bar at close

- **685 tests** (Sprint 5 → 589; Sprint 6 +96 net new), **100% line + branch coverage** across `app/` (1894 statements, 202 branches) — `--cov-fail-under=100` gate passes.
- ruff + black + mypy clean.
- One new migration (`c3d4e5f6a7b8_shift_sessions`); round-trips cleanly (`alembic upgrade head && downgrade -1 && upgrade head`); downgrade drops the index, the table, and the `shift_end_reason` enum in the right order.

### Pyproject deviations from baseline

**None.** No new dependencies, no version bumps.

### Architectural decisions worth carrying forward

- **`audit_log.session_id` semantic change (decision D — load-bearing for any future audit-query work).** Pre-Sprint-6 rows hold a JWT `sid` (ephemeral token UUID; a single shift typically spans several `sid` values as tokens rotate). Post-Sprint-6 rows hold a `shift_sessions.id` (shift UUID; one per engineer-shift). Schema unchanged — both columns are `UUID NOT NULL` — but the meaning of the value flipped. Audit-query consumers must either handle both interpretations or filter by `created_at > 2026-05-30`. No historical migration: rewriting old rows to a synthesised shift would invent attribution that didn't exist.
- **Decision E split for auto-end (10-min inactivity, ToR §4.1.3).** Mobile owns the 10-minute idle timer and calls `POST /sessions/end` with `{"end_reason": "inactivity_timeout"}`. The Sprint 7+ backend job will be the 12-hour-orphan fallback for crashed/offline tablets — `inactivity_timeout` is reused as the reason because operationally they're the same outcome ("tablet stopped sending heartbeats"). Backend `/end` accepts only `{manual, inactivity_timeout}` at the wire layer; `admin_force_close` is reserved for the Sprint 7+ admin endpoint, which will define its own auth surface.
- **Decision J for Keycloak revocation (load-bearing for the mobile/backend split).** Backend `/sessions/end` does NOT call Keycloak's revoke endpoint. Mobile is responsible for calling Keycloak's `/logout/revoke` with its refresh token AFTER `/sessions/end` returns success. Preserves Sprint 1's "mobile owns its tokens" principle, avoids adding a Keycloak admin client + new env vars (`KEYCLOAK_ADMIN_CLIENT_*`) to the backend, and matches the way mobile already speaks to Keycloak for OIDC login/refresh. **If a future requirement forces server-side revocation** (e.g. an admin force-close), the cleanest path is a new dedicated service rather than coupling it to `/sessions/end`.
- **`require_role_with_active_shift` as the single composite dep for write endpoints** (decision F.a). Role-gates first (so unauthorised callers don't pay the DB lookup) then does ONE indexed `SELECT * FROM shift_sessions WHERE user_keycloak_id = ? AND shift_end_at IS NULL` per request (the partial unique index makes this an index scan). The lookup is wrapped in an explicit `async with session.begin()` so SQLAlchemy 2.0's autobegun tx is committed/closed before downstream services open their own `session.begin()` blocks — without that, `QRLifecycleService.bind`'s defensive `in_transaction()` guard fires. Same pattern as Sprint 4 Q2.
- **No FOR UPDATE row lock on `end()`** — the partial unique index guarantees ≤1 active shift per user, so a concurrent end race collapses to last-write-wins on `shift_end_at` (acceptable: the operation is idempotent enough for a phone with twitchy connectivity). The TASK 2 plan called this out explicitly to make the omission obvious rather than accidental.
- **Triple-race in `start()` propagates `IntegrityError` rather than fabricating a `SessionAlreadyActive` with no payload.** If the partial-unique-index race fires AND a concurrent end happens between the IntegrityError and our re-read, the re-read returns None. Three concurrent requests for the same user within microseconds is theoretical; surfacing the raw `IntegrityError` as a 500 is more honest than constructing a placeholder.
- **`AuthUser.session_id` (JWT sid) kept on the dataclass** even though no service reads it after this sprint. Removing it would cascade to every test that constructs an AuthUser. Conservative; can be deleted in a follow-up if it stays unused.
- **Decision C: `/end` acts on the JWT-identified user's active session, not a session UUID in the body.** Avoids mobile having to persist the session UUID across app restarts; the server resolves it from `sub`.
- **`POST /api/v1/admin/batches/` is deliberately NOT gated** by `require_role_with_active_shift`. Batch generation is admin-only and the admin doesn't have a session API yet (decision I — sessions are mobile-driven). `QRGenerationService` hardcodes `session_id=None`, unchanged this sprint. Revisit when an admin sessions surface lands.

### Sprint 6 retrospective

**What went well:**
- Plan-then-confirm rhythm scaled to a 4-step task. Task 4 was decomposed in the plan into steps (a)→(d) with explicit "what's red after each step" expectations; that prediction held — exactly the integration tests the plan anticipated went red after step (b), and only those, with one bonus auth-test-fake-session miss caught quickly.
- TDD discipline held on Tasks 1-3 (tests before code, including failure-mode counterparts). Task 4 was a re-sourcing task across existing services; "write the new shape first" doesn't quite apply, but the per-step gate-running rhythm caught regressions immediately rather than at the close-out.
- Apparatus reuse was uniform: Sprint 2's CHECK + partial unique index pattern carried over to `shift_sessions` 1:1; Sprint 4's defensive `in_transaction()` guard pattern applied to `ShiftSessionService`; Sprint 4/5's `_user(...)` fake-AuthUser pattern extended cleanly to `shift_session_id`.
- 100% coverage held through 4 implementation tasks. The `--cov-fail-under=100` gate ran clean on the close-out.
- The Task 4 plan correctly predicted that `require_role_with_active_shift` would conflict with downstream services' own `session.begin()` blocks unless wrapped in an explicit `async with session.begin()` itself — the same fix Sprint 4 Q2 documented. Saved a debugging cycle.

**What slowed us down:**
- Step (a) of Task 4 initially shipped without the explicit `async with session.begin()` wrap around the shift lookup. SQLAlchemy 2.0's autobegun tx then conflicted with `QRLifecycleService.bind`'s `self._session.begin()`, surfacing as the cryptic `RuntimeError("QRLifecycleService.bind called inside an active transaction")` on the first integration-test run after the endpoint switch. One commit to fix; documented in the dep's comment for future reference.
- IDE noise (wrong Python interpreter path → constant false "module not found" diagnostics) continued across the sprint, same as Sprints 1-5. Not a real problem; just a steady drip of false positives in the editor.
- Test-DB container went stale once during the sprint (tmpfs ephemeral). Same friction Sprints 3-5 noted; permanent fix would be a non-tmpfs volume.
- The `tests/unit/api/v1/conftest.py` change to seed the canonical shift had to be paired with corresponding seed logic in 6 integration test files. Mechanical but boring; would have been cleaner with a shared `pytest_plugins` autouse, but the existing per-file `_truncate` fixtures already had per-file truncation scopes (some include `qr_codes`, some don't), so a shared autouse would have over-truncated. Per-file seeding it is.

**Discrepancies between ToR / Architecture and what shipped:**
- **`shift_sessions.id` vs JWT `sid` semantic change in `audit_log`** (decision D) is the only material divergence from Sprint 3's documented attribution. ToR §4.3.1's "Session: {session_id}" reference matches the new meaning; Architecture §3.1's three-record write apparatus is unchanged (still three records per write, still shared `request_id`).
- **`POST /api/v1/admin/batches/` keeps `session_id=None` on audit rows** — admin batch generation never had a shift attribution and still doesn't. Operationally this is a non-issue (audit rows still carry `user_keycloak_id` + `user_email` + `request_id`).
- **Enum naming divergence from ToR §7.2.4** (RESOLVED in Sprint 7 Task 0). Sprint 6 shipped descriptive `shift_end_reason` values (`inactivity_timeout`, `admin_force_close`) instead of ToR-canonical `auto_timeout`, `forced`. Caught during Sprint 7 ToR grep. Resolved via `ALTER TYPE RENAME VALUE` migration in Sprint 7 Task 0 before any post-Sprint-6 admin endpoints wrote to the column. Existing Sprint 6 rows preserved by Postgres' in-place rename semantics.

**Deliberately deferred (carried into Sprint 7+):**
- **Auto-end stale sessions (background job)** — backend fallback for tablets that crashed without sending the 10-minute idle `/end` call. Sprint 7 candidate; scope = a periodic job (cron or async task) that ends any `shift_sessions` row with `shift_start_at < NOW() - 12h AND shift_end_at IS NULL` with `end_reason='inactivity_timeout'`. 12h is liberal — mobile catches the 10-min case correctly under normal operation; the job is the safety net for the abnormal case.
- **Admin endpoints**: `GET /api/v1/admin/sessions` (list all shifts, filter by user/date) and `POST /api/v1/admin/sessions/{id}/force-close` (uses the reserved `admin_force_close` end reason). Both need an admin sessions surface — see decision I.
- **`POST /api/v1/admin/batches/` gating** — once admins have a shift surface, batch generation should also require an active shift for consistency with the rest of the write API. Architecturally a one-line endpoint change + an audit-row session_id source swap in `QRGenerationService`.
- **`GET /api/v1/admin/audit` query endpoint** — needed Sprint 6's sessions to be useful as a filter; now unblocked. Sprint 7+.
- **Sprint 5 carry-overs** (still deferred): decommission `reason` plumbed into NetBox journal comment; error-shape unification on `GET /qr/{id}`; `GET /api/v1/meta/device-create-form`; standalone `/devices/{id}` populating `qr_id`; specialised 422 translation across all write endpoints.
- **PDF labels, web admin, NetBox circuit breaker, idempotency-key TTL cleanup** — pre-existing deferrals.
- **Manual smoke against real Keycloak / NetBox** — skipped (no reachable instances in this environment, same as Sprints 1-5).

### Files added in Sprint 6 (high-level)

- `backend/alembic/versions/c3d4e5f6a7b8_shift_sessions.py` (Task 1) — migration
- `backend/app/domain/shift_session.py` (Task 1) — `ShiftEndReason`, `IllegalShiftTransition`, `ShiftSession`
- `backend/app/db/models/shift_session.py` (Task 1) — `ShiftSessionModel`
- `backend/app/db/repositories/shift_session.py` (Task 1) — `ShiftSessionRepository`
- `backend/app/services/shift_session.py` (Task 2) — `ShiftSessionService` + `SessionAlreadyActive` / `NoActiveShift` exceptions
- `backend/app/api/v1/sessions.py` (Task 3) — router + 3 endpoints + DI factory
- `backend/tests/unit/domain/test_shift_session.py` (Task 1, 19 tests)
- `backend/tests/integration/test_shift_sessions_migration.py` (Task 1, 12 tests)
- `backend/tests/integration/test_shift_session_repository.py` (Task 1, 12 tests)
- `backend/tests/unit/services/test_shift_session.py` (Task 2, 17 tests)
- `backend/tests/unit/api/v1/test_sessions.py` (Task 3, 24 tests)
- `backend/tests/integration/test_active_shift_gate.py` (Task 4, 7 tests — 6 gate + 1 E2E)

### Files modified in Sprint 6

- `backend/app/db/models/__init__.py` (Task 1) — `+ShiftSessionModel` registration
- `backend/app/db/repositories/__init__.py` (Task 1) — `+ShiftSessionRepository` export
- `backend/app/main.py` (Tasks 3, 4) — `+sessions_router` mount + `+handle_no_active_shift` exception handler
- `backend/app/auth/dependencies.py` (Task 4 step a) — `+AuthUser.shift_session_id`, `+NoActiveShiftError`, `+require_role_with_active_shift`
- `backend/app/services/netbox_write.py` (Task 4 step b1) — 3 sites switched from `user.session_id` to `user.shift_session_id` (audit row + both journal-text formatters)
- `backend/app/services/qr/lifecycle.py` (Task 4 step b2) — 2 audit-row sites switched to `user.shift_session_id`
- `backend/app/api/v1/qr.py` (Task 4 step c) — `bind_qr` + `retire_qr` switched to `require_role_with_active_shift`
- `backend/app/api/v1/devices.py` (Task 4 step c) — `create_device`, `update_device`, `add_comment`, `decommission_device` switched to `require_role_with_active_shift`
- `backend/tests/integration/conftest.py` (Task 4 step c) — `+DEFAULT_USER_KEYCLOAK_ID`, `+DEFAULT_SHIFT_SESSION_ID`, `+seed_default_active_shift(session)` helper
- `backend/tests/unit/api/v1/conftest.py` (Task 4 step c) — autouse fixture seeds the canonical shift; TRUNCATE extended to `shift_sessions`
- 6 integration test files (Task 4 step c) — autouse fixtures seed the canonical shift; TRUNCATE extended to `shift_sessions`
- `backend/tests/unit/services/test_netbox_write.py` (Task 4 step b1) — `_user(...)` helper switched to `shift_session_id`; the two session_id-on-audit tests now assert against the shift UUID
- `backend/tests/unit/services/qr/test_lifecycle.py` (Task 4 step b2) — `_user(...)` helper switched; compensation audit test updated
- `backend/tests/integration/test_netbox_write.py` (Task 4 step c) — `_user(...)` + the two session_id assertions migrated to `_SHIFT_SESSION_ID`
- `backend/tests/unit/auth/test_dependencies.py` (Task 4 step a) — 3 new tests for `require_role_with_active_shift` (happy path, 403, NoActiveShiftError)
- `backend/tests/unit/api/v1/test_devices.py` (Task 4 step a) — `+test_handle_no_active_shift_returns_409_with_structured_body`
- `docs/sprint-6.md` — per-task plans filled in across the sprint (this work-log entry is the close-out artifact)
- `docs/parking-lot.md` — admin sessions / `admin_force_close` / auto-end job carved out as Sprint 7+ deferrals
- `CLAUDE.md` — Repository Status updated for Sprint 6 closure

---

## Sprint 7 — Admin Surface + Polish (closed 2026-06-02)

**Status:** Closed. Tasks 0–6 complete.

### What shipped

| Task | Deliverable |
|---|---|
| 0 | **`shift_end_reason` enum rename to ToR §7.2.4 canon.** New Alembic migration `d4e5f6a7b8c9` (`ALTER TYPE shift_end_reason RENAME VALUE 'inactivity_timeout' TO 'auto_timeout'` + `'admin_force_close' TO 'forced'`). Non-destructive under CLAUDE.md §7 — no column/table dropped, no constraint touched; Postgres rewrites the enum label in place. `ShiftEndReason` StrEnum + `Literal[...]` in `SessionEndRequest` swept. The historical Sprint 6 migration `c3d4e5f6a7b8` keeps its original labels so anyone replaying history reaches the Sprint-6 shape before this migration applies. New `test_rename_migration_rewrites_pre_rename_rows_in_place` inserts pre-rename rows via raw SQL casts (since `ShiftSessionRepository.insert` now emits the new labels), applies the rename, and asserts the in-place rewrite. The Sprint 6 retrospective got a one-line addendum under "Discrepancies between ToR / Architecture and what shipped" — RESOLVED in Task 0. |
| 1 | **Auto-end stale-shifts background job (ToR §4.1.3 fallback).** `app/services/auto_end_job.py` — `AutoEndJobStatus` dataclass + `auto_end_loop` (asyncio loop in `app.main:lifespan` with cancel-via-`asyncio.Event` + 60s startup grace + per-iteration try/except + injectable `clock` for tests) + `_run_iteration` (per-row session/transaction so one bad row doesn't abort the iteration). Three new `Settings` knobs: `SHIFT_AUTO_END_ENABLED=true`, `SHIFT_AUTO_END_INTERVAL_SECONDS=300`, `SHIFT_AUTO_END_THRESHOLD_HOURS=12`. New repository method `ShiftSessionRepository.find_stale_active(*, older_than)` ordered by `shift_start_at ASC` (oldest cleaned up first). New service primitive `ShiftSessionService.end_by_id(*, session_id, reason)` shared with Task 3 force-close; new `ShiftSessionNotFound` for "id not in DB", existing `IllegalShiftTransition` covers "already ended" (both swallowed by the job as expected concurrent-end races). `/health` extended with `auto_end_job: {enabled, last_iteration_at, status}` sub-object — informational only (decision 1 of Task 1 plan): a `"stale"` sub-object does NOT flip overall `/health` status to `"degraded"` or return 503; operators alert on the sub-field. Status object always attached to `app.state` for shape consistency regardless of `SHIFT_AUTO_END_ENABLED`. |
| 2 | **`GET /api/v1/admin/audit` query endpoint.** Role `dcinv-admin` + active shift (decision I). Eight filters (`user_keycloak_id`, `from`, `to`, `entity_type`, `entity_id`, `operation`, `session_id`, `result`) + 1-indexed offset pagination (`page` ≥ 1, `page_size` 1..100, default 20). New `AuditLogQueryFilters` + `AuditLogRepository.query` returning `(rows, has_more)` via `LIMIT page_size + 1` — one query, no `COUNT(*)` round-trip (at 2-year retention × ~50 ops/day ≈ 36k rows minimum, that matters). Domain `AuditLogEntry.id: int \| None = None` activated (the field Sprint 2 reserved for "when a sprint requires reading back inserted rows"). Audit-of-audits row per ToR §5.4.6 + decision I: `operation="audit.query"`, `entity_type="audit"`, `entity_id="search"` (hard-coded so admin queries are themselves discoverable via `?entity_type=audit&entity_id=search`); `after_json={"filters": <as-passed>, "results_count": N}`; written in the SAME transaction as the user-facing query — read-without-audit is forbidden, so an audit-insert failure rolls back and returns 500. FAILURE path writes its own audit row (without `results_count`) and re-raises. OpenAPI description for the `session_id` filter carries the decision J semantic note (pre-2026-05-30 rows hold JWT sid; post-Sprint-6 hold `shift_sessions.id`) — locked in by a test that introspects `app.openapi()`. |
| 3 | **`GET /api/v1/admin/sessions` + `POST /api/v1/admin/sessions/{id}/force-close`.** Both role `dcinv-admin` + active shift. List endpoint: filters `user_keycloak_id` / `from` / `to` / `active_only` + the same pagination shape as Task 2. New `ShiftSessionQueryFilters` + `ShiftSessionRepository.query`. **No audit row on GET** (decision 8): shift listing is operational, not §5.4.6 sensitive. Force-close body `{"reason": str}` (required, `min_length=1, max_length=500`), ends with `end_reason='forced'`. Endpoint orchestrates the multi-record write (repo + audit) **directly in a single tx**, NOT via `ShiftSessionService.end_by_id`, because `ShiftSessionService` is deliberately not in the §3.1 apparatus (Task 1's `end_by_id` is for stand-alone callers like the auto-end job). Idempotent on already-ended target: returns 200 with current state + audit row `result=CONFLICT` + `after_json.no_op=true`. 404 on unknown id with NO audit row (admin typo, not a state-change conflict). Audit row's `session_id` is the admin's shift, NOT the target's (records who-did-what). No NetBox journal entry — force-close is a shift event, not a device event (consistent with mobile `/sessions/end`). |
| 4 | **Decommission `reason` plumbed into NetBox journal comment (Sprint 5 carry-over).** `_format_journal_comment` gains optional `reason: str \| None = None` parameter; renders a `Reason: <text>` line between the attribution block and the `Changes:` diff when provided; absent line (NOT `Reason: None`) when not. Plumbed through `_post_journal_entry` + `patch_with_attribution`. `DeviceDecommissionService.decommission` passes `reason=reason` to `patch_with_attribution`; docstring updated to mark this the implementation, not a future candidate. The audit row's `after_json` is unchanged — `reason` is human-readable journal attribution, not machine-queried forensics; if a future requirement wants `reason` in the audit row, that's a separate decision. **Task 4(a) (`GET /api/v1/meta/device-create-form`) had already shipped in Sprint 5** itself — the Sprint 7 plan's "endpoint was deferred" claim was stale (the endpoint is at `app/api/v1/meta.py:88-98` with tests in `tests/unit/api/v1/test_meta.py`). No code change for 4(a). |
| 5 | **`NetBoxValidationError → 422 NETBOX_VALIDATION_ERROR` translation extended to all six write endpoints.** Sprint 5 Correction 2 only covered `POST /api/v1/devices/`; Task 5 adds `PATCH /devices/{id}`, `POST /devices/{id}/comments`, `POST /devices/{id}/decommission`, `POST /qr/{id}/bind`, `POST /qr/{id}/retire`. New `app/api/v1/_helpers.py:netbox_validation_error_response(exc, *, fallback_message=...)` is the single shared body shape — `POST /devices/` refactored to use it; existing tests stayed green without modification. Per-endpoint `try/except` rather than a global `@app.exception_handler` (decision 1) — a global handler would also flip NBV from NetBox-side 401/403 on read endpoints (a backend-token issue) to a misleading 422; per-endpoint catches keep the translation explicit. The decommission bound-QR + device-PATCH-fails path is unaffected: compensation converts the NBV to `DeviceDecommissionRolledBackError` (500) before the endpoint sees it; the 422 path applies only to the no-bound-QR atomic-failure case. `result=FAILURE` audit row continues to land via existing `patch_with_attribution` / `post_with_attribution` logic — Task 5 is endpoint-layer only. |
| 6 | Acceptance + close-out (this entry). |

### Quality bar at close

- **802 tests** (Sprint 6 → 685; Sprint 7 +117 net new), **100% line + branch coverage** across `app/` — `--cov-fail-under=100` gate passes.
- ruff + black + mypy clean across `app/` and `tests/`.
- One new migration (`d4e5f6a7b8c9_rename_shift_end_reason_to_tor_canon`); round-trips cleanly; new integration test verifies `ALTER TYPE RENAME VALUE` rewrites pre-rename rows in place.

### Pyproject deviations from baseline

**None.** No new dependencies, no version bumps. Task 1's auto-end loop was deliberately implemented without APScheduler / Celery / aiojobs (Task 1 plan decision A: "no new dependency").

### Architectural decisions worth carrying forward

- **Auto-end job is single-replica until Sprint 8a (Task 1 decision A guardrail #3 — load-bearing for deployment).** Backend MUST run as a single replica until job ownership is solved via Postgres advisory lock OR k8s CronJob. The `shift_sessions_one_active_per_user` partial unique index + the idempotent `end_reason='auto_timeout'` outcome prevent the worst-case double-firing harm (cannot create two new actives; concurrent ends collapse to last-write-wins), but N replicas waste N× the DB scans on every interval. The single-replica caveat is documented both in `docs/sprint-7.md` (decision A) and in a comment on the `asyncio.create_task` line in `app/main.py`. Sprint 8a will revisit; the choice between advisory lock (simpler, app-owned) and k8s CronJob (external, operationally cleaner) is intentionally left open.
- **`shift_end_reason` enum uses ToR §7.2.4 canon (Task 0).** Values are `manual / auto_timeout / forced` — NOT Sprint 6's descriptive `inactivity_timeout / admin_force_close`. The rename migration is non-destructive under CLAUDE.md §7 (ALTER TYPE RENAME VALUE is in-place); existing Sprint 6 rows are rewritten by Postgres. Sprint 6's retrospective got an addendum under "Discrepancies" noting RESOLVED in Sprint 7 Task 0. **Do not re-introduce descriptive names** — the ToR is the contract.
- **Endpoint orchestrates the multi-record write directly when the service is NOT in the §3.1 apparatus (Task 3 force-close, decision 1).** `ShiftSessionService` is deliberately session-metadata-only (its docstring says so); force-close needs `shift_sessions.update` + `audit_log.insert` in one transaction. Rather than pollute `ShiftSessionService` with three-record-write responsibility, the endpoint composes the repos directly. Mirrors the audit-of-audits pattern from Task 2's `/admin/audit`. Future shift-write endpoints (admin start, admin end) should follow the same shape.
- **`LIMIT N+1` pagination, NOT `COUNT(*)` (Tasks 2 + 3).** Both `AuditLogRepository.query` and `ShiftSessionRepository.query` compute `has_more` by fetching `page_size + 1` and slicing the extra row. Cheap (one query, no separate count) and bounded — even a 2-year audit retention at ~50 ops/day = ~36k rows scans cleanly via the existing `audit_log_timestamp_idx`. Established pattern for future paginated read endpoints; do not add `total_count` to envelopes unless a consumer specifically needs it.
- **`AuditLogEntry.id` is now populated on the read path (Task 2).** Sprint 2's anticipated field (`id: int | None = None`, default None on insert) is active. The insert path is unchanged (BIGSERIAL populates DB-side); the read path through `AuditLogRepository.query` reads the row id back. Any future "read recent audit row by id" consumer can rely on this without further schema work.
- **Audit row attribution for admin actions: admin's shift_session_id, NOT the target's (Task 3 decision 10).** The `audit_log.session_id` column records WHO performed the action. For force-close, that's the admin (their `shift_session_id` populated by `require_role_with_active_shift`); the target shift's id lives in `entity_id`. Same pattern for the audit-of-audits row in Task 2. Future admin actions on other entities should follow this contract.
- **Active-shift gate on admin endpoints (decision I of `docs/sprint-7.md`).** `GET /admin/audit`, `GET /admin/sessions`, `POST /admin/sessions/{id}/force-close` all use `require_role_with_active_shift("dcinv-admin")`. Acknowledged trade-off: until an admin sessions surface lands (Sprint 8+), admins can't operate any of these endpoints — they return 409 NO_ACTIVE_SHIFT. The decision is deliberate (consistent audit attribution via `shift_session_id`); Sprint 8+ adds the admin-shift surface that unblocks live admin use. `POST /api/v1/admin/batches/` remains the lone admin endpoint NOT gated (decision F), because it predates the shift apparatus and gating it would brick the only working admin endpoint.
- **NBV translation is per-endpoint, NOT a global handler (Task 5 decision 1).** A global `@app.exception_handler(NetBoxValidationError)` would translate NBV everywhere — including read endpoints where NBV could surface for 401/403 against NetBox (a backend-token issue, not a user-input issue). Per-endpoint `try/except` + the shared `app/api/v1/_helpers.py:netbox_validation_error_response` helper keeps the translation explicit and intentional. The helper accepts an optional `fallback_message` so each endpoint can specialise the toast text without diverging the wire shape.
- **`/health` is the operational signal for the auto-end loop, NOT a 503 trigger (Task 1 decision 1).** A `"stale"` value on the `auto_end_job.status` sub-object surfaces in monitoring without changing overall `/health` status. The existing 503 semantic stays narrow (external dependency unreachable); operators alert on the sub-field separately. Write-path correctness is unaffected when the job is stale — only the safety net is.
- **Force-close on an already-ended target is an idempotent CONFLICT, NOT a 4xx (Task 3 decision 2).** A web admin double-clicking force-close (race or fat-finger) gets the same 200 response with the current shift state, plus an audit row carrying `result=CONFLICT` + `after_json.no_op=true` for forensic visibility. 404 is reserved for "no such id" (admin typo); CONFLICT is reserved for "id exists but already in the desired state."

### Sprint 7 retrospective

**What went well:**
- Plan-then-confirm rhythm held across all seven tasks. Each task got a detailed plan with decision rationale before any code; the plans accurately predicted what would touch which files. Notably, Task 1's plan correctly anticipated the `find_stale_active` index question (existing partial unique index suffices at target scale, no new index) and Task 3's plan correctly anticipated that the endpoint should orchestrate directly rather than route through `ShiftSessionService.end_by_id`.
- TDD discipline held on the read endpoints (Tasks 2, 3): pagination, filters, has_more, and the audit-of-audits shape all had explicit tests-first counterparts. Coverage stayed at 100% across every gate check.
- Reuse paid off: the `LIMIT N+1` pagination shape from Task 2's `AuditLogRepository.query` was carried 1:1 into Task 3's `ShiftSessionRepository.query`; the Task 1 `ShiftSessionService.end_by_id` primitive was reused (without modification) for Task 3's force-close endpoint orchestration; the `_helpers.py` extraction in Task 5 immediately simplified the existing `POST /devices/` callsite as a bonus refactor.
- The "Task 4(a) is already shipped" discovery was caught at the start of Task 4 by reading the actual code instead of trusting the plan. Saved ~half a day of redundant work and surfaced a small process lesson (sprint-plan claims about "what's deferred" can rot between plan-write and plan-execute when carried across two sprints).
- The Task 0 enum rename was risky-looking (live enum, used in domain + Pydantic + repo + 5+ tests, between Sprint 6 close and Sprint 7's downstream tasks) but landed cleanly thanks to the two-commit split (migration + tests first, code sweep second) — the intermediate state was acknowledged-and-safe rather than accidentally broken.
- Working-tree hygiene check at the start of Task 0 caught that Sprint 6 was uncommitted on main (despite the work-log claiming it had closed). Three pre-Task-0 commits (`feat(s6)`, `docs(s6) close-out`, `docs(s7) plan skeleton`) recovered honest history before Sprint 7 work landed on top.

**What slowed us down:**
- Test setup for the new integration test in Task 1 (`test_auto_end_job.py`) initially failed because it didn't bootstrap the schema — the migration test's module-scoped fixture leaves the DB at `base` between modules, so any new integration test file needs its own `_clean_schema` + `_truncate` setup. Mechanical but cost one cycle; documenting here so future new integration test files start from the right template.
- Three transient ruff issues across Sprint 7 commits: unicode `×` (multiplication sign) in code comments tripped RUF002/RUF003 twice (Task 1 + Task 2), and the ASYNC109 rule fired once on `_wait_or_cancel(event, timeout=...)` because the param name `timeout` conflicts with `asyncio.wait_for(timeout=...)` semantics. All three were trivial fixes (`x` instead of `×`; `wait_seconds` instead of `timeout`). Pattern to remember: avoid unicode in code, even comments.
- Black wanted reformatting in 4+ files at most gate runs — line-length collapses from inserting new code into existing modules. Always run `black` before claiming a gate is green.
- IDE noise (wrong Python interpreter → constant false "module not found" / "attribute not found" diagnostics) continued, same as every prior sprint. Not a real problem.

**Discrepancies between ToR / Architecture and what shipped:**
- **None new this sprint.** Sprint 6's enum-naming discrepancy was RESOLVED in Task 0; the Sprint 6 retrospective's addendum was committed as part of Task 0 commit #2. Sprint 7's surface (admin endpoints under `/api/v1/admin/`, audit query filters per ToR §8.3, force-close mandatory `reason`) matches ToR §4.4.2 / §5.4.6 / §7.2.4 / §8.3 as written.
- **Decision I trade-off acknowledged:** admin endpoints (`/admin/audit`, `/admin/sessions`, `/admin/sessions/{id}/force-close`) gate on active shift, but there's no admin-shift-open API yet. Live admin use returns 409 NO_ACTIVE_SHIFT until Sprint 8+. Not a divergence from ToR — `dcinv-admin` is an authorization role; shift-as-attribution is a Sprint 6 architectural choice and Sprint 7 follows it consistently.

**Deliberately deferred (carried into Sprint 8+):**
- **Admin-shift-open surface.** Without this, decision I's active-shift gate bricks admins in production. Sprint 8 candidate: `POST /api/v1/admin/sessions/start` (web-driven, no `tablet_id` — perhaps `workstation_id` or just the admin's keycloak id) so admins can attribute their own actions to a shift. Once it lands, `POST /api/v1/admin/batches/` can switch from un-gated to gated, completing the consistency story.
- **Multi-replica auto-end-job ownership.** Postgres advisory lock OR k8s CronJob. Sprint 8a (production hardening) candidate. Single-replica caveat documented.
- **NetBox circuit breaker** (Architecture §3.3 deferral, carried since Sprint 3). Sprint 8a.
- **Phase 2 partial-failure alerting** (Architecture §3.1, parking-lot). Sprint 8a.
- **Performance testing against ToR §5.1 targets** (QR lookup p95 ≤ 800ms, device update p95 ≤ 1500ms). Sprint 8a.
- **Rate limiting** per ToR §5.4.7. Sprint 8a.
- **Idempotency-key TTL cleanup job** — carried from Sprints 2-6, no consumer yet.
- **HTML admin web pages** per ToR §4.4.2 — `/web/`, `/web/batches/`, `/web/qr/search`, `/web/audit/`, `/web/users/`, `/web/sessions/`. Sprint 8b. Sprint 7 shipped the JSON foundations for `/web/audit/` (Task 2) and `/web/sessions/` (Task 3); HTML is a thin presentation layer on top.
- **PDF batch label generation** (Architecture §6 deliverable; ToR §4.4.2 Download/Print buttons). Sprint 8b.
- **CSV export for `GET /admin/audit`** (ToR §4.4.2 "Export to CSV"). Sprint 8b. Will be a separate endpoint (`GET /web/audit/export.csv` or similar), NOT content negotiation on the JSON endpoint — keeps the JSON contract pure.
- **`GET /api/v1/admin/users`** — backs ToR §4.4.2 `/web/users/`. Needs a Keycloak admin client + `KEYCLOAK_ADMIN_CLIENT_*` env vars (Sprint 6 decision J deliberately avoided). Sprint 8+ when the admin surface justifies the new attack surface.
- **`GET /api/v1/admin/qr/{id}/history`** — backs `/web/qr/search`. Task 2's `entity_id` audit filter partially covers this use case (`?entity_type=qr&entity_id=DCQR-XXX` gives the change history); a dedicated endpoint can land later if the join shape is awkward.
- **Manual smoke against real Keycloak / NetBox** — skipped (no reachable instances in this environment, same as Sprints 1-6).

### Files added in Sprint 7 (high-level)

- `backend/alembic/versions/d4e5f6a7b8c9_rename_shift_end_reason_to_tor_canon.py` (Task 0) — migration
- `backend/app/services/auto_end_job.py` (Task 1) — `AutoEndJobStatus`, `auto_end_loop`, `_run_iteration`
- `backend/app/api/v1/admin/audit.py` (Task 2) — `GET /admin/audit` router + Pydantic envelopes + audit-of-audits orchestration
- `backend/app/api/v1/admin/sessions.py` (Task 3) — `GET /admin/sessions` + `POST /admin/sessions/{id}/force-close` router
- `backend/app/api/v1/_helpers.py` (Task 5) — `netbox_validation_error_response` translator
- `backend/tests/integration/test_auto_end_job.py` (Task 1, 2 tests) — end-to-end stale-row sweep against real DB
- `backend/tests/integration/test_main_lifespan.py` (Task 1, 3 tests) — lifespan task wiring + shutdown drain
- `backend/tests/integration/test_audit_log_repository.py` (Task 2, 12 tests) — `query` method filters + pagination + ordering
- `backend/tests/integration/test_admin_audit.py` (Task 2, 3 tests) — end-to-end pagination + audit-of-audits queryable
- `backend/tests/integration/test_admin_sessions.py` (Task 3, 5 tests) — list filters + force-close happy/no-op/404 paths
- `backend/tests/unit/services/test_auto_end_job.py` (Task 1, 18 tests) — status state machine + loop guardrails + `_run_iteration` row-level exception handling
- `backend/tests/unit/api/v1/test_admin_audit.py` (Task 2, 14 tests) — handler logic + role/active-shift gating + OpenAPI semantic-note check
- `backend/tests/unit/api/v1/test_admin_sessions.py` (Task 3, 15 tests) — list + force-close handler logic + body validation
- `backend/tests/unit/api/v1/test_helpers.py` (Task 5, 3 tests) — NBV translator + fallback message contract

### Files modified in Sprint 7

- `backend/alembic/versions/c3d4e5f6a7b8_shift_sessions.py` — **NOT modified** (kept Sprint 6's original labels so historical replay is honest; Task 0's rename is a separate migration on top)
- `backend/app/domain/shift_session.py` (Task 0) — `ShiftEndReason` members renamed to `MANUAL / AUTO_TIMEOUT / FORCED`
- `backend/app/domain/audit.py` (Task 2) — `+AuditLogEntry.id: int | None = None` (Sprint 2's reserved field now active for the read path)
- `backend/app/api/v1/sessions.py` (Task 0) — wire `Literal["manual", "auto_timeout"]` + docstring sweep
- `backend/app/services/shift_session.py` (Tasks 0 + 1) — docstring sweep (Task 0) + `+ShiftSessionNotFound` + `+end_by_id` (Task 1)
- `backend/app/db/repositories/shift_session.py` (Tasks 1 + 3) — `+find_stale_active` (Task 1), `+ShiftSessionQueryFilters` + `query` (Task 3)
- `backend/app/db/repositories/audit_log.py` (Task 2) — `+AuditLogQueryFilters` + `query` returning `(rows, has_more)`
- `backend/app/api/v1/health.py` (Task 1) — `+auto_end_job` sub-object on `/health` response; new `_auto_end_job_sub_object(request)` helper
- `backend/app/main.py` (Tasks 1 + 2 + 3) — lifespan extended with the auto-end task scheduling + `app.state.auto_end_job_status` (Task 1); `+admin_audit_router` mount (Task 2); `+admin_sessions_router` mount (Task 3)
- `backend/app/api/v1/devices.py` (Tasks 4 + 5) — `reason` passed through to `patch_with_attribution` (Task 4); NBV catch added to `update_device`, `add_comment`, `decommission_device`; `POST /devices/` refactored to use the shared helper (Task 5)
- `backend/app/api/v1/qr.py` (Task 5) — NBV catch added to `bind_qr` and `retire_qr`
- `backend/app/services/netbox_write.py` (Task 4) — `_format_journal_comment` + `_post_journal_entry` + `patch_with_attribution` gain optional `reason: str | None`
- `backend/app/services/device_decommission.py` (Task 4) — `reason=reason` passed to `patch_with_attribution`; docstring updated
- `backend/app/config.py` + `backend/.env.example` (Task 1) — three new `SHIFT_AUTO_END_*` knobs + docs
- 5 unit test files for endpoint NBV catches (Task 5)
- 4 integration test files (Tasks 4 + 5) — decommission journal-body assertion (Task 4); end-to-end NetBox 400 → 422 + FAILURE audit row (Task 5, +1 updated existing 502 → 422 contract on add-comment)
- `backend/tests/unit/test_config.py` (Task 1) — 4 new tests for `SHIFT_AUTO_END_*` defaults + override + zero-rejection
- `backend/tests/unit/services/test_shift_session.py` (Tasks 0 + 1) — sweep + 5 new `end_by_id` tests + 1 new `ShiftSessionNotFound` exception payload test
- `backend/tests/unit/services/test_netbox_write.py` (Task 4) — 3 new tests: format-helper reason-present, reason-omitted, end-to-end `patch_with_attribution` reason → journal body
- `backend/tests/unit/domain/test_shift_session.py` (Task 0) — sweep
- `backend/tests/unit/domain/test_audit.py` (Task 2) — 2 new `id`-field tests (defaults None, populated on read)
- `backend/tests/integration/test_shift_sessions_migration.py` (Task 0) — label-list assertion + insert string updated; `test_downgrade_drops_shift_sessions_table_and_enum_type` re-pointed at the explicit pre-shift_sessions revision `b2c3d4e5f6a7`; `+test_rename_migration_rewrites_pre_rename_rows_in_place`
- `backend/tests/integration/test_shift_session_repository.py` (Tasks 0 + 1 + 3) — sweep (Task 0); 4 new `find_stale_active` tests (Task 1); 8 new `query` tests (Task 3)
- `docs/sprint-7.md` — per-task plans filled in across the sprint (this work-log entry is the close-out artifact)
- `docs/parking-lot.md` — admin sessions surface + audit_log.session_id semantic notes marked resolved
- `CLAUDE.md` — Repository Status updated for Sprint 7 closure

### How to run locally (close-of-sprint snapshot)

Unchanged from Sprint 6 in shape — same `docker-compose.test.yml` for the test DB, same env vars (`DATABASE_URL`, `NETBOX_URL`, `NETBOX_SERVICE_TOKEN`, `KEYCLOAK_BASE_URL`). Sprint 7 adds three optional `SHIFT_AUTO_END_*` knobs; defaults work for local dev (job enabled, 5-minute interval, 12-hour threshold). To disable the auto-end loop in a local one-off run (e.g. inspecting `/health` without the loop scheduled):

```bash
SHIFT_AUTO_END_ENABLED=false uvicorn app.main:app --reload
```

The new admin endpoints require an active shift for the calling user. The conftest fixtures handle this in tests via `seed_default_active_shift`; for local manual smoke, call `POST /api/v1/sessions/start` first (any role can technically open a shift; the gate is the role on the downstream admin endpoint).

---

## Sprint 8a — Production Hardening (closed 2026-06-03)

**Status:** Closed. Tasks 0–4 complete.

### What shipped

| Task | Deliverable |
|---|---|
| 0 | **Admin-shift-open API + `POST /admin/batches/` gating + `QRGenerationService` audit-row session_id source swap.** New `POST /api/v1/admin/sessions/start` (role `dcinv-admin` only — chicken-and-egg: can't require a shift to open one) with body `{workstation_id: str(1..255)}`. Distinct `AdminSessionStartRequest` Pydantic model from mobile's `SessionStartRequest` even though both write to `shift_sessions.tablet_id`; the API layer renames at the schema boundary so admin (`workstation_id`) and mobile (`tablet_id`) surfaces stay semantically distinct. `POST /admin/batches/` + `GET /admin/batches/{id}` switched from `require_role` to `require_role_with_active_shift("dcinv-admin")` — all of `/api/v1/admin/*` now gate consistently per Sprint 7 decision I. `QRGenerationService` audit row's `session_id` source swapped from hardcoded `None` → `user.shift_session_id`. Pre-Sprint-8a batch rows retain `session_id NULL` (consistent with Sprint 6 decision D "no historical migration"); NEW rows carry the admin's shift id. Unblocks live use of every Sprint 7 admin endpoint (`/admin/audit`, `/admin/sessions`, `/admin/sessions/{id}/force-close`). |
| 1 | **Multi-replica auto-end-job ownership via Postgres advisory lock.** `_run_iteration` body wrapped in `pg_try_advisory_lock(<id>)` on a separate "ceremony" session. Lock-skip returns 0 without raising → outer `auto_end_loop`'s "bump `last_iteration_at` if no exception" semantic naturally treats lock-skip as a successful tick; lock-loser replicas do NOT flip to `"stale"` on `/health`. Lock id is `int.from_bytes(sha256(b"dcinv:auto_end_job").digest()[:8], "big", signed=True)` — stable, greppable, operator-inspectable via `SELECT * FROM pg_locks WHERE locktype='advisory' AND objid=<id>`. Sprint 7's single-replica caveat removed from `app/main.py` code comment + `docs/parking-lot.md` entry marked RESOLVED. Per-iteration try/except + per-row session/tx semantics from Sprint 7 unchanged. |
| 2 | **NetBox circuit breaker (Architecture §3.3, deferred since Sprint 3).** Module-level lazy-initialised `CircuitBreaker(expected_exception=(NetBoxServerError, NetBoxTimeout), name="netbox")` from the `circuitbreaker` PyPI package. `NetBoxNotFound` (404) and `NetBoxValidationError` (4xx) are NOT counted — they mean "NetBox said your request is wrong," not "NetBox is broken." `_send` split into public open-check + delegation + new `_send_impl` (original retry loop). New typed `NetBoxCircuitOpenError(NetBoxClientError)` carrying `recovery_timeout_seconds`. `main.py` exception handler returns 503 + `Retry-After: N` header + `{"error":{"code":"NETBOX_CIRCUIT_OPEN","retry_after_seconds":N}}`. Distinguished from the existing 502 `NetBoxClientError` handler: 502 = "I asked NetBox and got a bad response"; 503 = "I'm refusing to call NetBox because it's been failing." Handler registered BEFORE the broader `NetBoxClientError` handler so FastAPI's most-specific dispatch routes correctly. `/health` extended with `netbox_circuit:{enabled,state,failure_count,open_until}` sub-object — **informational only**; the existing `_check_netbox` probe (uses a fresh `httpx.AsyncClient`, bypasses the circuit) remains the 503 trigger. Three new `Settings` knobs (`NETBOX_CIRCUIT_ENABLED`/`_FAILURE_THRESHOLD`/`_RECOVERY_TIMEOUT_SECONDS`); module-level `reset_netbox_circuit()` helper for tests added to the `clean_env` fixture. |
| 3 | **Per-user rate limiting (ToR §5.4.7).** FastAPI middleware enforces fixed-window budgets across three endpoint classes — READ (60/min default), WRITE (20/min), ADMIN (30/min) — plus UNLIMITED bypass for `/health`, `/docs`, `/openapi.json`, `/redoc`. Classification: `/api/v1/admin/*` → ADMIN regardless of method; GET/HEAD/OPTIONS → READ; POST/PATCH/PUT/DELETE → WRITE. Module-level `_buckets: dict[(sub, class, window_index), int]`. **Per-replica state**, NOT cluster-wide (decision F) — Sprint 9+ adds Redis-backed cross-replica counters. User identity extracted via `jwt.get_unverified_claims()` — rate-limit keying does NOT need full signature verification (that happens later in `require_role`); saves a JWKS lookup per request. Unauthenticated requests bypass rate limiting and 401 at auth. 429 response: `Retry-After: <seconds>` header + `{"error":{"code":"RATE_LIMIT_EXCEEDED","retry_after_seconds":N}}` body (shape mirrors Task 2's 503). Middleware registered BEFORE `request_id_middleware` in source order so request_id ends up OUTER (Starlette applies user_middleware in reverse-registration order) and structlog contextvars are bound when the rate-limit middleware logs a 429. Four new `Settings` knobs; `reset_rate_limit_buckets()` test helper added to `clean_env`. |
| 4 | Acceptance + close-out + performance baselines (this entry). |

### Quality bar at close

- **881 tests** (Sprint 7 → 802; Sprint 8a +79 net new), **100% line + branch coverage** across `app/` — `--cov-fail-under=100` gate passes.
- ruff + black + mypy clean across `app/` and `tests/`.
- One new pyproject dependency (`circuitbreaker>=2.0,<3`); `uv.lock` regenerated.
- One new module package (`app/middleware/`) + one new module (`app/services/auto_end_job.py` already existed; Task 1 modified it).

### Pyproject deviations from baseline

**`circuitbreaker>=2.0,<3` added under `[project.dependencies]`** (Sprint 8a Task 2 — first pyproject deviation since Sprint 1). Pure-Python, no transitive deps. Pre-approved at Sprint 8 plan stage (decision E). Justification: rolling our own circuit breaker would be ~150 lines of state-machine code with subtle timing concerns around the OPEN → HALF_OPEN transition under concurrency; the PyPI package is ~200 LOC, well-tested, and integrates as a one-line decorator. The "no new deps" Sprint 1 stance was about avoiding heavy frameworks (APScheduler, Celery); a small focused package for a specific resilience pattern is the kind of dep that pays for itself. Mypy override added for the (untyped) module next to the existing `jose` / `yaml` overrides.

### Architectural decisions worth carrying forward

- **Backend is now multi-replica safe for the auto-end job (Task 1).** `pg_try_advisory_lock` on `_AUTO_END_JOB_ADVISORY_LOCK_ID` (sha256 of `b"dcinv:auto_end_job"` truncated to bigint) ensures only one replica runs the work per interval. Lock-loser replicas tick cleanly. Sprint 7's single-replica caveat is REMOVED.
- **Admin actions now have shift attribution (Task 0).** Every `/api/v1/admin/*` endpoint requires an active shift (decision I held from Sprint 7); admins open shifts via `POST /api/v1/admin/sessions/start`. All future admin endpoints should follow this pattern. `POST /api/v1/admin/batches/` is no longer the lone un-gated outlier — Sprint 7 decision F is RESOLVED.
- **NetBox circuit breaker is the resilience boundary (Task 2).** Service-layer code can rely on the client to fast-fail when NetBox is hosed; no need for per-service retry exhaustion logic. The new `NetBoxCircuitOpenError → 503` semantic distinguishes "upstream's broken (I refuse to escalate)" from `NetBoxClientError → 502` ("upstream answered badly"). Future write endpoints get this behavior for free — they already let `NetBoxClientError` propagate to `main.py`'s handler.
- **Per-user rate limiting at the middleware layer (Task 3).** Classification by path + method (no per-endpoint decorators); state is in-process and per-replica. For cluster-wide rate limits in Sprint 9+, the natural extension is replacing `_buckets` with a Redis / Postgres backend behind the same `_consume` interface — the middleware itself doesn't need to change.
- **`circuitbreaker` PyPI dep precedent.** First dep added since Sprint 1. The bar to clear: small + focused + well-tested + no transitive deps. Future sprints can add similar single-purpose deps without re-establishing the precedent, but heavy frameworks (APScheduler, Celery, Redis client libraries) still need explicit plan-stage approval.
- **`/health` sub-objects are informational, NOT 503 triggers.** Both Task 1's `auto_end_job` and Task 2's `netbox_circuit` follow Sprint 7's pattern: surface state for operators to scrape, but keep the existing 503 trigger logic (db / netbox / keycloak reachability) narrow.
- **JWT `sub` extraction without verification at the middleware layer (Task 3 decision 4).** Rate-limit keying doesn't need full signature verification — that's `require_role`'s job. Saves a JWKS lookup per request. A forged `sub` lets an attacker mess with their own bucket; real auth still rejects them downstream. Useful pattern for any future middleware that needs user identity for bookkeeping but not security.
- **Test conftest fixture composition.** `clean_env` now resets four things across two sprints: settings + engine + sessionmaker + NetBox client (Sprint 7) → adds NetBox circuit + rate-limit buckets (Sprint 8a). Future module-level state should follow the same `reset_*()` helper + clean_env-injection pattern.

### Sprint 8a retrospective

**What went well:**
- Plan-then-confirm rhythm held across all 5 tasks. Each task had a detailed plan with decision rationale before any code; plans accurately predicted what would touch which files. Task 0's "rename Pydantic field at API boundary, keep DB column as-is" call paid off for backwards compat. Task 1's "lock on ceremony session, work on per-row sessions" preserved Sprint 7's per-row-isolation invariant cleanly. Task 2's `expected_exception=(NetBoxServerError, NetBoxTimeout)` decision (NetBoxNotFound + Validation NOT counted) was the most non-obvious circuit-breaker design choice and was caught at plan time.
- Reuse paid off: Task 0 reused `ShiftSessionService.start` 1:1 (just a different wire shape); Task 3's `_buckets` dict + `clean_env` reset hook mirrors Task 2's `reset_netbox_circuit()` from earlier in the sprint, which itself mirrors Sprint 7's pattern. The `/health` sub-object shape is now consistent across `auto_end_job` and `netbox_circuit`.
- TDD discipline held. Coverage stayed at 100% through every gate check. The dep-injection patterns from Sprint 7 (FastAPI `dependency_overrides`, `clean_env` cache-clearing) carried over without modification.
- The `circuitbreaker` API discovery was done up-front (`uv run python -c "..."` to inspect public methods) before drafting Task 2's implementation steps. Saved a probable second-pass refactor when the package's actual API didn't match initial assumptions about `call_async` vs decorator semantics.
- The Task 4 perf baseline measure-and-document approach (decision I) hit both targets comfortably — QR lookup p95 = 8.4ms vs 800ms target; PATCH device p95 = 14.3ms vs 1500ms target. The development-loop conditions (test Postgres + respx-mocked NetBox + single asyncio loop, no concurrent load) make these "well under floor" numbers, not "production acceptance" claims; documented clearly.

**What slowed us down:**
- Task 3's integration tests initially failed in pytest's default-order run because (a) the cached NetBox client leaks event-loop binding across tests (same Sprint 6 friction), and (b) the `/health` endpoint reads `app.state.auto_end_job_status` which only exists if the FastAPI lifespan has run — and `AsyncClient` + `ASGITransport` does NOT trigger lifespan. Dropped the dedicated `/health bypass` integration test and rely on the UNLIMITED-classification parametrize for the contract; documented the trade-off in commit `df36ce8`.
- IDE noise (wrong Python interpreter path → constant false "module not found" / "attribute not found" diagnostics) continued, same as every prior sprint. Not a real problem.
- Two ruff RUF002/RUF003 hits on `×` (multiplication sign) and one ASYNC109 on a `timeout` parameter name. Pattern carried forward from Sprint 7 — avoid unicode `×` in docstrings + don't name parameters `timeout` if they wrap `asyncio.wait_for(timeout=...)`. Cost was minutes-per-fix.
- Mypy unhappiness with `circuitbreaker` (no type stubs) and `jose.jwt.encode` (returns Any). Fixed by adding a mypy override for the package (mirrors `jose` / `yaml`) + explicit `cast()` on the `call_async` return + local `str` annotation on `jwt.encode` calls in tests.

**Discrepancies between ToR / Architecture and what shipped:**
- **None new this sprint.** Sprint 8a delivered exactly what the plan called for; no ToR §5.1 / §5.4.7 / Architecture §3.3 divergence. The rate-limit storage being per-replica (not cluster-wide) is a deliberate choice documented as Sprint 9+ work, not a divergence from ToR.

**Deliberately deferred (carried into Sprint 8b / Sprint 9+):**
- **Cluster-wide rate-limit state** (Sprint 9+). In-process per-replica state is sufficient at single-replica deployment scale; Redis or Postgres-backed counters land when the deployment goes multi-replica.
- **Phase 2 partial-failure alerting** (Architecture §3.1, parking-lot, deferred since Sprint 3). Depends on operational monitoring infra (Prometheus / Loki / etc.) not yet in place; backend-side, `/health`'s `netbox_circuit` + `auto_end_job` sub-objects provide enough state for an external scraper.
- **Manual smoke against real Keycloak / NetBox** — skipped (same as every prior sprint; environment-blocked).
- **HTML admin web pages + dashboard counters + PDF labels + CSV export + `GET /admin/qr/{id}/history`** — Sprint 8b. Sprint 7 + Sprint 8a together ship the complete JSON foundation; the HTML layer can be a thin presentation layer on top.
- **`GET /api/v1/admin/users`** — Sprint 8b or later; needs a Keycloak admin client + `KEYCLOAK_ADMIN_CLIENT_*` env vars (Sprint 6 decision J deliberately avoided).
- **Idempotency-key TTL cleanup job** — pre-existing carry-over from Sprints 2-7, no consumer yet.

### Performance baselines

Measure-and-document one-shot, NOT CI-wired (decision I). Conditions: in-process ASGI client + test Postgres (Docker, port 5433) + respx-mocked NetBox + single asyncio loop + N=100 iterations + rate limiting / circuit breaker / auto-end loop all disabled via env. Run on the developer workstation, not production-like infra.

| Endpoint | ToR §5.1 p95 target | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| `GET /api/v1/qr/{qr_id}` | ≤ 800ms | 6.1ms | 8.4ms | 13.8ms | 14.5ms |
| `PATCH /api/v1/devices/{id}` | ≤ 1500ms | 10.8ms | 14.3ms | 16.6ms | 26.3ms |

Both endpoints come in **well under** ToR §5.1 targets in development-loop conditions — QR lookup at ~1% of budget, device update at ~1% of budget. These are floor numbers; production-like infra (real NetBox latency, network hops, concurrent load) will be substantially higher. Operators should re-run against production-like infra for acceptance.

Re-run via `cd backend && uv run python scripts/perf_baseline.py` (env vars per the script's docstring).

### Files added in Sprint 8a (high-level)

- `backend/app/middleware/__init__.py` + `backend/app/middleware/rate_limit.py` (Task 3) — middleware package + per-user rate limiter
- `backend/app/netbox/errors.py` is modified (not added), but `NetBoxCircuitOpenError` is new
- `backend/tests/unit/middleware/__init__.py` + `backend/tests/unit/middleware/test_rate_limit.py` (Task 3, 39 tests)
- `backend/tests/integration/test_circuit_breaker.py` (Task 2, 1 end-to-end test)
- `backend/tests/integration/test_rate_limit.py` (Task 3, 5 end-to-end tests)
- `backend/scripts/perf_baseline.py` (Task 4) — operator-runnable, not CI-wired

### Files modified in Sprint 8a

- `backend/pyproject.toml` (Task 2) — `+circuitbreaker>=2.0,<3` dep + mypy override
- `backend/uv.lock` (Task 2) — regenerated
- `backend/.env.example` (Tasks 2 + 3) — 3 `NETBOX_CIRCUIT_*` + 4 `RATE_LIMIT_*` knobs documented
- `backend/app/config.py` (Tasks 2 + 3) — 7 new Settings fields
- `backend/app/main.py` (Tasks 1 + 2 + 3) — single-replica caveat removed (Task 1); `NetBoxCircuitOpenError → 503` handler added before the broader `NetBoxClientError` handler (Task 2); rate-limit middleware registered BEFORE `request_id_middleware` in source order (Task 3)
- `backend/app/api/v1/health.py` (Task 2) — `netbox_circuit` sub-object on `/health`
- `backend/app/api/v1/admin/sessions.py` (Task 0) — `+AdminSessionStartRequest`, `+POST "/start"` handler, `+get_shift_session_service` DI factory
- `backend/app/api/v1/admin/batches.py` (Task 0) — `require_role` → `require_role_with_active_shift("dcinv-admin")` on both endpoints
- `backend/app/services/auto_end_job.py` (Task 1) — `+_AUTO_END_JOB_ADVISORY_LOCK_ID`, `+_do_iteration_work` (extracted), `_run_iteration` wraps with `pg_try_advisory_lock` + `_send_impl`/`_send` split parallels Task 2's pattern
- `backend/app/netbox/client.py` (Task 2) — module-level circuit + `_send_impl`/`_send` split + lazy init via `_get_netbox_circuit()` + `reset_netbox_circuit()` + `get_netbox_circuit_state()`
- `backend/app/netbox/errors.py` (Task 2) — `+NetBoxCircuitOpenError(NetBoxClientError)`
- `backend/app/services/qr/generation.py` (Task 0) — audit row `session_id=None` → `session_id=user.shift_session_id`
- `backend/tests/conftest.py` (Tasks 2 + 3) — `clean_env` calls `reset_netbox_circuit()` + `reset_rate_limit_buckets()`
- `backend/tests/unit/test_config.py` (Tasks 2 + 3) — 9 new tests (4 NETBOX_CIRCUIT_*, 5 RATE_LIMIT_*)
- `backend/tests/unit/services/test_auto_end_job.py` (Task 1) — `_FakeAsyncSession` learned `.execute()` for the advisory-lock SELECTs; new lock-held-elsewhere test
- `backend/tests/unit/netbox/test_client.py` (Task 2) — 8 new circuit-breaker tests
- `backend/tests/unit/api/v1/test_health.py` (Task 2) — 3 new tests for `netbox_circuit` sub-object
- `backend/tests/unit/api/v1/test_admin_sessions.py` (Task 0) — 8 new tests for `POST /admin/sessions/start`
- `backend/tests/unit/api/v1/test_batches.py` (Task 0) — 2 new tests (audit row shift_session_id; 409 NO_ACTIVE_SHIFT path)
- `backend/tests/integration/test_admin_sessions.py` (Task 0) — 2 new end-to-end tests (admin opens shift → uses /admin/audit + creates batch with attributed audit row)
- `backend/tests/integration/test_auto_end_job.py` (Task 1) — new concurrent-call test (two `_run_iteration`s; exactly one ends rows)
- `docs/sprint-8.md` — per-task detail filled in inline as we executed each task; this work-log entry is the close-out artifact
- `docs/parking-lot.md` — "Multi-replica auto-end-job ownership" marked RESOLVED in Task 1
- `CLAUDE.md` — Repository Status updated for Sprint 8a closure

### How to run locally (close-of-sprint snapshot)

Same shape as Sprint 7. Sprint 8a adds 7 new optional env knobs (3 `NETBOX_CIRCUIT_*` + 4 `RATE_LIMIT_*`); defaults work for local dev. To run the app with all the new safety nets disabled (e.g. for one-off perf measurement):

```bash
RATE_LIMIT_ENABLED=false NETBOX_CIRCUIT_ENABLED=false SHIFT_AUTO_END_ENABLED=false \
  uvicorn app.main:app --reload
```

Admins need to open a shift before using `/api/v1/admin/*` endpoints (including `/admin/batches/`):

```bash
curl -X POST -H "Authorization: Bearer <admin-jwt>" -H "Content-Type: application/json" \
  -d '{"workstation_id":"admin-ws-01"}' http://localhost:8000/api/v1/admin/sessions/start
```

To re-run the performance baseline:

```bash
cd backend
DATABASE_URL='postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test' \
  NETBOX_URL='https://netbox.example.com' \
  NETBOX_SERVICE_TOKEN='test-token' \
  KEYCLOAK_BASE_URL='https://sso.example.com' \
  uv run python scripts/perf_baseline.py
```

---

## Sprint 8b — User-Facing Deliverables (closed 2026-06-04)

**Status:** Closed. Tasks 0–4 complete; Task 5 (this entry) closes the sprint.

### What shipped

| Task | Deliverable |
|---|---|
| 0 | **Web auth foundation + template scaffolding.** Keycloak OIDC authorization-code redirect flow at `GET /web/{login,oidc/callback,logout}` against a confidential client (`KEYCLOAK_WEB_CLIENT_ID`/`_SECRET`); identity stored in a Fernet-encrypted `dcinv_admin_session` cookie (`SESSION_COOKIE_KEY` is a Fernet key — fail-fast if missing at startup); cookie payload is identity-only (`sub`, `email`, `roles`, `exp`) with an 8-hour lifetime. New `app/web/auth.py` with `WebAdminUser` frozen-slots dataclass + `WebAdminAuthRequired` / `AdminShiftNeeded(user)` typed exceptions; `require_web_admin` FastAPI dep mirrors Sprint 7's `require_role_with_active_shift("dcinv-admin")` but on the cookie path. Non-admin users get the same response as no-cookie (redirect to login; no information leak between "wrong cookie" and "wrong role"). Authenticated admin with no active shift → renders an intermediate `_admin_shift_needed.html` page with a "Start admin shift" form posting to Sprint 8a's `POST /api/v1/admin/sessions/start`. New Jinja2 `_base.html` (nav + flash slot), hand-written `static/admin.css` (~150 lines, no preprocessor, no framework). Rate-limit middleware (`app/middleware/rate_limit.py`) extended with `_UNLIMITED_PREFIXES = ("/web/", "/static/")` so the admin browser doesn't double-fire against the per-user ADMIN bucket when a web page calls the same data path internally. Three new `Settings` fields. |
| 1 | **`/web/` dashboard + counters endpoint.** New `app/domain/dashboard.py` (`DashboardSnapshot` frozen dataclass — six counters + `generated_at`) + `app/db/repositories/dashboard.py` (`DashboardRepository.snapshot(*, now)` issues one SELECT with six scalar subqueries — verified via SQLAlchemy `after_cursor_execute` listener in the integration suite). New `GET /api/v1/admin/dashboard` JSON endpoint gated on `dcinv-admin` + active shift; **no audit row** (operational read parallels Sprint 7 decision 8 for `/admin/sessions`). The `/web/` placeholder from Task 0 replaced with the real dashboard.html template rendering a card grid + "As of {{ generated_at }} UTC" freshness line. Web handler consumes the repo directly via dep injection (decision I); no HTTP self-call. Task 1 also retroactively closed a 99.39% → 100% coverage gap left by Task 0's close-out (OIDC failure branches were untested; backfilled via direct-await unit tests). |
| 2 | **`/web/batches/` list + detail + PDF batch labels.** New `app/services/pdf_labels.py::render_batch_labels_pdf` is a pure function (bytes in → bytes out) rendering A4 landscape, 8×4 = 32 labels per page via `reportlab.graphics.barcode.qr.QrCodeWidget` (no separate `qrcode`/`pillow` dependency path). `pageCompression=0` keeps caption text greppable in the raw PDF byte stream so unit tests assert page count + caption rendering without a PDF-parser dep. Repository extensions: `QRBatchRepository.query` (newest-first pagination with `LIMIT page_size + 1` `has_more` — same shape as `AuditLogRepository.query`) and `QRCodeRepository.count_by_status_for_batch` (single GROUP BY for the detail page's status chips). New endpoints: `GET /api/v1/admin/batches/` (list, no audit row); `GET /api/v1/admin/batches/{id}/labels.pdf` (PDF via `Response(content=..., media_type="application/pdf")` with `Content-Disposition: attachment` header; **no audit row** — same data as the JSON detail endpoint, not a §5.4.6 sensitive read). The synchronous reportlab call runs via `asyncio.to_thread(...)` so the event loop stays responsive. New templates `batches/list.html`, `batches/detail.html`, and a shared `_not_found.html` (custom HTML 404 page reused across web detail handlers — web flows render HTML, not JSON detail bodies). |
| 3 | **`/web/audit/` list + detail + CSV export.** New `GET /api/v1/admin/audit/csv` returns `StreamingResponse(_csv_iter(rows), media_type="text/csv")` — DECISION D1 DEVIATION FROM PLAN SKELETON: endpoint is at `/api/v1/admin/audit/csv` (under the existing router prefix) rather than the plan's `/api/v1/admin/audit.csv`. Cleaner FastAPI routing without colliding with the `{audit_id}` matcher; explicitly registered before `{audit_id}` so it wins precedence. CSV uses the same 8 filters as Sprint 7's `/admin/audit` JSON endpoint; `page_size` default 1000, cap 10000 (vs JSON's 100). JSONB columns serialised compact via `json.dumps(..., separators=(",", ":"))`; datetimes use `isoformat()`. **Audit-of-audits row written** with `operation="audit.export_csv"`, `entity_type="audit"`, `entity_id="export"`, and `after_json["rows_exported"]` count — CSV export IS a sensitive read per ToR §5.4.6. Failure-path audit row (`result=FAILURE`) preserved before the exception propagates, matching Sprint 7's `/admin/audit` pattern. Web pages: `GET /web/audit/` (filter form with 8 fields + paginated table + Download CSV button; filter query string preserved into both pagination links and the CSV download link), `GET /web/audit/{audit_id}` (`<pre>{{ before_json \| tojson(indent=2) }}</pre>` pretty-printed JSON, custom 404 via the shared `_not_found.html`). Repo addition: `AuditLogRepository.get_by_id` for the detail page. |
| 4 | **`/web/sessions/` + inline force-close form.** New `GET /web/sessions/` paginated list (filter form mirrors the 4-filter JSON endpoint; active-shift rows highlighted) with **per-row inline `<form method="post">`** containing the `reason` textarea + Force-close button. Ended rows show an end-reason badge instead. New `POST /web/sessions/{id}/force-close` handler delegates to Sprint 7's existing JSON `force_close_session` via direct Python call (NOT HTTP self-call) so the three-record-write apparatus + 404 / idempotent-already-ended semantics stay in one place. 303-redirects back to the list with `?flash=Shift+force-closed&flash_kind=info` on success or `?flash=Shift+not+found&flash_kind=error` on 404; non-404 `HTTPException`s re-raise. Audit attribution: the web handler looks up the admin's own active shift in a FRESH session (via `get_sessionmaker()()`) before delegating, so the audit row credits the right shift — the existing session can't be used for the lookup because `force_close_session` opens its own `session.begin()` and SQLAlchemy 2.0 errors with "A transaction is already begun" if the injected session already auto-started one. Filter context (`user_keycloak_id`/`from`/`to`/`active_only`) is echoed via hidden inputs into the form body so the redirect carries the operator's filter context. New `sessions/list.html` template; CSS additions: `.end-reason-badge--{manual,auto_timeout,forced}` chips, `.inline-form` compact textarea, `.session-row--active` highlight. |
| 5 | Acceptance + close-out (this entry). |

### Quality bar at close

- **1018 tests** (Sprint 8a → 881; Sprint 8b +137 net new), **100% line + branch coverage** across `app/` — `--cov-fail-under=100` gate passes.
- ruff + black + mypy clean across `app/` and `tests/`.
- Three new pyproject dependencies (`jinja2`, `reportlab`, `python-multipart`); `uv.lock` regenerated three times.
- Three new module/template directories (`app/web/templates/{batches,audit,sessions}/`), one new module (`app/services/pdf_labels.py`), two new domain modules (`app/domain/dashboard.py` only; the rest reused existing).

### Pyproject deviations from baseline

Three new dependencies added under `[project.dependencies]` this sprint:

1. **`jinja2>=3.1,<4`** (Task 0). FastAPI's documented standard for HTML templating. Pre-approved at Sprint 8b plan stage (decision J). Explicit pin avoids transitive surprises if FastAPI ever drops the transitive dep.
2. **`reportlab>=4.0,<5`** (Task 2). Pure-Python PDF generation, no system deps; `reportlab.graphics.barcode.qr.QrCodeWidget` renders QR codes natively so no separate `qrcode` dependency is needed. Pre-approved at Sprint 8b plan stage (decision F). Pulled transitive `pillow` + `charset-normalizer` (both required by reportlab; no alternative short of replacing reportlab with `qrcode` + `pillow` independently). Mypy override `module = "reportlab.*"` added next to existing `jose` / `yaml` / `circuitbreaker` overrides.
3. **`python-multipart>=0.0.20,<0.1`** (Task 4). FastAPI's documented standard for parsing `application/x-www-form-urlencoded` request bodies; required by the `Form(...)` parser used by `POST /web/sessions/{id}/force-close`. Pure-Python, ~5 KB, MIT. **NOT pre-approved at plan stage** — surfaced as a runtime error mid-Task 4 when the first integration test ran (`RuntimeError: Form data requires "python-multipart" to be installed`). Execution-time approval recorded in the Task 4 commit body and here. Lesson: future sprints touching HTML forms should audit FastAPI's documented helper deps at plan stage.

### Architectural decisions worth carrying forward

- **Web auth = OIDC cookie, separate from mobile JWT bearer.** `WebAdminUser` is a distinct dataclass from `AuthUser` because the cookie carries identity only — no JWT context, no `sid`, no `realm_access` re-validation per request. The JWT bearer path (`app/auth/`) does full JWKS verification for inbound mobile API calls; the cookie path trusts its own freshly-completed OIDC handshake.
- **Cookie is Fernet-encrypted, not signed-only** (CLAUDE.md mandate). `cryptography.fernet.Fernet` provides authenticated encryption. `SESSION_COOKIE_KEY` is a required Settings field (fail-fast at startup if missing) — operators generate via `Fernet.generate_key()` once at deploy and persist. The lazy-singleton `_fernet()` cached at module level is cleared by `reset_web_auth_cache()` in tests, mirroring Sprint 8a's `reset_netbox_circuit()` / `reset_rate_limit_buckets()` pattern.
- **Web pages consume repos DIRECTLY via dep injection** (decision I, locked in across all four web pages this sprint). Pages do NOT self-HTTP-call the JSON endpoints. Two consequences: (a) the per-user ADMIN rate-limit bucket fires once per browser page-view (not twice), (b) the dep graph stays Pythonic — refactors don't cross HTTP boundaries. `/web/*` and `/static/*` are UNLIMITED in the rate-limit middleware to make the design explicit.
- **HTML 404 pages, not JSON `{detail: "..."}` bodies.** The shared `_not_found.html` template is reused across `/web/batches/{unknown}` and `/web/audit/{unknown}` so the operator gets a navigable page, not a stack-trace-shaped JSON body. Web flows redirect or render HTML; they never surface JSON to the browser.
- **Audit-row policy on the web layer:** the four web pages (dashboard, batches list/detail, audit list/detail, sessions list) write NO audit rows themselves. The JSON endpoints they consume already audit per ToR §5.4.6 (when applicable — `/admin/audit/csv` is the one new write path this sprint that DOES audit). Re-rendering the same query result is not a separate read.
- **Sync libraries via `asyncio.to_thread`.** reportlab's `Canvas.save()` is synchronous; wrapping the call in `asyncio.to_thread(...)` keeps the event loop responsive even on large batches. Any future sync-only library (pillow imaging, e.g.) follows the same pattern.
- **`pageCompression=0` for testability.** reportlab compresses content streams by default; disabling compression keeps the QR-id caption text greppable in raw bytes, so unit tests can assert page count + caption rendering without pulling a PDF-parser dep. The size cost (~10–15 KB per 32-label batch) is negligible at admin-tool scale.
- **Web handler delegates to JSON handler** (Task 4 D1). `web_force_close_session` calls `app.api.v1.admin.sessions.force_close_session` via Python (not HTTP) so Sprint 7's three-record-write apparatus + idempotency contract live in one place. The web handler only adds the HTML-friendly UX shell: form parsing, flash redirects, filter-context preservation. **Pattern for any future web write path:** the JSON handler does the work, the web handler is a thin transport shim.
- **Fresh-session lookup for audit attribution** (Task 4). When delegating to a JSON handler that opens its own `session.begin()`, the web handler must use a NEW session for any preliminary lookups — SQLAlchemy 2.0 errors with "A transaction is already begun on this Session" if the injected session was already auto-started by a prior call. Recorded as a gotcha; the inline comment in `web_force_close_session` documents it.
- **Direct-await coverage tracing remains the load-bearing testing pattern.** Sprint 7's `feedback_endpoint_test_direct_await.md` lesson held this sprint: every web handler's post-await `return templates.TemplateResponse(...)` line needs a direct-await unit test to register in coverage. Integration tests via `httpx.AsyncClient` + `ASGITransport` exercise the routing + dep gates but don't trace the return through the ASGI stack.

### Sprint 8b retrospective

**What went well:**
- Plan-then-confirm rhythm held across all 5 tasks. Each task's plan accurately predicted the file footprint, decision tree, and test count.
- The "delegate to existing JSON handler via Python" pattern in Task 4 saved re-implementing the Sprint 7 three-record-write apparatus. It also surfaced the `transaction already begun` gotcha cleanly, with a one-line fix (fresh session for the lookup).
- The Task 2 local code review caught the `dict.fromkeys` type-annotation tightening (M2) and the PDF-audit parking-lot question (M3) — both applied as part of this close-out commit. Catching them at code-review time meant they didn't ferment into Sprint 9+ technical debt.
- TDD discipline held end-to-end. Coverage stayed at 100% through every task's final gate. The `reportlab` `pageCompression=0` lever for testability was a nice find — it eliminated the need for a PDF-parser test dep.
- The flash-via-query-string pattern in Task 4 (`?flash=Shift+force-closed&flash_kind=info`) avoided introducing session-flash cookie state. Survives one redirect by design; the existing `_base.html` flash slot picked it up with zero template changes.

**What slowed us down:**
- **Task 0's close-out reported 100% coverage but actually shipped at 99.39%.** Task 1 spent ~30 minutes retroactively backfilling: the OIDC callback handler's four token-exchange failure branches (HTTPError, non-200 response, missing id_token, claim-parse failure) plus `_redirect_to_login`'s query-string branch plus the dashboard-handler post-await return were untested. Going forward: always trust the literal `--cov-fail-under=100` exit code; never paraphrase the coverage outcome from memory when writing a close-out summary.
- **`python-multipart` wasn't flagged at Task 4 plan stage.** Plan decision D10 said "FastAPI's `Form(...)` parses it" without auditing the dep chain. Surfaced as a runtime error on the first integration test run; required pausing for user approval (`AskUserQuestion`). **Lesson: any future sprint touching HTML form-handling endpoints needs an explicit dep audit at plan stage, paralleling how `circuitbreaker` / `jinja2` / `reportlab` were each flagged.**
- **Pre-existing Sprint 8a NetBox-timeout flake** in `tests/integration/test_rate_limit.py::test_429_after_exhausting_read_budget` recurred across every task's final full-suite run. The test pings `/api/v1/meta/sites` (which calls NetBox at the fake URL `netbox.example.com`); DNS-resolution timing makes the per-call latency vary, and three of those calls can push past whatever budget remained from the prior test's bucket. Passed on rerun every single time. Carry-forward as a Sprint 9+ stabilisation item.
- Coverage backfill in Task 1 had to also paper over `app/main.py:89-91` (lifespan shutdown TimeoutError), `app/middleware/rate_limit.py:163` (UNLIMITED sentinel), and `app/services/auto_end_job.py:222->exit` (the while-check exit branch) — none of those were Task 1 regressions; they were latent gaps inherited from Sprint 7 / 8a that Task 0's 99.39% measurement had masked. Cleared as a side-effect of fixing the rule violation; called out in the Task 1 commit body.
- IDE noise (wrong Python interpreter → constant false "module not found" / "attribute not found" / "Unexpected keyword argument" diagnostics) continued across every Task. Not a real problem; ignored every time and moved on.

**Discrepancies between ToR / Architecture and what shipped:**
- **`/web/qr/search` deferred** — ToR §4.4.2 lists it; Sprint 7 Task 2's `entity_type=qr&entity_id=...` audit filter partially covers the use case via `/web/audit/`. Dedicated `/web/qr/search` page can land in a future sprint when a real consumer asks for it. Recorded in the Sprint 8b plan's "Out of scope" section.
- **`/web/users/` deferred** — needs a Keycloak admin client + `KEYCLOAK_ADMIN_CLIENT_*` env vars (Sprint 6 decision J deliberately avoided to limit attack surface). Significant new dep surface; deserves its own sprint.
- **CSV endpoint path deviation** (Task 3 D1) — plan skeleton said `/api/v1/admin/audit.csv`; shipped as `/api/v1/admin/audit/csv` under the existing router prefix. Cleaner FastAPI routing without breaking the `{audit_id}` matcher. Documented in the Task 3 commit body + this entry.

**Deliberately deferred (Sprint 9+):**
- **PDF download audit row** — Task 2 decision 6 chose no audit row on `/api/v1/admin/batches/{id}/labels.pdf` (data is same as the audited JSON detail). For inventory traceability ("who printed labels for batch X at time Y"), this may matter. New parking-lot entry.
- **CSRF token for `/web/*` form POSTs** — Task 4 decision 12 chose no token, relying on `SameSite=Lax` cookie + admin-shift gate + VPN-only deploy. If a future security review requires it, add a per-session CSRF token. New parking-lot entry.
- **Cluster-wide rate-limit state** — carried from Sprint 8a; still in-process per-replica. Replace `_buckets` with Redis/Postgres when deployment goes multi-replica.
- **Phase 2 partial-failure alerting** — carried from Sprint 3; depends on operational monitoring infra not yet in place.
- **Idempotency-key TTL cleanup job** — pre-existing carry-over from Sprints 2–7, no consumer yet.
- **`/web/qr/search` and `/web/users/`** — see Discrepancies above.

### Files added in Sprint 8b (high-level)

- `backend/app/web/auth.py` (Task 0) — `WebAdminUser` + cookie encode/decode + `require_web_admin` dep + typed exceptions
- `backend/app/web/router.py` (Task 0; modified Tasks 1–4) — OIDC redirect flow + four `/web/*` page handlers + force-close shim
- `backend/app/web/templates/{_base,_dashboard_placeholder,_admin_shift_needed,_not_found,dashboard}.html` (Task 0 + Task 1; `_dashboard_placeholder.html` deleted in Task 1; `_not_found.html` added in Task 2)
- `backend/app/web/templates/{batches,audit,sessions}/{list,detail}.html` (Tasks 2, 3, 4)
- `backend/app/web/static/admin.css` (Task 0; appended Tasks 1, 2, 3, 4)
- `backend/app/domain/dashboard.py` (Task 1) — `DashboardSnapshot` frozen dataclass
- `backend/app/db/repositories/dashboard.py` (Task 1) — `DashboardRepository.snapshot`
- `backend/app/api/v1/admin/dashboard.py` (Task 1) — `GET /api/v1/admin/dashboard`
- `backend/app/services/pdf_labels.py` (Task 2) — pure-function PDF renderer
- `backend/tests/integration/web/test_oidc_flow.py` (Task 0)
- `backend/tests/integration/web/test_dashboard_page.py` (Task 1)
- `backend/tests/integration/web/test_batches_pages.py` (Task 2)
- `backend/tests/integration/web/test_audit_pages.py` (Task 3)
- `backend/tests/integration/web/test_sessions_pages.py` (Task 4)
- `backend/tests/integration/test_dashboard_repository.py` (Task 1)
- `backend/tests/unit/web/{__init__,test_auth,test_router}.py` (Task 0; `test_router.py` extended Tasks 1, 2, 3, 4)
- `backend/tests/unit/api/v1/test_admin_dashboard.py` (Task 1)
- `backend/tests/unit/api/v1/test_admin_batches_list_and_pdf.py` (Task 2)
- `backend/tests/unit/api/v1/test_admin_audit_csv.py` (Task 3)
- `backend/tests/unit/services/test_pdf_labels.py` (Task 2)

### Files modified in Sprint 8b

- `backend/pyproject.toml` — three new deps + mypy override for `reportlab.*`
- `backend/uv.lock` — regenerated three times
- `backend/.env.example` (Task 0) — three new `KEYCLOAK_WEB_CLIENT_*` + `SESSION_COOKIE_KEY` env vars documented
- `backend/app/config.py` (Task 0) — three new Settings fields
- `backend/app/main.py` (Task 0) — `/web/*` router mounted; `/static/*` `StaticFiles` mounted; `WebAdminAuthRequired` + `AdminShiftNeeded` exception handlers added
- `backend/app/middleware/rate_limit.py` (Task 0) — `_UNLIMITED_PREFIXES = ("/web/", "/static/")` + classification check
- `backend/app/api/v1/admin/batches.py` (Task 2) — `GET /api/v1/admin/batches/` list endpoint + `GET /api/v1/admin/batches/{id}/labels.pdf` PDF endpoint
- `backend/app/api/v1/admin/audit.py` (Task 3) — `GET /api/v1/admin/audit/csv` streaming endpoint + audit-of-audits row write
- `backend/app/db/repositories/qr_batch.py` (Task 2) — `query` paginated method
- `backend/app/db/repositories/qr_code.py` (Task 2; M2 annotation in Task 5) — `count_by_status_for_batch` GROUP BY method + explicit `dict[QRStatus, int]` annotation
- `backend/app/db/repositories/audit_log.py` (Task 3) — `get_by_id` method for the detail page
- `backend/tests/conftest.py` — unchanged (Task 0 deliberately did NOT add the three new web env vars to `_APP_ENV_KEYS` wipe list; the two failure-path tests in `test_config.py` use their own `monkeypatch.delenv`)
- `backend/tests/unit/test_config.py` (Task 0) — four new tests (defaults applied, missing client secret raises, missing cookie key raises, env override)
- `backend/tests/unit/middleware/test_rate_limit.py` (Task 0 + Task 1) — six new parametrize cases for `/web/*` + `/static/*` UNLIMITED + `_limit_for_class` UNLIMITED-sentinel test
- `backend/tests/unit/services/test_auto_end_job.py` (Task 1) — while-check-exit branch test
- `backend/tests/integration/test_main_lifespan.py` (Task 1) — shutdown TimeoutError test
- `backend/tests/integration/test_repositories.py` (Task 2) — new tests for `QRBatchRepository.query` + `QRCodeRepository.count_by_status_for_batch`
- `backend/tests/integration/test_audit_log_repository.py` (Task 3) — new tests for `AuditLogRepository.get_by_id`
- `docs/sprint-8b.md` — per-task detail not filled in inline this sprint (skeleton-only at plan stage); this work-log entry is the close-out artifact
- `docs/parking-lot.md` — two new entries: PDF download audit row, CSRF token for `/web/*` forms
- `CLAUDE.md` — Repository Status updated for Sprint 8b closure; `docs/sprint-8b.md` line flipped from "TBD" → "(delivered)"

### How to run locally (close-of-sprint snapshot)

Same shape as Sprint 8a. Sprint 8b adds three new required env vars (one with a default):

```bash
export KEYCLOAK_WEB_CLIENT_ID=dcinv-web          # optional, defaults to dcinv-web
export KEYCLOAK_WEB_CLIENT_SECRET=<from-Keycloak>
export SESSION_COOKIE_KEY=$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
```

`SESSION_COOKIE_KEY` is a Fernet key (44-byte url-safe base64). Generate once per deployment and persist; rotating it invalidates all outstanding admin session cookies (users re-login on next request).

To smoke-test the full admin web surface against a running stack:

1. Open `http://localhost:8000/web/` in a browser. You'll be 302'd to Keycloak. Authenticate as an admin (role `dcinv-admin`).
2. Land on `/web/` — if you have an active admin shift, you see the dashboard. Otherwise you see the "Open an admin shift" intermediate page; click Start.
3. Navigate via the top nav: Dashboard, Batches, Audit, Sessions.
4. Create a batch via `POST /api/v1/admin/batches/` (curl or the existing mobile path), then go to `/web/batches/` → click the row → "Download labels (PDF)".
5. Go to `/web/audit/`, filter on `entity_type=qr`, click "Download CSV" to download a filtered export.
6. Go to `/web/sessions/`, type a reason into another shift's force-close form, submit. See the green flash + the row's state change.

---

## Post-Sprint 8b fixes (pre-deployment)

Standalone bug-fix commits between Sprint 8b close and first production deployment. Not a sprint — small targeted fixes that surfaced during a pre-deploy template walk-through. Sprint 9 has not been opened.

### 2026-06-05 — fix: `_admin_shift_needed.html` form actually opens an admin shift

The "Start admin shift" intermediate page was visually correct but functionally broken in any real browser:

- The form set `enctype="application/json"` and posted to `/api/v1/admin/sessions/start`. Browsers ignore that enctype (it is not a valid form enctype value) and send `application/x-www-form-urlencoded` regardless — so the JSON endpoint's Pydantic body parser saw urlencoded data and returned 422.
- Even with a matching body shape, the JSON endpoint is gated by `require_role("dcinv-admin")` which reads the JWT bearer header. The web admin only carries the Fernet cookie → 401.

**Fix.** New `POST /web/admin/shift/start` handler in [backend/app/web/router.py](../backend/app/web/router.py) mirroring the Sprint 8b Task 4 force-close pattern: cookie auth inline (skipping the active-shift check — this handler IS for the no-shift state), `Form(workstation_id)`, delegates directly to `ShiftSessionService.start(...)`, 303 → `/web/`. `SessionAlreadyActive` also 303s to `/web/` (idempotent UX: a concurrent open in another tab already put the user in the state the page wanted). Template ([_admin_shift_needed.html](../backend/app/web/templates/_admin_shift_needed.html)) action repointed and the misleading enctype dropped.

Tests: four direct-await cases in [tests/unit/web/test_router.py](../backend/tests/unit/web/test_router.py) (success, already-active idempotent, no cookie, non-admin role). Unit suite: 547 → 551 passing (Sprint 8b's 1018-test count was suite-wide including integration).

### 2026-06-05 — fix: bare-hostname `GET /` → 307 redirect to `/web/`

First-deployment feedback: users opening `https://qr-dc.t-cloud.kz/` saw FastAPI's default `{"detail": "Not Found"}` JSON instead of the admin. The web surface lives under `/web/`; bare `/` had no route.

**Fix.** Add `GET /` handler in [backend/app/main.py](../backend/app/main.py) returning `RedirectResponse(url="/web/", status_code=307)`. 307 (not 301/308) so we keep room to serve real content at `/` later without fighting browser cache. `include_in_schema=False` keeps `/docs` clean.

Also added `/` to `_UNLIMITED_PATHS` in [backend/app/middleware/rate_limit.py](../backend/app/middleware/rate_limit.py) so an upstream LB liveness probe pointed at `/` (we saw this from `10.121.43.31:0` in the first-deploy logs) cannot exhaust the per-user READ bucket. Semantically correct: bare `/` is pure redirect plumbing with no business logic.

Tests: new [tests/unit/test_main.py](../backend/tests/unit/test_main.py) with one direct-await test, plus a new row in the parametrized `_classify_request` table. Unit suite: 551 → 553 passing.

### 2026-06-05 — fix(web): OIDC state-mismatch log → actionable fields

First-login attempt in production hit `state_mismatch` even in incognito (no stale-cookie carryover possible). The existing log collapsed three distinct failure modes into one boolean `state_match=False`, so we couldn't tell which one fired. Root cause turned out to be Keycloak client-scopes misconfig on the IdP side (unrelated to our code), discovered out-of-band — but the diagnostic log improvement landed anyway since it's load-bearing for the next time this misfires.

**Fix.** Split [backend/app/web/router.py](../backend/app/web/router.py)'s `web_oidc_callback_state_mismatch` log into five actionable fields:
- `has_state_query` — did Keycloak echo state back?
- `has_state_cookie` — did `__dcinv_oidc_state` reach the callback?
- `state_match` — both present, equal?
- `is_https` — does the server see this as HTTPS? (checks `--proxy-headers` + nginx `X-Forwarded-Proto`)
- `cookie_names_present` — sorted list of all cookies that DID arrive (empty list = full cookie drop)

No behaviour change. Diagnostic-only.

### 2026-06-05 — feat(web): admin-action forms (create batch + retire QR + decommission device)

User feedback after first successful login: "I can see batches and audit, but I don't know how to issue QRs or control them." The Sprint 8b admin surface was read-only + force-close-only; the curl-only admin workflows (create batch, retire QR, decommission device) needed UI to close the daily-use loop.

**Added** in [backend/app/web/router.py](../backend/app/web/router.py):
- `GET /web/batches/new` + `POST /web/batches/` — full create-batch form (count, comment, optional intended site/location/rack). Delegates to `QRGenerationService.generate_batch` directly (decision I — same Python-call pattern as `web_force_close_session`), 303 to `/web/batches/{id}` with flash.
- `POST /web/qr/{qr_id}/retire` — inline FREE-only retire button on batch detail rows. Maps `QRNotFoundError`, `QRStateConflictError(RETIRED)` (idempotent info flash), `QRStateConflictError(BOUND)` + `MissingVersionError` (error flash pointing at decommission flow), and the rollback variants to distinct flash banners.
- `GET /web/devices/decommission` + `POST /web/devices/decommission` — device-id + reason form. Handler fetches the device's current `last_updated` itself, passes it as the OCC version, calls `DeviceDecommissionService.decommission`. Error surface: 404, `WriteConflictError`, `QRStateConflictError` on the bound QR, plus the rollback / inconsistency exceptions — each mapped to its own flash banner.

Helper `_build_auth_user_for_admin_action` factors the cookie-decode + active-shift-lookup + `AuthUser` construction that was previously duplicated inline in `web_force_close_session`. Five new routes total.

**Templates** added: [batches/new.html](../backend/app/web/templates/batches/new.html), [devices/decommission.html](../backend/app/web/templates/devices/decommission.html). Modified: [dashboard.html](../backend/app/web/templates/dashboard.html) (new `quick-actions` row with two CTAs), [batches/list.html](../backend/app/web/templates/batches/list.html) (`+ New batch` CTA + flash banner), [batches/detail.html](../backend/app/web/templates/batches/detail.html) (extra column with inline Retire form on FREE rows). CSS: one new `.quick-actions` flex rule in [admin.css](../backend/app/web/static/admin.css).

**Tests:** 12 new direct-await cases in [tests/unit/web/test_router.py](../backend/tests/unit/web/test_router.py) covering create-batch happy + comment-stripping, all five retire-QR error branches, decommission happy + 404 + write-conflict, and template-render coverage for both new GET forms. Unit suite: 553 → 565 passing.

**Workflow now closed entirely in the browser:**
1. Dashboard → "+ New QR batch" → form → 303 to `/web/batches/{id}` → "Download labels (PDF)" → print.
2. Batch detail → Retire button next to any FREE code → 303 with flash.
3. Dashboard → "Decommission device" → form → 303 with flash. QR-first ordering still handled in the underlying service; OCC version fetched server-side so the admin only types device id + reason.

**Still deferred** (parking lot unchanged otherwise): CSRF for `/web/*` POSTs; `/web/qr/search` (lookup by id); `/web/users/` (Keycloak admin client). BOUND→RETIRED via the dedicated retire button is intentionally NOT supported — the device decommission flow does it correctly with proper OCC, and the retire button is for FREE codes only.

### 2026-06-05 — fix(web): retire redirects back to batch detail + confirm dialogs

Self-code-review of the admin-action forms surfaced two issues:

1. **Retire-QR redirected to `/web/batches/`** (the list page), losing the admin's context when retiring multiple FREE codes from inside a single batch detail page.
2. **Destructive actions had no confirmation step** — Retire on each FREE row and Decommission device were single-click-submit, easy to misfire.

**Fix.** `POST /web/qr/{qr_id}/retire` now accepts a `batch_id: UUID | None` form field. The inline form in [batches/detail.html](../backend/app/web/templates/batches/detail.html) carries it as a hidden input, so the 303 lands back on `/web/batches/{batch_id}?flash=...` instead of the bare list. Falls back to the list when absent (curl / hand-rolled POST). Both [batches/detail.html](../backend/app/web/templates/batches/detail.html) (Retire button) and [devices/decommission.html](../backend/app/web/templates/devices/decommission.html) (Decommission button) get a one-line `onsubmit="return confirm(...);"` JS guard. The decommission confirm interpolates the device id at submit time.

One new direct-await test covers the `batch_id`-present branch; existing retire tests updated to pass `batch_id=None` explicitly (direct-await bypasses FastAPI's `Form(...)` default-unwrap, so the dependency-injected default doesn't apply when the handler is called as a plain coroutine). Unit suite: 565 → 567 passing.

**CSRF still deferred** — same parking-lot item from Sprint 8b. Worth escalating into the next sprint now that there are four `/web/*` POST forms in active use.

### 2026-06-05 — feat(web): CSRF protection on all /web/* POST forms

Closed the Sprint 8b parking-lot item ahead of any new sprint open, on user request the day they hit live production. Five POST forms in active use (admin-shift-start, create-batch, retire-QR, force-close-session, decommission-device) were all CSRF-vulnerable until this change.

**Design.** Per-session CSRF token stored in the Fernet-encrypted cookie payload:
- 32 url-safe random bytes generated at OIDC callback (`secrets.token_urlsafe(32)`), bound to the cookie's 8-hour lifetime, rotates on each fresh login.
- Surfaced to templates as `csrf_token` context var; embedded in every form as `<input type="hidden" name="_csrf" value="{{ csrf_token }}">`.
- Each POST handler reads `csrf: str = Form(alias="_csrf")` and calls `verify_csrf_token(csrf, user.csrf_token)` before any business logic. Mismatch → 403 via `HTTPException`. Constant-time compare via `hmac.compare_digest`.

**Why this design.** Stateless (no server-side token storage), tied to the encrypted cookie so a stolen-cookie attack already wins everything, simple to implement against the existing OIDC-cookie infrastructure. Pydantic doesn't allow leading-underscore parameter names, hence the Python identifier `csrf` with HTML alias `_csrf`.

**Changes.** [app/web/auth.py](../backend/app/web/auth.py): `csrf_token` added to `WebAdminUser` dataclass, threaded through `encode_session_cookie` / `decode_session_cookie` / `build_session_cookie_payload`, plus new `verify_csrf_token(submitted, expected)` helper. [app/web/router.py](../backend/app/web/router.py): 5 template-render sites now pass `csrf_token=user.csrf_token`, 5 POST handlers accept `csrf` form field + verify on entry. Five templates ([_admin_shift_needed.html](../backend/app/web/templates/_admin_shift_needed.html), [batches/new.html](../backend/app/web/templates/batches/new.html), [batches/detail.html](../backend/app/web/templates/batches/detail.html), [sessions/list.html](../backend/app/web/templates/sessions/list.html), [devices/decommission.html](../backend/app/web/templates/devices/decommission.html)) embed the hidden input.

**Migration.** All pre-CSRF cookies fail to decode (missing `csrf_token` field) → redirect to `/web/login` → fresh cookie carries the token. One-time re-login on the deploy. No DB migration.

**Tests:** 5 new direct-await CSRF-mismatch tests (one per POST handler), confirming `HTTPException(403)` raises before any DB work happens. Updated all 18 existing POST-handler test invocations to pass `csrf="test-csrf-token"`, refactored `_admin_cookie_value` test helper to build the cookie with a deterministic `csrf_token` (matching what tests pass). Unit suite: 567 → 572 passing.

**Parking lot now empty for `/web/*` security.** `/web/qr/search` and `/web/users/` remain as feature gaps, not security ones.

### 2026-06-07 — feat(web): /web/qr/search

Closes the `/web/qr/search` parking-lot item. One-page lookup: form at the top, optional result block below when `?qr_id=...` is set.

**Renders:** the QR row (status badge, batch link, bound device id, bound/retired timestamps); the bound NetBox device card (id, name, status, site, rack, version) when QR is BOUND and the device is reachable; the 20 most recent audit rows for the QR with a "See all" link to `/web/audit/?entity_type=qr&entity_id=…` for deeper history. Stale-binding diagnostic when QR points at a device NetBox no longer recognizes (404) — surfaces a flash banner instead of silently swallowing.

**Read-only.** No audit row for the search itself (operational read, mirrors Sprint 7 decision 8). NetBox is consulted only when QR is BOUND — FREE and RETIRED skip the network round-trip.

[router.py](../backend/app/web/router.py) `web_qr_search` GET at `/web/qr/search`; template [qr/search.html](../backend/app/web/templates/qr/search.html); top-nav link in [_base.html](../backend/app/web/templates/_base.html) between Batches and Audit.

Four direct-await tests: empty form (no `qr_id`), unknown QR, FREE QR (asserts NetBox isn't touched), BOUND QR with NetBox 404 (stale-binding flash). Unit suite: 572 → 576.

### 2026-06-07 — feat(web): /web/users/ (read-only Keycloak admin)

Closes the second parking-lot item. Sprint 6 decision J deliberately avoided this because it needed a separate Keycloak admin client — that's what this commit adds.

**Design.** Read-only first slice: list + per-user detail. Write operations (disable/enable, role changes) stay deferred — they'd need their own audit-row + CSRF flow plus a spec for which writes the admin tool should actually expose.

**New module** [app/auth/keycloak_admin.py](../backend/app/auth/keycloak_admin.py):
- `KeycloakAdminClient` — async wrapper over Keycloak's `/admin/realms/{realm}/users` API.
- `client_credentials` grant against a confidential admin client. Service account needs `realm-management.view-users`.
- In-process token cache with 10s pre-expiry refresh leeway + an `asyncio.Lock` so concurrent requests don't all hit the token endpoint.
- `KeycloakAdminNotConfigured` raised when `KEYCLOAK_ADMIN_CLIENT_SECRET` is unset — the web handler renders a friendly notice instead of crashing.
- `KeycloakAdminError(status_code=...)` for transport / non-2xx errors so callers can distinguish misconfig (4xx) from transient failure (5xx).

**New config fields** ([app/config.py](../backend/app/config.py)):
- `keycloak_admin_client_id: str = "dcinv-admin-cli"` (defaulted, so the typical install needs no override).
- `keycloak_admin_client_secret: SecretStr | None = None` (optional — leave unset to disable `/web/users/`).
- Computed `keycloak_token_url` and `keycloak_admin_realm_base` so both the OIDC callback and the admin client read the same URL convention.

**Routes** in [app/web/router.py](../backend/app/web/router.py):
- `GET /web/users/` — paginated list with optional `?search=...` filter (Keycloak's free-text search across username/email/first/last).
- `GET /web/users/{user_id}` — single-user detail: identity, enable state, realm roles, created-at. Links to `/web/audit/?user_keycloak_id=...` for the per-user audit trail.

**Templates:** [users/list.html](../backend/app/web/templates/users/list.html) (filter form + table + not-configured + error fallbacks), [users/detail.html](../backend/app/web/templates/users/detail.html) (kv list + realm-roles list + audit link). Top-nav gains a `Users` link after `Sessions`.

**Docs:** [docs/deploy.md](deploy.md) section 1 gets a new row for the admin CLI client (how to create + assign `realm-management.view-users`); the `.env` table gets two new optional rows.

**Tests:** 9 new — 6 for `KeycloakAdminClient` (respx mocking the token + admin endpoints): not-configured raise, list pagination with `has_more`, get_user 404 → None, get_user assembles roles, token cache hits token endpoint exactly once on repeated calls, non-404 4xx → KeycloakAdminError. 3 direct-await handler tests: list happy path, list not-configured notice, detail unknown user → 404 page. Unit suite: 576 → 585.

**Out of scope (deliberate):** no disable/enable, no role mapping changes, no password reset trigger. Those land separately once we have a concrete use-case; meanwhile the page gives the admin enough visibility to know who can use the system and trace any user back to their audit trail.

### 2026-06-07 — fix(web): create-batch + PDF download production bugs

Two browser bugs surfaced from first-use of the new admin forms.

**Bug 1: Empty optional id fields 422'd on create-batch.** The form declared `intended_site_id`/`intended_location_id`/`intended_rack_id` as `int | None = Form(default=None, ge=1)`, but browsers submit unchecked optional inputs as `""` rather than omitting them. Pydantic's `int | None` coerces `""` → 422 with `int_parsing` errors before the handler body even runs.

Fix: accept them as `str = Form(default="")` and parse via new `_parse_optional_form_int(raw, field=...)` helper — empty/whitespace → `None`, valid positive int → `int`, anything else → 422 with a clear "must be a positive integer or blank" message. Tested with two parametrized cases (4 happy values + 4 invalid values) plus an all-blank create-batch flow.

**Bug 2: Download labels (PDF) 401'd ("missing bearer token").** The batch detail page linked to `/api/v1/admin/batches/{id}/labels.pdf` (bearer-only JSON API). Browsers carry only the Fernet session cookie, so the click 401'd.

Fix: new `GET /web/batches/{batch_id}/labels.pdf` handler ([app/web/router.py](../backend/app/web/router.py)) authed by the same `require_web_admin` cookie dep, delegates to the same `render_batch_labels_pdf` worker in `asyncio.to_thread`. Template link in [batches/detail.html](../backend/app/web/templates/batches/detail.html) repointed at the web shim. The JSON endpoint stays bearer-only — mobile / curl flows are unchanged.

Three new tests for the PDF handler (happy path returns PDF bytes + correct Content-Disposition, 404 raises HTTPException). Unit suite: 585 → 596.

### 2026-06-07 — feat(web): Tailwind-style redesign of all 14 templates

After two weeks of production use, the user came back with concrete UI direction (was deferred per the ship-then-iterate principle): Tailwind-style utility classes, light-gray app background (`bg-gray-50`), white content cards (`bg-white`), dark-gray headings (`text-gray-900`), muted secondary text (`text-gray-500`), minimalist flat design with subtle shadows.

**Approach.** Tailwind via Play CDN (`<script src="https://cdn.tailwindcss.com">`) — one line in `_base.html`, no build step (preserves CLAUDE.md's Python-only stack constraint). ~300KB JS load is browser-cached and acceptable for an internal VPN-only admin tool. Switch to a precompiled build under `/static/` if it ever becomes a perf concern (Sprint 9+).

**Palette.** Indigo as primary action color (buttons + links), emerald/rose/amber for status/result/end-reason badges, slate-900 for the JSON pretty-print blocks, rose for the destructive Decommission button.

**All 14 templates rewritten** with Tailwind utilities and Jinja macros for the status/result/end-reason badges (one macro per template family — no global include since Jinja includes don't auto-pass scope). [_base.html](../backend/app/web/templates/_base.html) restructured: top nav uses pill-link hover state, max-w-7xl content container, responsive grid for the dashboard counters (1/2/3 columns by viewport), `bg-gray-50` body, `bg-white` cards with `ring-1 ring-gray-200 shadow-sm`.

**[admin.css](../backend/app/web/static/admin.css) reduced** from ~450 lines of hand-rolled CSS to a 6-line stub kept as a stable URL the base template links — every visual is now Tailwind. The two-paragraph header notes when to add rules back (Tailwind can't easily express something).

Unit suite still 596 passing — the redesign preserves every text string the tests assert on (h1 copy, error messages, badge text). Visual verification post-deploy.

### 2026-06-07 — fix(web): audit CSV download cookie-authed + form a11y

Two findings from a self-review of the Tailwind redesign commit:

**HIGH — audit CSV link was bearer-only.** Same class of bug as the PDF download we just fixed in `901d827`: the Download CSV button in [audit/list.html](../backend/app/web/templates/audit/list.html) pointed at `/api/v1/admin/audit/csv` (JWT-bearer-gated), so clicking it in the browser returned `{"detail":"missing bearer token"}`. Add `GET /web/audit/csv` cookie-authed shim in [router.py](../backend/app/web/router.py) that builds an `AuthUser` from the cookie's active shift and delegates to `query_audit_log_csv` — keeps the `audit.export_csv` audit-of-audits row (ToR §5.4.6) firing with proper attribution. Template href repointed. Registered BEFORE `/audit/{audit_id}` (same route-ordering invariant as `/batches/new`); regression test pins it.

**MEDIUM — form labels not associated with inputs.** The Tailwind redesign moved labels from wrapping inputs (`<label>Text <input></label>`) to sibling pairs (`<label>Text</label><input>`) without explicit `for=`/`id=` pairing, breaking screen-reader association. Added `for="X"`/`id="X"` to every label/input pair across [batches/new.html](../backend/app/web/templates/batches/new.html), [devices/decommission.html](../backend/app/web/templates/devices/decommission.html), [qr/search.html](../backend/app/web/templates/qr/search.html), [audit/list.html](../backend/app/web/templates/audit/list.html), [sessions/list.html](../backend/app/web/templates/sessions/list.html), [users/list.html](../backend/app/web/templates/users/list.html). The `_admin_shift_needed.html` form already had the pair from the earlier explicit-fix commit.

Two new tests: `web_audit_csv` happy-path delegation (auth user built + filters passed through), `/audit/csv` registered before `/audit/{audit_id}` route-order regression guard. Unit suite: 596 → 598.

### 2026-06-07 — fix(web): code-review MEDIUM/LOW polish + wider filter forms

Follow-up to the same-day code review. The HIGH was fixed in the previous commit; this one closes the MEDIUM + most of the LOW items plus a width tweak the user asked for on top.

**MEDIUM 3 — Tailwind classes via Jinja string concatenation.** The flash banner pattern `border-{{ 'rose-200 …' if … else 'indigo-200 …' }}` worked because the Play CDN JIT scans the rendered DOM, but it was fragile (a typo would silently drop the border) and invisible to IDE/CI Tailwind tooling. Replaced with full `{% if %}{% else %}` blocks holding complete class strings in [batches/list.html](../backend/app/web/templates/batches/list.html), [sessions/list.html](../backend/app/web/templates/sessions/list.html), [devices/decommission.html](../backend/app/web/templates/devices/decommission.html).

**LOW 4 — inline `onsubmit` with Jinja interpolation.** The Retire button on each FREE row in [batches/detail.html](../backend/app/web/templates/batches/detail.html) was confirming via `onsubmit="return confirm('Retire QR {{ c.id }}? …');"` — safe today because QR ids come from `secrets.token_urlsafe()` (url-safe base64, no quotes/backslashes), but defense-in-depth dictates avoiding template interpolation inside JS attribute values. Refactored to `<form class="js-retire-form" data-qr-id="{{ c.id }}">` + a small `<script>` block at the end of the template that wires `submit` listeners. The QR id is read from the DOM as a string at runtime — no JS injection path even if the id-character-set widens later.

**LOW 5 — supply-chain CDN dependency.** Closed in a follow-up commit (user gave permission to vendor the script). Downloaded https://cdn.tailwindcss.com → [backend/app/web/static/tailwind.js](../backend/app/web/static/tailwind.js) (~407KB minified). `_base.html` now references `/static/tailwind.js` — no runtime dependency on cdn.tailwindcss.com. Same JIT engine as the CDN; pinned to the version downloaded on 2026-06-07. Replace the file to upgrade; precompile to a CSS bundle (needs Node) only if the ~400KB JS load becomes a perf concern.

**LOW 6 — CDN console warning.** Not actionable without a precompile step (Node), which would break the Python-only stack constraint. Acknowledged; left as-is.

**Wider filter forms.** User feedback on the screenshot: the UUID input in the audit filter form was squeezed at `lg:grid-cols-4` (≈25% of form width on large viewports). Dropped to `lg:grid-cols-3` in [audit/list.html](../backend/app/web/templates/audit/list.html) and [sessions/list.html](../backend/app/web/templates/sessions/list.html) — each input now gets ~33% width on `lg`, plenty for a 36-char UUID. Span on the action row updated to match (`lg:col-span-3`).

Unit suite still 598 passing — these were all template-only changes preserving the text strings tests assert on. Lint clean.

## Sprint 9 — Operational Hygiene (in progress, opened 2026-06-08)

Plan: [docs/sprint-9.md](sprint-9.md). 5 tasks, ~6 days target. Goal: close the operational debt accumulated through the user-facing sprints (8a + 8b) so the system is ready for sustained production use plus the mobile rollout — not just a "demo to admins" surface.

### Task 0 — Idempotency on every write endpoint (closed 2026-06-08)

Split into 0a / 0b / 0c by endpoint family. Total: 8 mobile + admin write endpoints now accept the optional `Idempotency-Key` header. Sprint 5's existing `POST /admin/batches/` was already integrated; Task 0 brings the rest in line.

**Approach.** Pre-implementation plan considered refactoring Sprint 6+ services (ShiftSession, QRLifecycle, DeviceDecommission) to caller-managed transactions so Sprint 5's `with_idempotency` could compose atomically. Aborted after measuring the blast radius — 15+ existing tests per service plus 4 caller updates would have made Task 0 a multi-day refactor with high regression risk. Switched to a **separate-session wrapper** (`with_optional_idempotency_outer` in [app/services/idempotency.py](../backend/app/services/idempotency.py)) that:

- Reads any existing idempotency row in its own session.
- Runs the work via each endpoint's existing service-managed-tx style.
- Records the response in a third session after work completes.

**Trade-off** documented in code: between "check" and "record" two concurrent retries can both run the work. For mobile retry scenarios (seconds-long backoff) the race window is microseconds and effectively never trips. Downstream protections (`qr_codes` partial unique index, OCC version checks via NetBox `last_updated`, NetBox 409 on duplicate device names) catch the residual cases. Create-device and add-comment are the genuinely-risky cases — NetBox has no native dedupe — and a comment in the docs/API guide flags them as **especially critical** for the mobile client to send the header on.

**Task 0a — sessions** (commit `a29df63`). `POST /sessions/start` + `/sessions/end`. 5 new unit tests for the wrapper (no-key passthrough, replay returns cached, 422 on payload mismatch, first-call store, race-loss returns winner). Unit suite: 598 → 603.

**Task 0b — QR lifecycle** (commit `a749de7`). `POST /qr/{id}/bind` + `/qr/{id}/retire`. Each handler refactored to wrap its existing try/except chain (7-8 different error response shapes) in a `do_work()` closure returning `(status_code, body)`. `request_payload` for idempotency hashing includes `qr_id` so two retries of `bind(QR-A, dev=1)` and `bind(QR-A, dev=2)` with the same key correctly hash differently → 422. Existing direct-await integration tests updated.

**Task 0c — devices** (commit `9b597fd`). `POST /devices/`, `PATCH /devices/{id}`, `POST /devices/{id}/comments`, `POST /devices/{id}/decommission`. 18 existing direct-await integration test sites updated via a depth-tracking bulk-edit script. `PATCH /devices/{id}` includes the `If-Unmodified-Since` header in the idempotency payload so a retry against a stale version with the same key surfaces 422 (client bug — should be a fresh key after the version changed).

**Docs update.** [mobile-api-guide.md](mobile-api-guide.md) gains a new **2.5 Idempotency contract** section before the endpoint catalogue: which 9 endpoints accept the header, when to generate fresh vs reuse, replay semantics (bit-for-bit response, including 4xx error bodies), and the namespacing rule (`(user_keycloak_id, key)` per-user — two engineers can use the same UUID).

**Unit suite at Task 0 close.** 603 passing (5 new wrapper tests). Lint clean. The endpoint-level integration tests remain marked `pytest.mark.integration` and skip without DB env vars; their direct-await sites have been updated to pass the new args so they'll be green when integration runs.

**Deliberately deferred from Task 0.**

- **Idempotency-key TTL cleanup job** — a 24h `DELETE WHERE created_at < NOW() - 24h` cron is in the Sprint 10 parking lot; until then, replay works forever (acceptable — the table is `(user, key)` keyed and small).
- **`POST /admin/sessions/start`** (admin shift open) — Sprint 9 plan in-scope list was the 8 mobile-facing endpoints. The admin-web flow has its own retry pattern (browser refresh + CSRF) so it doesn't need idempotency. Marked NOT-IN-SCOPE.

### Task 1 — Device search API (closed 2026-06-08, commit `e4af90e`)

New `GET /api/v1/devices/search` with five filters (`name` → `?name__ic=`, `asset_tag` / `serial` exact, `site` / `rack` as NetBox ids). Pagination via `?page=` + `?page_size=` (default 20, cap 100); service requests `page_size + 1` from NetBox and trims to detect `has_more` without a separate COUNT call. 30-second TTL cache keyed on the full query string — stricter than the 60s device-data cap because search semantics shift faster than per-device state.

Route registered BEFORE `/{device_id}` (regression test pins the order — same lesson as `/batches/new`). New `DeviceService.search()` + `DeviceSearchResponse` envelope shape `{results, page, page_size, has_more}` where each entry is the same `DeviceResponse` shape mobile already consumes from `GET /devices/{id}` — no schema gymnastics on the client.

8 new service tests (happy + parametrized over the 4 non-name filters + multi-filter combo + empty + has_more trimming). 4 new endpoint tests (param pass-through, cache hit, distinct queries miss separately, route-order regression). [mobile-api-guide.md](mobile-api-guide.md) gains section 3.5b. Unit suite: 603 → 611.

### Task 2 — `/web/devices/search` + `/web/devices/{id}` detail (closed 2026-06-08, commit `daf3dfd`)

Three new web handlers + 2 new templates. `GET /web/devices/search` is a Tailwind-style filter form over Task 1's search API. `GET /web/devices/{device_id}` is a read-only detail page — kv block of identity + location fields, the 20 most recent audit rows for `entity_type=device` (via `AuditLogRepository.query` direct call, decision I), plus a CSRF-protected `<textarea>` comments form. `POST /web/devices/{device_id}/comments` delegates to the JSON `add_comment` handler via direct Python call (same delegation pattern as `web_force_close_session`). Web admin's `dcinv-admin` role bypasses the JSON handler's `dcinv-mobile-user` dep gate via direct delegation — they're authorised via their own auth path (`require_web_admin`).

Top nav gains a "Devices" link between "QR search" and "Audit". `/devices/search` MUST register BEFORE `/devices/{device_id}` (regression test pins it).

6 new direct-await tests: detail happy + 404, comments happy → 303 + flash, comments CSRF mismatch → 403, search empty form (asserts NetBox isn't touched), route-ordering regression. Unit suite: 611 → 617.

**Deliberately out of scope.** Device EDIT path on the web (status change, field updates). Edits stay mobile-only per ToR — admin doesn't edit through web. If Sprint 10+ feedback says admins actually need this, it gets its own form + CSRF + OCC.

### Task 3 — Backup strategy (closed 2026-06-08, commit `4036662`)

PostgreSQL data lives in a docker volume; without this, disk failure = total loss of `qr_codes`, `qr_batches`, `audit_log`, `shift_sessions`, `idempotency_keys` (NetBox is **not** a backup for these — they hold history NetBox doesn't).

[backend/scripts/backup.sh](../backend/scripts/backup.sh) runs `pg_dump --format=custom` inside the `dcinv-db` container, writes to local staging, uploads to S3 via `aws s3 cp`, touches a marker file on success. Keeps the last 3 dumps locally; older dumps live in S3 only. Runs on the HOST via cron (decision H — backups must survive an app crash). [backend/scripts/restore.sh](../backend/scripts/restore.sh) is the inverse with a "type 'restore' to proceed" confirmation gate. [docs/backup.md](backup.md) is the full operator guide (env vars, cron entry, restore walkthrough, suggested S3 bucket lifecycle, alert thresholds).

`/health` gets a new informational `backups` sub-object reading the marker file's mtime. Returns one of three shapes: `{configured: false}`, `{configured: true, last_completed_at: null, age_seconds: null}` (set up but never ran), or `{configured: true, last_completed_at: <iso>, age_seconds: <int>}`. INFORMATIONAL ONLY (decision J): stale backups do NOT flip overall `/health` to degraded — mirrors NetBox circuit + auto-end-job patterns. External monitors alert on `age_seconds > <RPO threshold>`.

3 pure-unit tests for `_backups_sub_object` (configured-false, marker-missing, mtime-driven) placed in [tests/unit/services/test_health_backups.py](../backend/tests/unit/services/test_health_backups.py) to dodge the api/v1 conftest's autouse alembic fixture. 1 integration-marked TestClient test in [tests/unit/api/v1/test_health.py](../backend/tests/unit/api/v1/test_health.py) confirms the sub-object doesn't flip overall status to degraded. Scripts validated by inspection + manual smoke test at deploy time (no shellcheck in CI yet). Unit suite: 617 → 620.

**Deliberately out of scope.** Point-in-time recovery (WAL archiving), automated restore-validation cron, automated S3 lifecycle management — all noted in `docs/backup.md` "What's NOT in scope" + parked for Sprint 10+.

### Sprint 9 close-out (2026-06-08)

**Quality bar at close.**
- 620 unit tests passing (598 → 620, +22 across the 4 tasks)
- Lint clean (`ruff check`)
- Integration tests still skip without DB env vars; their direct-await sites updated for Sprint 9 signatures so they'll be green when integration runs
- No new Python dependencies (decision K from sprint plan)

**Mobile workstream prerequisites complete.**
- Idempotency contract is documented and live on 9 endpoints (Task 0)
- Device search lets engineers find devices without a working QR sticker (Task 1)
- Comments UI lets admins record observations from the web (Task 2)
- Backup cron means a disk failure no longer = total loss (Task 3)

**Deliberately deferred to Sprint 10+** (revised Sprint 10 scope, will land in its own plan):
- **Dashboard activity feed** — last N audit rows as a live stream on `/web/`. UX nice-to-have, not blocking.
- **Date-preset chips** ("Last 24h", "Today", "Last 7 days") on audit + sessions filter forms.
- **Bulk operations** on `/web/batches/{id}` (multi-select FREE codes + retire all) and a future `/web/devices/` bulk-decommission.
- **Real-time SSE/WebSocket dashboard updates** — Sprint 12+ (premature without observed cause).
- **Mobile offline-queue implementation** — Sprint 11 (this sprint laid the idempotency foundation it needs).
- **Cluster-wide rate-limit state** — carried since Sprint 8a; needs Redis.
- **Phase 2 partial-failure alerting** — carried since Sprint 3.
- **Idempotency-key TTL cleanup job** — 24h cron, will land alongside backup cron operationalisation in Sprint 10.
- **`/web/devices/{id}` write path** (edit fields, change status) — admin web stays read-only until ToR feedback says otherwise.
- **WAL archiving / point-in-time recovery** — backup.md Task 3 noted this; needs WAL-G or pgbackrest, separate operational story.
- **Automated restore-validation cron** — Sprint 10+ ops polish.
