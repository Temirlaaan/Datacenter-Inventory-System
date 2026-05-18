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
