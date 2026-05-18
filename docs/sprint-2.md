# Sprint 2 — QR Registry (Generation + Lookup)

> **Status:** Delivered 2026-05-17. See `docs/work-log.md` for the retrospective and any deviations.
> **Duration target:** 7–10 working days
> **Goal:** Functioning QR registry with batch generation and public lookup. Three new tables (`qr_batches`, `qr_codes`, `audit_log`) plus an idempotency layer. The service layer is exercised end-to-end via two HTTP entry points. **No NetBox writes** — `bind`/`retire` and the full three-record write apparatus land in Sprint 3.

## Why this sprint exists

Sprint 1 delivered a runnable foundation but no business logic. Sprint 2 adds the **first feature** of the system: the QR registry.

The QR registry is the unique value of the product. Without it, the backend is just a NetBox proxy. Generation and lookup are the bounded slice that:

- Doesn't require NetBox writes (so we don't have to build the three-record write pattern yet)
- Establishes the patterns future sprints copy: domain types, repository layer, service layer, idempotency, audit logging
- Forces the database invariants (CHECK constraint + partial unique index) into the schema from day one — the cross-cutting rule #4 in CLAUDE.md is easier to honour from the start than to retrofit
- Pre-creates the `audit_log` table with the **full** ToR §7.2.3 schema, so Sprint 3's device update doesn't need a separate migration

Sprint 3 will then add the `bind`/`retire` operations on top of this registry, alongside the device update endpoint, since both share the three-record write pattern.

## Scope boundaries

**In scope:**

- Migrations for `qr_batches`, `qr_codes`, `audit_log`, `idempotency_keys` (all per ToR §7.2 + Sprint-2-specific table)
- Domain types in `app/domain/` (pure Python, no SQLAlchemy/Pydantic) with state-transition validation
- Repository layer (`app/db/repositories/`) for the new tables
- Token generation: stdlib `secrets.choice` against the ToR §4.2.1 alphabet, with collision retry
- Idempotency layer (PostgreSQL-backed, 24h TTL)
- `QRGenerationService.generate_batch` — single transaction inserting batch + codes + audit row
- `QRLookupService.get_by_id` — public-flavoured lookup
- `POST /api/v1/admin/batches/` and `GET /api/v1/admin/batches/{id}` (admin role)
- `GET /api/v1/qr/{qr_id}` (mobile role)

**Out of scope (deferred to Sprint 3+):**

- `bind` and `retire` operations (require NetBox PATCH + journal entry → full three-record write apparatus)
- Device read / update / decommission endpoints (Sprint 3, share the three-record write apparatus)
- PDF label generation (Architecture §11.1 open question — `reportlab` vs `weasyprint` vs `fpdf2`)
- Web admin pages (separate sprint after API stabilises)
- `shift_sessions` table (added when session-start/end endpoints are built)
- `GET /api/v1/admin/audit` query endpoint (Sprint 3+ when there's interesting data to query)
- `GET /api/v1/admin/batches/` list endpoint with filters (out of immediate need; add when web admin lands)
- `GET /api/v1/admin/batches/{id}/pdf` (depends on PDF library decision)

## Cross-cutting decisions

These apply across multiple tasks; capturing once so each task doesn't re-litigate.

1. **Token generation uses stdlib `secrets.choice`**, not the `nanoid` library. ToR §4.2.1 specifies a fixed alphabet (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, 32 chars excluding `I/O/0/1`). `secrets.choice(alphabet)` covers it in a few lines and adds no new dependency. If the team later wants `nanoid` for ergonomics, that's a sprint-deviation justified in `docs/work-log.md`.
2. **Endpoint paths follow ToR §8.3 verbatim**: `/api/v1/admin/batches/` (with trailing slash, no `/qr/` segment). The mobile lookup follows §8.2: `/api/v1/qr/{qr_id}`.
3. **`audit_log` table is created with the FULL schema from ToR §7.2.3** (12 columns), even though only `qr.generate_batch` operations are written in Sprint 2. The columns Sprint 2 won't yet populate (`session_id`, possibly empty `before_json`/`after_json`) are nullable or default-empty. This avoids a destructive migration in Sprint 3 when `device.update` arrives — see CLAUDE.md cross-cutting #7.
4. **Idempotency storage is PostgreSQL** (Architecture §11.3 closed: not Redis). Dedicated `idempotency_keys` table, 24h TTL, scoped by `(user_keycloak_id, key)` to prevent one user's key from colliding with another's.
5. **Domain types in `app/domain/` are pure Python** — `@dataclass`, no SQLAlchemy, no Pydantic (CLAUDE.md naming conventions). State-transition validation lives here, not in the service. Repositories return domain types; SQLAlchemy models stay inside `app/db/`.
6. **Coverage target: 100% on `app/`** — same bar Sprint 1 closed at. Lower target only if discussed and recorded.
7. **Test discipline mirrors Sprint 1**: respx for any HTTP mock, real Postgres (`docker-compose.test.yml`) for repository tests, no happy-path-only tests, 100% on `app/` per the Sprint 1 close-out gate.

## Task list

Tasks ordered for dependency. Do not start task N+1 until task N's acceptance criteria are met. Each task sized for half a day to a day of focused work.

---

### Task 1 — Domain types + state machine

**Goal:** Pure-Python domain layer for `QR`, `QRBatch`, `QRStatus`, with state-transition validation that raises on illegal transitions. Repositories and services in later tasks return and consume these types.

**Steps:**

1. Create `app/domain/qr.py` with:
   - `class QRStatus(StrEnum): FREE, BOUND, RETIRED`
   - `@dataclass(frozen=True, slots=True) class QR` with fields per ToR §7.2.2 (`id, batch_id, status, bound_to_device_id, bound_at, bound_by_email, retired_at, retired_reason`)
   - **`__post_init__` enforces the same state-consistency invariant the DB CHECK enforces** (Architecture §4), so an illegal QR can't be constructed even before it touches Postgres:
     - `FREE`: `bound_to_device_id IS NULL AND retired_at IS NULL`
     - `BOUND`: `bound_to_device_id IS NOT NULL AND retired_at IS NULL`
     - `RETIRED`: `retired_at IS NOT NULL`
     Raises `ValueError` on violation. Mirroring the DB CHECK in code catches bugs at the call site instead of as opaque `IntegrityError`s deep in a transaction.
   - `@dataclass(frozen=True, slots=True) class QRBatch` with fields per ToR §7.2.1
   - State-transition methods on `QR`:
     - `bind(device_id, by_email)` → returns a new `QR` in BOUND state, raises `IllegalQRTransition` if not currently FREE
     - `retire(reason)` → returns a new `QR` in RETIRED state, raises `IllegalQRTransition` if currently RETIRED (FREE→RETIRED is allowed: unused codes can be discarded)
   - `class IllegalQRTransition(Exception)` with the from/to states in the message
2. Create `app/domain/__init__.py` re-exports.
3. Tests in `tests/unit/domain/test_qr.py`:
   - **`__post_init__` validation:** legal combinations construct cleanly; illegal combinations raise `ValueError` — explicit cases:
     - `FREE` with `bound_to_device_id` set → ValueError
     - `FREE` with `retired_at` set → ValueError
     - `BOUND` without `bound_to_device_id` → ValueError
     - `BOUND` with `retired_at` set → ValueError
     - `RETIRED` without `retired_at` → ValueError
   - All legal transitions: `FREE → BOUND`, `FREE → RETIRED`, `BOUND → RETIRED`
   - All illegal transitions: `BOUND → FREE`, `RETIRED → BOUND`, `RETIRED → FREE`, `RETIRED → RETIRED`
   - Frozen dataclass equality semantics

**Acceptance criteria:**

- `pytest tests/unit/domain/` passes; 100% coverage on `app/domain/`
- `IllegalQRTransition` raised for every transition not in the ToR §4.2.3 state machine
- No SQLAlchemy or Pydantic imports in `app/domain/` (verify by `grep`)
- Even though `bind`/`retire` ops aren't exposed as endpoints in this sprint, the domain types are ready and tested for Sprint 3 to consume

**Anti-criteria:**

- Don't add a `to_dict` or `to_orm` method — separation of concerns; conversion lives at the boundary
- Don't import anything from `app.db` or `app.api`
- Don't add validation that belongs at the API boundary (e.g., max comment length)

**Suggested prompt:**

```
Implement the QR domain layer per Architecture §4 and ToR §7.2.
Pure Python in app/domain/qr.py: QR + QRBatch + QRStatus enum +
state-transition methods with IllegalQRTransition. TDD: write the
transition tests first (legal AND illegal per ToR §4.2.3 state
machine), then the implementation. 100% coverage on app/domain/.
No SQLAlchemy, no Pydantic.
```

---

### Task 2 — Migrations: `qr_batches`, `qr_codes`, `audit_log`

**Goal:** Three new tables with the full ToR §7.2 schema, including the database-level invariants for the QR state machine.

**Steps:**

1. `alembic revision -m "qr_batches qr_codes audit_log"` — write by hand (autogenerate is fine, but the CHECK constraint and partial unique index won't be picked up).
2. Create `qr_batches` per ToR §7.2.1: `id` UUID PK, `created_at`, `created_by_email`, `created_by_keycloak_id`, `count`, `intended_site_id`, `intended_location_id`, `intended_rack_id`, `comment`, `pdf_path` (nullable — populated when PDF generation lands).
3. Create `qr_codes` per ToR §7.2.2: `id` VARCHAR(13) PK (the `DCQR-XXXXXXXX` string), `batch_id` UUID FK → `qr_batches.id`, `status` ENUM, `bound_to_device_id`, `bound_at`, `bound_by_email`, `retired_at`, `retired_reason`.
4. Add the constraints from Architecture §4:

   ```sql
   ALTER TABLE qr_codes
     ADD CONSTRAINT qr_state_consistency CHECK (
       (status = 'free'    AND bound_to_device_id IS NULL AND retired_at IS NULL)
    OR (status = 'bound'   AND bound_to_device_id IS NOT NULL AND retired_at IS NULL)
    OR (status = 'retired' AND retired_at IS NOT NULL)
     );

   CREATE UNIQUE INDEX qr_one_per_device
     ON qr_codes (bound_to_device_id)
     WHERE status = 'bound';
   ```

5. Create `audit_log` per ToR §7.2.3 — **all 12 columns**: `id` BIGSERIAL PK, `request_id` UUID, `timestamp` TIMESTAMPTZ, `user_email`, `user_keycloak_id`, `session_id` UUID **NULLABLE** (until `shift_sessions` lands), `operation` VARCHAR(50), `entity_type` VARCHAR(50), `entity_id` VARCHAR(50), `before_json` JSONB DEFAULT `'{}'::jsonb`, `after_json` JSONB DEFAULT `'{}'::jsonb`, `result` ENUM (`success`, `failure`, `conflict`).
6. Indexes: `qr_codes(batch_id)`, `audit_log(timestamp DESC)`, `audit_log(entity_type, entity_id)`.
7. Add SQLAlchemy `Base`-bound models in `app/db/models/qr.py` and `app/db/models/audit.py` matching the schema.
8. Integration test in `tests/integration/test_migrations.py`: `alembic upgrade head` succeeds, all expected tables/indexes/constraints exist (query `pg_indexes`, `information_schema.check_constraints`).

**Acceptance criteria:**

- `alembic upgrade head` against fresh test DB runs cleanly (subprocess + Python API)
- `alembic downgrade base` reverses cleanly (no orphan tables)
- CHECK constraint rejects an INSERT of `('DCQR-XXXX', batch_id, 'bound', NULL, ...)` (verified in integration test)
- Partial unique index rejects two rows with the same `bound_to_device_id` and `status='bound'` (verified in integration test)
- Partial unique index ALLOWS multiple rows with the same `bound_to_device_id` if status differs (so a retired QR doesn't block a new binding)

**Anti-criteria:**

- Don't make `audit_log.session_id` non-nullable. `shift_sessions` doesn't exist yet; failing INSERTs would block Task 6.
- Don't add `ON DELETE CASCADE` to the FK from `qr_codes.batch_id` — QR records are forever (ToR §4.2.3: "QR IDs are never reused")
- Don't include `shift_sessions` in this migration — out of Sprint 2 scope
- Don't autogenerate the migration without manually adding the CHECK + partial unique index (autogenerate misses both)

**Suggested prompt:**

```
Write a single Alembic migration that creates qr_batches, qr_codes,
and audit_log per ToR §7.2. qr_codes gets the CHECK constraint and
partial unique index from Architecture §4 — autogenerate misses
both, write them by hand. audit_log gets ALL 12 columns from §7.2.3
(session_id nullable). Add SQLAlchemy models matching the schema.
Integration test: upgrade head succeeds, downgrade reverses, CHECK
and partial unique index actually reject illegal rows.
```

---

### Task 3 — Token generator with collision check

**Goal:** Generate `DCQR-XXXXXXXX` tokens per ToR §4.2.1, verify uniqueness against the registry, retry on collision.

**Steps:**

1. Create `app/services/qr/token.py`:
   - `_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"` (Crockford-ish, 32 chars, no `I/O/0/1` per ToR §4.2.1)
   - `_PREFIX = "DCQR-"`
   - `def generate_token() -> str:` returns `f"{_PREFIX}{''.join(secrets.choice(_ALPHABET) for _ in range(8))}"`
   - `async def generate_unique_token(repo: QRCodeRepository, *, max_retries: int = 10) -> str:` calls `generate_token()`, queries `repo.exists(token)`, retries on collision; raises `TokenGenerationExhausted` if all retries collide
2. Tests in `tests/unit/services/qr/test_token.py`:
   - Format: `^DCQR-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$`
   - Alphabet: 1000 generations contain no forbidden chars (`I`, `O`, `0`, `1`)
   - `generate_unique_token` returns a fresh token when no collision
   - **Collision retry test (per your request):** monkey-patch `generate_token` to return a known-existing value once, then a new value; assert two repo calls and the new value is returned
   - `TokenGenerationExhausted` raised after `max_retries` collisions

**Acceptance criteria:**

- `pytest tests/unit/services/qr/` passes; 100% coverage on `app/services/qr/token.py`
- `secrets` module used (cryptographically secure), not `random`
- 32^8 ≈ 1.1T address space verified by alphabet length × 8

**Anti-criteria:**

- Don't add `nanoid` dependency — stdlib covers this
- Don't lowercase or uppercase-normalize the token; the alphabet is fixed-case
- Don't try to "make collisions impossible" with `len > 8`; ToR fixes the format

**Suggested prompt:**

```
Implement DCQR token generator per ToR §4.2.1: stdlib
secrets.choice over the 32-char alphabet, 8-char suffix, DCQR-
prefix. Add a generate_unique_token that takes a QRCodeRepository
and retries on collision. TDD with the collision-retry test
(monkey-patch generate_token to return a known-existing value
first), and a TokenGenerationExhausted test. No nanoid dep.
```

---

### Task 4 — Repository layer

**Goal:** Async repositories for the three new tables, returning domain types.

**Steps:**

1. Create `app/db/repositories/__init__.py`.
2. `app/db/repositories/qr_code.py` — `QRCodeRepository`:
   - `__init__(self, session: AsyncSession)`
   - `async def get_by_id(qr_id: str) -> QR | None`
   - `async def find_by_batch_id(batch_id: UUID) -> list[QR]`
   - `async def exists(qr_id: str) -> bool`
   - `async def bulk_insert(codes: list[QR]) -> None`
3. `app/db/repositories/qr_batch.py` — `QRBatchRepository`:
   - `async def get_by_id(batch_id: UUID) -> QRBatch | None`
   - `async def insert(batch: QRBatch) -> None`
4. `app/db/repositories/audit_log.py` — `AuditLogRepository`:
   - `async def insert(entry: AuditLogEntry) -> None` (`AuditLogEntry` is a domain type in `app/domain/audit.py`)
5. SQLAlchemy ↔ domain conversion happens inside the repos via `_to_domain` / `_from_domain` private functions; keep model classes private to `app/db/`.
6. Integration tests in `tests/integration/test_qr_repositories.py` against the live test DB:
   - Each method round-trips correctly
   - `bulk_insert` of 50 codes runs in one transaction (capture connection.execute count or similar)
   - `find_by_batch_id` returns codes in deterministic order (sorted by `id`)

**Acceptance criteria:**

- `pytest tests/integration/` passes against `docker-compose.test.yml`
- 100% coverage on `app/db/repositories/`
- Repositories never raise SQLAlchemy exceptions to the caller — wrap `IntegrityError` into a typed `RepositoryError` so callers don't import SQLAlchemy

**Anti-criteria:**

- Don't return SQLAlchemy model instances — only domain types (CLAUDE.md naming conventions)
- Don't add `update_state` to `QRCodeRepository` yet — `bind`/`retire` ops are Sprint 3
- Don't put business logic in repositories (no "if status is bound, raise" — that lives in the domain or service)

**Suggested prompt:**

```
Implement QRCodeRepository, QRBatchRepository, AuditLogRepository
in app/db/repositories/. Async sessions injected. Methods return
domain types from app/domain/ (not SQLAlchemy models). Wrap
IntegrityError into RepositoryError so callers don't import
SQLAlchemy. Integration tests against the live test DB —
docker-compose.test.yml is on port 5433.
```

---

### Task 5 — Idempotency layer

**Goal:** PostgreSQL-backed idempotency for POST endpoints. `Idempotency-Key` header + same payload → cached response. Different payload with same key → 422.

**Steps:**

1. Migration: `idempotency_keys` table:
   - `id` BIGSERIAL PK
   - `user_keycloak_id` UUID — scoping prevents cross-user key collisions
   - `key` VARCHAR(255) — the client-supplied `Idempotency-Key`
   - `request_hash` VARCHAR(64) — SHA-256 of canonical request body
   - `response_status` SMALLINT
   - `response_body` JSONB
   - `created_at` TIMESTAMPTZ DEFAULT NOW()
   - UNIQUE constraint on `(user_keycloak_id, key)`
   - Index on `created_at` for TTL cleanup
2. `app/services/idempotency.py`:
   - `class IdempotencyResult` with `is_replay: bool`, `cached_response: dict | None`, `record(response_status, response_body)` method
   - `async def with_idempotency(session, user_keycloak_id, key, request_payload)` async context manager
   - On enter: SELECT for existing `(user_keycloak_id, key)`. If found and `request_hash` matches → set `is_replay=True`, populate `cached_response`. If found and hash mismatches → raise `IdempotencyKeyConflict` (mapped to 422 by API layer). If not found → INSERT a placeholder (with NULL `response_status`/`response_body`) **inside the same transaction** so the UNIQUE constraint serializes concurrent requests.
   - On exit: if work succeeded, UPDATE the placeholder with the response. If work raised, the transaction rolls back → idempotency row gone → next attempt is fresh.
3. Tests in `tests/unit/services/test_idempotency.py` and `tests/integration/test_idempotency.py`:
   - Replay: same key + same payload → returns cached response
   - Conflict: same key + different payload → raises `IdempotencyKeyConflict`
   - Cross-user isolation: same key from different `user_keycloak_id` → independent records
   - **Race condition test (per your request):** spawn two concurrent `asyncio` tasks both with the same key, both with the same payload → exactly one performs the work, the other waits and reads the cached response. Verify via a counter mutated inside the work-block.
   - TTL cleanup: rows older than 24h are deletable via a query (the actual cleanup job is out of scope; just verify the index supports it)

**Acceptance criteria:**

- `pytest tests/{unit,integration}/test_idempotency*.py` passes
- 100% coverage on `app/services/idempotency.py`
- Race-condition test passes deterministically (run 10 times in CI to verify stability)

**Anti-criteria:**

- Don't add Redis (Architecture §11.3 closed: PostgreSQL)
- Don't auto-clean expired rows in this sprint — that's a separate background-job sprint when one is needed; expose a query others can run instead
- Don't bypass the UNIQUE constraint to "optimize" — the constraint IS the serialization mechanism

**Suggested prompt:**

```
Implement PostgreSQL-backed idempotency in app/services/idempotency.py
as an async context manager. Storage: idempotency_keys table with
UNIQUE(user_keycloak_id, key). Race-condition handling via the
UNIQUE constraint — concurrent requests with the same key serialize.
Tests must cover: replay (same payload), conflict (different payload),
cross-user isolation, AND a race-condition test using asyncio.gather
of two concurrent requests with a counter inside the work-block.
```

---

### Task 6 — `QRGenerationService` + audit log

**Goal:** Service that generates a batch end-to-end: insert batch row, generate N unique tokens, bulk insert code rows, write `audit_log` row — **all in one transaction**.

**Steps:**

1. Create `app/services/qr/generation.py`:
   - `class QRGenerationService`
   - `async def generate_batch(self, request: GenerateBatchRequest, user: AuthUser) -> QRBatch`
     - Open transaction
     - Insert `QRBatch` row (`created_by_email=user.email`, `created_by_keycloak_id=user.sub`)
     - Loop N times calling `generate_unique_token(repo)`
     - `repo.bulk_insert([QR(...), ...])` (status=FREE)
     - Insert `audit_log` row: `operation='qr.generate_batch'`, `entity_type='batch'`, `entity_id=str(batch_id)`, `before_json={}`, `after_json={"count": N, "intended_site_id": ..., ...}`, `result='success'`, `request_id=current_request_id()`, `user_email`, `user_keycloak_id`, `session_id=NULL`
     - Commit
   - On failure inside the transaction: roll back, write a separate `audit_log` row with `result='failure'`, re-raise
2. Pull `request_id` from `structlog.contextvars.get_contextvars().get("request_id")`. Mint a UUID if absent (mirror `app/netbox/client.py:_current_request_id`).
3. Pydantic request schema `GenerateBatchRequest` with: `count: int (1..500)`, `intended_site_id: int | None`, `intended_location_id: int | None`, `intended_rack_id: int | None`, `comment: str | None (max 200)`.
4. Tests in `tests/unit/services/qr/test_generation.py` and `tests/integration/test_generation.py`:
   - Happy path: 50 codes generated, all unique, all FREE, batch row + audit row written
   - Validation failures (count=0, count=501, comment too long) raise before any DB write
   - Failure inside the transaction: monkey-patch `bulk_insert` to raise → assert no batch row, no code rows, but a `result='failure'` audit row exists in a separate transaction
   - Atomicity: monkey-patch `audit_log.insert` to raise after batch + codes inserted → assert NOTHING is committed (single transaction)

**Acceptance criteria:**

- `pytest tests/unit/services/qr/ tests/integration/test_generation.py` passes
- 100% coverage on `app/services/qr/generation.py`
- Verified: a failed generation produces a `result='failure'` audit row, but no orphan batch/codes
- Verified: a successful generation produces exactly one `audit_log` row with `result='success'`

**Anti-criteria:**

- Don't write a NetBox journal entry — there's no NetBox write in this operation
- Don't validate `intended_site_id` against NetBox — Sprint 3 will (when the device read endpoint is in)
- Don't accept counts above 500 — ToR §5.1 says "QR batch generation (50 codes)" as a benchmark; 500 keeps memory bounded

**Suggested prompt:**

```
Implement QRGenerationService.generate_batch in app/services/qr/
generation.py. Single transaction: insert qr_batches row, generate
N unique tokens (Task 3), bulk insert qr_codes (status=FREE), insert
audit_log row (operation='qr.generate_batch', result='success').
On any failure inside the transaction, roll back AND write a
separate audit_log row with result='failure'. Pull request_id from
structlog contextvars. Pydantic request schema with count 1..500
and comment max 200. Tests must cover atomicity (audit insert
failure rolls back batch+codes too).
```

---

### Task 7 — Lookup service + HTTP endpoints

**Goal:** Three endpoints wiring the services to the outside world. Pydantic schemas for request/response. Role enforcement via `require_role`.

**Steps:**

1. Create `app/services/qr/lookup.py`:
   - `class QRLookupService`
   - `async def get_by_id(qr_id: str) -> QRLookupResult | None` — returns the QR plus its batch metadata (intended site/location/rack) plus, if BOUND, the bound device ID. Does not call NetBox in this sprint — bound device details surface in Sprint 3 once the device read endpoint exists.
2. Create `app/api/v1/admin/__init__.py`, `app/api/v1/admin/batches.py`:
   - `POST /api/v1/admin/batches/` — body = `GenerateBatchRequest`, header = `Idempotency-Key` (optional but recommended), depends on `require_role("dcinv-admin")`, depends on `with_idempotency` if header present, calls `QRGenerationService.generate_batch`, returns `BatchCreatedResponse` (201)
   - `GET /api/v1/admin/batches/{batch_id}` — depends on `require_role("dcinv-admin")`, returns `BatchDetailsResponse` (batch metadata + list of codes), 404 if not found
3. Create `app/api/v1/qr.py`:
   - `GET /api/v1/qr/{qr_id}` — depends on `require_role("dcinv-mobile-user")`, returns `QRLookupResponse`, 404 if not in registry
4. Register routers in `app/main.py` under their respective prefixes.
5. Pydantic response schemas:
   - `BatchCreatedResponse` — `batch_id, count, codes: list[QRCodeShort]`
   - `BatchDetailsResponse` — full batch metadata + `codes: list[QRCodeDetail]`
   - `QRLookupResponse` — `id, status, batch: {intended_site_id, intended_location_id, intended_rack_id}, bound_to_device_id?, bound_at?, retired_at?, retired_reason?`
6. Integration tests via `TestClient` in `tests/unit/api/v1/test_batches.py` and `tests/unit/api/v1/test_qr_lookup.py`:
   - Generate batch → response shape correct → DB has batch + codes
   - Generate batch with bad payload → 422
   - Generate batch without admin role → 403
   - Generate batch twice with same `Idempotency-Key` → second returns the first's response, no new batch in DB
   - Lookup unknown id → 404
   - Lookup with mobile role → 200
   - Lookup with admin role → 200 (admin is a superset)
   - Lookup with no role → 403
7. **Contract test** in `tests/unit/api/v1/test_batches_contract.py`:
   - Generate a batch via `TestClient`
   - Pull the auto-generated OpenAPI schema from `/openapi.json`
   - For the `POST /api/v1/admin/batches/` 201 response schema:
     - Assert all `required` fields from the schema are present in the actual response body
     - Assert each present field's runtime type matches the declared schema type (`string`/`integer`/`array`/`object`)
   - Catches silent schema drift when Sprint 3+ adds a field to the response without updating downstream consumers' expectations. Not a full JSON-Schema validator — keep it minimal.

**Acceptance criteria:**

- `pytest tests/unit/api/v1/test_batches.py tests/unit/api/v1/test_qr_lookup.py` passes
- 100% coverage on `app/api/v1/admin/` and `app/api/v1/qr.py`
- Manual smoke (after Task 8): `curl -X POST -H 'Authorization: Bearer ...' -H 'Idempotency-Key: foo' .../api/v1/admin/batches/ -d '{"count":10}'` returns 201 with 10 fresh tokens; second call with same key returns the same response

**Anti-criteria:**

- Don't expose `audit_log` rows in the response — it's an internal forensics table
- Don't include `retired_reason` in the lookup response when the QR is FREE/BOUND — only when `RETIRED` (the JSON shouldn't carry NULL noise for irrelevant states)
- Don't add a `GET /api/v1/admin/batches/` list endpoint yet — out of scope, no immediate consumer

**Suggested prompt:**

```
Wire up the QR endpoints per ToR §8.2 and §8.3:
- POST /api/v1/admin/batches/ (dcinv-admin, idempotency)
- GET  /api/v1/admin/batches/{batch_id} (dcinv-admin)
- GET  /api/v1/qr/{qr_id} (dcinv-mobile-user)
Pydantic schemas with explicit shape; lookup response omits
retired_reason for non-retired states. Integration tests via
TestClient covering: happy path, validation 422, role 403,
idempotency replay, lookup 404. 100% coverage on app/api/v1/.
```

---

### Task 8 — Acceptance and close-out

**Goal:** Sprint 2 verified end-to-end and history recorded. Same close-out shape as Sprint 1.

**Steps:**

1. Build the image: `docker compose up -d --build`. Ensure the new migration runs at startup (`Running upgrade 068437e38dd9 -> <new>` in entrypoint logs).
2. Manual smoke. **Two valid paths, no third option:**
   - **Real Keycloak (preferred)** — via VPN, with a real `dcinv-admin` token. Run the round-trip:
     - Generate a batch of 50 codes
     - Lookup one of the codes via `GET /api/v1/qr/{id}`
     - Verify the audit row appears via `psql` (no admin/audit endpoint yet)
     - Re-run with same `Idempotency-Key`, verify same response and no duplicate batch
   - **Skip manual smoke** if Keycloak is not reachable. The integration tests (respx-mocked Keycloak + real Postgres) cover the round-trip. Note this in the work-log entry.

   **Do NOT** add a dev-only JWT bypass / stub auth mode. Security antipattern: too easy to forget and ship a build with auth disabled.
3. Run the full test matrix:
   - `uv run pytest --cov=app --cov-fail-under=100`
   - `uv run ruff check .`
   - `uv run black --check .`
   - `uv run mypy app tests`
4. Update `docs/work-log.md` with the Sprint 2 retrospective (mirror Sprint 1's section): what shipped, deviations from plan, deferred items, what went well / what slowed us down.
5. Update `CLAUDE.md` "Repository Status" line to reflect Sprint 2 closed (one-line edit).
6. If anything was deferred mid-sprint, append it to `docs/work-log.md` Sprint 2 "deliberately deferred" list with the reason.

**Acceptance criteria:**

- All test/lint/type/coverage commands pass
- Manual smoke validates the round-trip
- `docs/work-log.md` has the Sprint 2 entry committed
- `CLAUDE.md` Repository Status reflects Sprint 2 closed

**Anti-criteria:**

- Don't start Sprint 3 work as part of close-out
- Don't add new tests in close-out — they belong to whichever task added the code
- Don't move open items from "deferred" to "in scope" without an explicit user-approved scope expansion

---

## Working principles (carried from Sprint 1)

- **TDD discipline.** Tests first, including failure-mode counterparts. No happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit "go", then code.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are met. Gate is the user's, not the agent's.
- **Coverage 100%** on `app/` per the bar set in Sprint 1 close-out.
- **No new dependencies** without explicit approval. Version bumps of existing deps are allowed when justified (record the reason in `docs/work-log.md`).
- **CLAUDE.md cross-cutting rules** (#1–#7) are non-negotiable. The fact that Sprint 2 doesn't write to NetBox is *because* `bind` is a NetBox write, and NetBox writes need three records — that's why it's deferred to Sprint 3.
- **Integration tests assume `docker-compose.test.yml` is running** (port 5433, credentials `dcinv_test`/`dcinv_test`/`dcinv_test`). Sprint 1 established this — see `docs/work-log.md` Sprint 1 entry for the exact command.

## Reference documents

- ToR §4.2 (QR Code Management), §4.3 (Device Operations including QR scan flow), §7.2 (Application Database Schema), §8 (API endpoints)
- Architecture_Overview.md §3 (NetBox interaction patterns — relevant for Sprint 3 context), §4 (QR state machine — diagram + SQL)
- Sprint 1 retrospective in `docs/work-log.md`
- CLAUDE.md cross-cutting rules #1, #2, #4, #6, #7 (all touch this sprint)
