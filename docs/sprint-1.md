# Sprint 1 — Foundation

> **Status:** Delivered 2026-05-14. See `docs/work-log.md` for the retrospective and any deviations.
> **Duration target:** 5–7 working days
> **Goal:** A running FastAPI service with health endpoint, structured logging, JWT auth dependency, and NetBox client — all under TDD, all observable, all deployable via docker-compose. No business logic yet.

## Why this sprint exists

Before any real feature (QR registry, device update, audit log) is built, the project needs a **foundation** that future sprints can stand on:

- Project structure matching Architecture §7.1
- Dependency declaration and lockfile
- Database connection and migration plumbing
- Observability (structured logs, request IDs, health checks)
- Authentication primitives (JWT validation against Keycloak `prod-v1`)
- NetBox client with retry, timeout, circuit breaker
- Test infrastructure (unit, integration, contract layers)
- Containerized deployment (docker-compose stack)

Without these, every subsequent sprint pays the cost of "should we just stub this out?". By the end of Sprint 1, future sprints answer "no, the foundation handles it".

## Scope boundaries

**In scope:**
- Bootstrapping the backend repository
- Health endpoint with real downstream checks
- Auth dependency (JWT validation, role checks) — no actual endpoint protection yet
- NetBox client wrapper — read operations only, no writes yet
- Test infrastructure and CI skeleton

**Out of scope (deferred to Sprint 2+):**
- QR generation, binding, retirement
- Device CRUD endpoints
- Audit logging service
- Web admin interface
- Mobile app (separate sprint sequence)
- Real Keycloak/NetBox integration in CI (use respx fixtures)

## Task list

Tasks are ordered for dependency. Do not start task N+1 until task N's acceptance criteria are met. Each task is sized for half a day to a day of focused work.

---

### Task 1 — Bootstrap the project structure

**Goal:** Empty but correctly-shaped repository, dependencies declared, tests run (vacuously).

**Steps:**
1. Initialize git repository (if not yet done) in `backend/`
2. Create `pyproject.toml` with project metadata and the **exact** allowed dependencies from CLAUDE.md
3. Use `uv` or `poetry` for dependency management (pick one and stick with it)
4. Create the module skeleton per Architecture §7.1:
   ```
   app/
   ├── __init__.py
   ├── main.py            # FastAPI app factory, just creates empty app
   ├── config.py          # pydantic Settings, all envs documented
   ├── auth/__init__.py
   ├── netbox/__init__.py
   ├── api/v1/__init__.py
   ├── web/__init__.py
   ├── db/__init__.py
   ├── domain/__init__.py
   ├── services/__init__.py
   └── observability/__init__.py
   tests/
   ├── unit/
   ├── integration/
   └── contract/
   ```
5. Add `.gitignore` (Python standard + `.env`, `__pycache__`, coverage, IDE files)
6. Add `pytest.ini` or pyproject.toml `[tool.pytest.ini_options]` with `pythonpath = "."` and test discovery rules
7. Add Ruff and Black config in pyproject.toml
8. Create empty `tests/unit/test_smoke.py` with `def test_imports(): import app.main` to confirm everything wires up

**Acceptance criteria:**
- `python -m pytest tests/` exits 0
- `ruff check .` and `black --check .` exit 0
- `python -m app.main` would import without ImportError (use `python -c "import app.main"` to verify)
- pyproject.toml contains ONLY the dependencies listed in CLAUDE.md

**Anti-criteria (what NOT to do):**
- Do not add dependencies not in CLAUDE.md ("just in case")
- Do not create deeply nested service modules with no implementation
- Do not add a Dockerfile yet (later task)

**Suggested prompt for Claude:**
```
Per CLAUDE.md, bootstrap the FastAPI backend project structure
following Architecture §7.1 exactly. Use uv for dependency
management. Initialize pyproject.toml with only the allowed
dependencies. Create empty module skeleton with __init__.py files,
.gitignore, pytest config, ruff/black config, and a smoke test
that just imports app.main. Run pytest at the end to verify.
```

---

### Task 2 — Configuration and structured logging

**Goal:** Settings loaded from environment, structlog configured to emit JSON, request_id correlation works.

**Steps:**
1. Implement `app/config.py` with pydantic Settings:
   - `NETBOX_URL`, `NETBOX_SERVICE_TOKEN` (from secret file or env)
   - `KEYCLOAK_BASE_URL`, `KEYCLOAK_REALM` (default: `prod-v1`)
   - Two `computed_field` properties exposed: `keycloak_issuer = "{base}/realms/{realm}"` (OIDC-compliant) and `jwks_url = "{issuer}/protocol/openid-connect/certs"` (used by Task 4)
   - `DATABASE_URL`
   - `LOG_LEVEL` (default `INFO`)
   - `JWKS_CACHE_TTL_SECONDS` (default 3600)
   - All fields validated, missing required ones fail fast at startup
2. Implement `app/observability/logging.py`:
   - Configure `structlog` with JSON renderer
   - Add `contextvars` processor for request_id correlation
3. Implement request_id middleware in `app/main.py`:
   - Extract `X-Request-ID` from header or generate UUID4
   - Bind it to `structlog.contextvars`
   - Pass through to downstream calls

**Acceptance criteria:**
- Unit test: `tests/unit/test_config.py` — loading config from a dict produces validated Settings; missing required field raises
- Unit test: `tests/unit/test_logging.py` — log output is valid JSON, contains `request_id` field when bound
- Manual: run `uvicorn app.main:app` and `curl http://localhost:8000/_test` (temporary test route logs and returns 200); confirm logs are JSON in stdout with `request_id`

**Suggested prompts:**
```
Implement app/config.py with pydantic-settings, including all
environment variables from CLAUDE.md detailed rules. Add a unit
test confirming validation works.
```
```
Implement structured logging via structlog in
app/observability/logging.py. JSON output, contextvars for
request_id correlation. Add a FastAPI middleware in app/main.py
that generates or propagates X-Request-ID and binds it to logs.
```

---

### Task 3 — Database connection and migration scaffold

**Goal:** Async SQLAlchemy session works, Alembic is configured, an empty migration runs against a test database.

**Steps:**
1. Implement `app/db/session.py`:
   - Async engine with `create_async_engine`
   - Session factory using `async_sessionmaker`
   - FastAPI dependency `get_session()` that yields an `AsyncSession`
2. Configure Alembic:
   - `alembic init alembic/`
   - Point at `app.db.models.Base.metadata` (will be empty for now)
   - Configure `script_location` and `sqlalchemy.url` to read from `app.config`
   - Override `env.py` to use async engine
3. Create an empty initial migration: `alembic revision --autogenerate -m "initial empty"`
4. Verify the migration runs against a test Postgres (use docker-compose or a local install)

**Acceptance criteria:**
- `alembic upgrade head` runs against a fresh test Postgres without errors
- Integration test `tests/integration/test_db.py` — opens an async session, executes `SELECT 1`, closes; passes
- Migration file is committed but empty (no operations yet, since no models exist)

**Note:** Per CLAUDE.md, destructive migrations are split across two releases. Document this in `alembic/README.md` so future engineers don't forget.

**Suggested prompt:**
```
Set up async SQLAlchemy 2.0 with PostgreSQL per CLAUDE.md stack
constraints. Implement app/db/session.py with async engine and
session factory. Configure Alembic for async migrations. Create
empty initial migration. Add integration test that opens a
session and runs SELECT 1.
```

---

### Task 4 — JWKS client and JWT validation

**Goal:** JWT tokens from Keycloak `prod-v1` realm can be validated, the user identity extracted, and an `AuthUser` dataclass exposed to downstream code.

**Steps:**
1. Implement `app/auth/jwks.py`:
   - JWKS cache that fetches from `settings.jwks_url` (computed in `app/config.py` from `KEYCLOAK_BASE_URL` + `KEYCLOAK_REALM`; matches OIDC issuer `{base}/realms/{realm}/protocol/openid-connect/certs`)
   - 1-hour TTL with proactive refresh
   - Background task or lazy-load on cache miss
2. Implement `app/auth/dependencies.py`:
   - `AuthUser` dataclass: `sub`, `email`, `roles`, `session_id`
   - `get_current_user()` FastAPI dependency: extracts Bearer token from `Authorization` header, validates signature against JWKS, checks `exp`, returns `AuthUser`
   - `require_role(role: str)` dependency factory: returns a dependency that checks the role is in `user.roles`
3. Test with **respx**-mocked Keycloak (do not connect to real Keycloak in tests):
   - Valid token → returns AuthUser
   - Expired token → raises 401
   - Wrong signature → raises 401
   - Missing role → raises 403

**Acceptance criteria:**
- Unit tests `tests/unit/auth/test_jwks.py` and `tests/unit/auth/test_dependencies.py` cover all paths
- 100% coverage of `app/auth/` per CLAUDE.md "Domain logic: 100% unit test coverage"
- No real network calls in tests (verified via respx assertion)
- Manual: with a real token from Keycloak, `get_current_user()` extracts the expected `email` and `roles`

**Suggested prompt:**
```
Use the planner agent first: draft the approach for JWT validation
against Keycloak realm prod-v1, including JWKS caching strategy
and how to test without hitting real Keycloak. After plan approval,
implement app/auth/jwks.py and app/auth/dependencies.py via TDD.
Mock Keycloak with respx, never with hand-rolled httpx mocks.
```

---

### Task 5 — NetBox client (read-only)

**Goal:** Async HTTP client to NetBox with proper resilience patterns. Read operations only. Service token from config.

**Steps:**
1. Implement `app/netbox/client.py`:
   - `NetBoxClient` class wrapping `httpx.AsyncClient`
   - Auth via `Authorization: Token {service_token}` header
   - Per-request `X-Request-ID` propagated from contextvars
   - Retry policy: 3 attempts with exponential backoff (200ms, 600ms, 1800ms) on 5xx and connection errors
   - Timeouts: 5s for reads, 10s for writes (future)
   - **Do NOT** implement write methods yet — only `get()`
2. Implement pydantic models for NetBox responses we'll use in Sprint 2:
   - `Device`, `Site`, `Rack`, `Status` — minimal fields only
   - These live in `app/netbox/models.py`
3. Add FastAPI dependency `get_netbox_client()` that returns a shared client

**Acceptance criteria:**
- Integration tests `tests/integration/test_netbox_client.py` with respx:
  - Successful GET returns parsed pydantic model
  - 500 → retries 3x → eventually raises
  - 404 → raises immediately (no retry)
  - Timeout → raises after configured time
  - Request includes correct Authorization header and X-Request-ID
- ≥70% coverage on `app/netbox/`
- Manual: with a real NetBox URL and token in env, `await client.get("/api/status/")` returns NetBox status payload

**Anti-criteria:**
- No write methods (PATCH/POST/DELETE) yet — Sprint 2 task
- No caching layer yet — out of scope until we have device endpoints

**Design note — future caching layer (do NOT implement in Sprint 1):**

When caching is added alongside device endpoints (Sprint 3), the approach is:

- **In-process TTL cache only.** No Redis — adding it would require dependency approval, and per-instance memory is sufficient for the load (single-DC, ~500 devices, small handful of concurrent users).
- **Static lookups only**, 5-minute TTL: `sites`, `racks`, `statuses`, `device-types`, `manufacturers`, `device-roles`. These change rarely and don't participate in optimistic concurrency.
- **Device responses are NEVER cached beyond 60 seconds** (CLAUDE.md caching policy + cross-cutting #1/#3). Stale device data would break OCC and let one engineer overwrite another's change.
- Implementation uses SQLAlchemy 2.0 `select()` style only — never the legacy 1.4 `Query` API (CLAUDE.md stack constraints).

This note exists so the design decision survives until Sprint 3 picks it up.

**Suggested prompt:**
```
Implement the NetBox client (read-only) per Architecture §3 and
§7.2. Use httpx async with retry-with-backoff and request_id
propagation. Cover with integration tests using respx. Per
CLAUDE.md, no hand-rolled httpx mocks. No write methods yet.
```

---

### Task 6 — Health endpoint with real downstream checks

**Goal:** `GET /health` reports liveness AND readiness by checking database, NetBox, and Keycloak.

**Steps:**
1. Implement `app/api/v1/health.py`:
   - `GET /health` returns 200 with JSON `{"status": "ok", "checks": {...}}` if all downstreams reachable
   - Returns 503 with same structure but `"status": "degraded"` if any check fails
   - Checks: DB session opens and runs SELECT 1; NetBox `/api/status/` returns 200; Keycloak JWKS endpoint returns 200
   - Each check has its own timeout (2s) so a slow downstream doesn't block the health endpoint
2. Add the route to the main router in `app/main.py`
3. Add unit tests for each check independently, and an integration test for the combined endpoint

**Acceptance criteria:**
- `curl localhost:8000/health` returns 200 with all three checks `"ok"` when downstreams are up
- With NetBox stopped, returns 503 with `"netbox": "unreachable"` but other checks may still be ok
- Total endpoint latency ≤ 3 seconds even when one check times out
- Tests cover all-ok, db-down, netbox-down, keycloak-down scenarios

**Suggested prompt:**
```
Implement /health endpoint per Architecture §5.5. Real downstream
checks (DB, NetBox, Keycloak), per-check timeout, returns 200/503.
TDD: write the failure-mode tests first (each downstream down),
then implementation.
```

---

### Task 7 — Docker Compose stack

**Goal:** `docker compose up` brings up the entire backend stack locally.

**Steps:**
1. Write `Dockerfile` for the backend (multi-stage: builder + slim runtime)
2. Write `docker-compose.yml` with services:
   - `dcinv-db` — Postgres 15
   - `dcinv-backend` — built from Dockerfile, depends on db
   - (no proxy yet — that's a deployment-time concern)
3. Add `.env.example` documenting all environment variables (no secrets committed)
4. Backend container runs `alembic upgrade head` on startup, then `uvicorn`
5. Healthcheck in compose file calls `/health`

**Acceptance criteria:**
- `docker compose up` brings the stack up healthy within 30 seconds
- `curl localhost:8000/health` returns 200 (NetBox and Keycloak checks may fail since they're external — adjust assertion accordingly)
- `docker compose down` shuts everything down cleanly
- `.env.example` exists and documents every env variable

**Suggested prompt:**
```
Containerize the backend with Docker. Write a multi-stage
Dockerfile (slim runtime, no build tools in final image). Write
docker-compose.yml with dcinv-backend and dcinv-db. Backend
runs alembic upgrade head before uvicorn. Add .env.example.
Add a healthcheck calling /health.
```

---

### Task 8 — CI skeleton

**Goal:** GitHub Actions workflow runs lint, type check, and tests on every PR.

**Steps:**
1. Create `.github/workflows/ci.yml`:
   - Trigger on push to main and on PR
   - Job: lint (ruff, black --check)
   - Job: typecheck (mypy)
   - Job: tests (pytest with coverage report)
   - All three jobs run in parallel matrix on Python 3.12
2. Add `coverage` configuration in pyproject.toml: fail under 70%
3. Add status badge to README

**Acceptance criteria:**
- Push a trivial PR (e.g., README edit) — CI passes
- Introduce a deliberate lint violation — CI fails on lint job
- Introduce a failing test — CI fails on test job
- Coverage report visible in CI logs

**Suggested prompt:**
```
Set up GitHub Actions CI. Three parallel jobs: lint (ruff + black),
typecheck (mypy), tests (pytest with coverage ≥70%). All on
Python 3.12. Reference CLAUDE.md test discipline section.
```

---

## Definition of Done for Sprint 1

The sprint is complete when:

- [ ] All 8 tasks meet their acceptance criteria
- [ ] `docker compose up` brings up a working stack locally
- [ ] `/health` reports honestly on all three downstreams
- [ ] CI is green on the main branch
- [ ] Coverage ≥70% on `app/auth/`, `app/netbox/`, `app/observability/`
- [ ] No business logic exists yet (no QR, no device endpoints, no audit) — this is intentional
- [ ] `docs/sprint-1-retrospective.md` written: what went well, what surprised, what to change for Sprint 2

## Sprint 2 preview (for context — do NOT start yet)

After Sprint 1, the foundation supports:

- Sprint 2: QR registry (DB schema, generation, lifecycle state machine)
- Sprint 3: Device read endpoints and `/qr/{id}` lookup
- Sprint 4: Device write endpoints with OCC and three-record audit
- Sprint 5: Web admin interface for batches and PDF generation
- Sprint 6: Production hardening, deployment to staging

## Working principles for this sprint

1. **TDD everywhere.** Per CLAUDE.md test discipline, every test has a failure-mode counterpart. Skip TDD for trivial config-only tasks (Task 7, 8); apply rigorously elsewhere (Tasks 2–6).

2. **Use the planner agent for non-trivial tasks.** Tasks 4 (auth) and 5 (NetBox client) involve enough design choices to warrant a written plan before coding.

3. **Reference CLAUDE.md and Architecture explicitly.** Every PR description should cite which sections of CLAUDE.md or Architecture it implements. This habit pays off in code review six months later.

4. **One task per PR.** Do not bundle. Small PRs are easier to review and revert.

5. **No "while I'm here" refactors.** If you spot something to fix outside the current task's scope, file it as a TODO comment with a date and your initials. Do not silently expand scope.

6. **Ask, don't assume.** When CLAUDE.md is unclear, ask in the agent session before writing code. Architecture §11 deliberately leaves some questions open — surface them, do not silently resolve.

## How to start

Open the agent in VS Code, in a new session (so CLAUDE.md and this file are loaded fresh), and say:

```
Read CLAUDE.md and docs/sprint-1.md. Then start Task 1.
Follow the acceptance criteria and anti-criteria strictly.
At the end of Task 1, report back what you did and which
acceptance criteria are met before I approve Task 2.
```

After Task 1 is approved, repeat for Task 2, and so on. Do not let the agent self-approve and move on — the gate is yours.
