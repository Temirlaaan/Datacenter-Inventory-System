# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Status

Sprint 4 (QR Lifecycle Completion) closed 2026-05-24. The `backend/` directory contains a runnable FastAPI service with auth, async DB + Alembic, NetBox read+write client, `/health`, and a docker-compose stack. Business surface so far: Sprint 2's QR registry (`POST /api/v1/admin/batches/`, `GET /api/v1/admin/batches/{id}`); Sprint 3's device read + update (`GET`/`PATCH /api/v1/devices/{id}`) on the three-record-write apparatus, plus NetBox static lookups behind a 5-minute cache (`GET /api/v1/meta/{sites,racks,statuses}`) and the server-driven device-edit form config (`GET /api/v1/meta/device-form`); Sprint 4's QR lifecycle — `POST /api/v1/qr/{id}/bind` (role `dcinv-mobile-user`) and `POST /api/v1/qr/{id}/retire` (role `dcinv-admin`) with atomic free→bound and bound→retired transitions plus explicit three-branch compensation (clear/restore conditional and idempotent), and the combined `GET /api/v1/qr/{id}` returning QR + bound-device in one call (response shape changed from Sprint 2's flat shape — see work-log). Still to come — device decommission/create, add-comment, PDF labels, and the web admin all land in Sprint 5+.

- `Architecture_Overview.md` — the technical *how*...
- `DC_Inventory_ToR_v3.docx` — the formal Terms of Reference...
- `docs/sprint-1.md` — Sprint 1 plan (delivered)
- `docs/sprint-2.md` — Sprint 2 plan (delivered)
- `docs/sprint-3.md` — Sprint 3 plan (delivered)
- `docs/sprint-4.md` — Sprint 4 plan (delivered)
- `docs/work-log.md` — running log of what shipped, what was deferred, and per-sprint retrospectives. **Authoritative for sprint history.**

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