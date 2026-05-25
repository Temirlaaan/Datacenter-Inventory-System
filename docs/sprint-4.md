# Sprint 4 — QR Lifecycle Completion

> **Status:** Planned. Awaiting full task breakdown.
> **Duration target:** 5–7 working days
> **Goal:** Complete the QR lifecycle. Ship `bind` (free→bound) and `retire`
> (free→retired and bound→retired) on top of Sprint 3's three-record-write
> apparatus, and extend `GET /api/v1/qr/{id}` to fetch the bound device from
> NetBox so the QR scan flow (ToR §4.3.1) returns the full device-screen field
> set in one call.

## Why this sprint exists

Sprint 2 shipped QR generation + lookup (free state only). Sprint 3 built the
three-record-write apparatus and exercised it for device update — but never
touched a QR write path. Sprint 4 finally exercises that apparatus for what it
was built for, and completes two unfinished arcs:

1. **The QR state machine.** The domain (`app/domain/qr.py`, Sprint 2) supports
   all transitions; the database (`qr_codes` CHECK + `qr_one_per_device` partial
   unique index, Sprint 2) enforces them; but there is no write endpoint to
   trigger `free→bound` or `*→retired`. Sprint 4 adds them.
2. **The QR scan flow** (ToR §4.3.1). For a *bound* QR, the mobile app expects
   a *combined* response carrying the linked device's full ToR §4.3.3 field set
   — not just the QR data Sprint 2's `GET /api/v1/qr/{id}` returns. Sprint 3
   Task 5 deliberately scoped the device-screen field set to Sprint 4 because
   "the device screen is delivered via the combined response" (sprint-3.md
   "Out of scope"). Sprint 4 delivers it.

Bind is the first orchestration where a DB transition (`qr_codes` free→bound)
must be coordinated with a NetBox write — Architecture §4 mandates this
atomicity to make partial states impossible. Sprint 3's
`patch_with_attribution` doesn't compose this case (its audit row writes in a
separate transaction). Task 1 builds the right orchestration on top of Sprint
3's pieces (journal + audit + re-read + version compare); it doesn't rewrite
them.

## Scope boundaries

**In scope — 4 tasks (detailed breakdown TBD, see Task list below):**

1. QR bind (atomic free→bound + NetBox attribution)
2. QR retire (free→retired and bound→retired)
3. Combined QR+device response (extend `GET /api/v1/qr/{id}` + extend device
   parsing to ToR §4.3.3's full field set)
4. Acceptance + close-out

**Out of scope (deferred to Sprint 5+):**

- **Device decommission** (status → `Decommissioning` + retire the bound QR) —
  Sprint 5. Also gated on a NetBox config dependency (the additional device
  statuses) per `docs/parking-lot.md`. The retire service from this sprint
  will be reused by Sprint 5's decommission flow.
- **Device creation** (`POST /api/v1/devices/`) — Sprint 5.
- **Add-comment endpoint** (`POST /api/v1/devices/{id}/comments` — a NetBox
  journal POST without a device PATCH) — Sprint 5.
- `shift_sessions` table + `POST /api/v1/sessions/{start,end}` — later sprint.
- PDF label generation, web admin pages — later sprints.
- NetBox circuit breaker (Architecture §3.3) — still deferred (Sprint 3
  decision D).
- `GET /api/v1/admin/audit` query endpoint — later, when there's a use case.
- Idempotency-key TTL cleanup job — pre-existing deferral.

## Cross-cutting decisions

These apply across multiple tasks; capturing once so each task doesn't
re-litigate. Proposed for Sprint 4 — to be confirmed during skeleton review.

**A. QR-bind atomicity + explicit compensation** (Architecture §4 — "free →
bound happens inside a database transaction together with the NetBox write, so
partial states are impossible"). The orchestration writes `qr_codes`
(free→bound) inside a DB transaction, PATCHes NetBox to attach the QR token to
the device, then commits the DB. NetBox-PATCH failure rolls back the DB → QR
stays free.

The DB-commit-after-successful-NetBox-PATCH asymmetric case (Architecture §4)
is handled by **explicit compensation**, not "log for human cleanup":

1. Attempt a compensating NetBox PATCH that clears `custom_fields.qr_id` on the
   device (direct client call — **not** through `patch_with_attribution`,
   since that would write a confusing second journal entry attributing the
   compensation as a regular bind).
2. If compensation succeeds → log error `qr_bind_db_failed_netbox_compensated`
   with `{qr_token, device_id, request_id}`, return 500 "Bind failed (rolled
   back)". System is left consistent.
3. If compensation fails → log critical
   `qr_bind_inconsistency_unrecoverable` with `{qr_token, device_id,
   request_id, compensation_error}`, write a NetBox journal entry naming the
   inconsistency (best-effort, log on its own failure), return 500 "Bind
   failed, manual cleanup required".

Task 2's BOUND→RETIRED uses the **same three-branch pattern in reverse**
(compensation restores the device's `qr_id`); the shared helpers live on
`QRLifecycleService`.

Journal + audit-row writes for the primary operation remain best-effort after
success (Sprint 3 decision B applies uniformly — **no per-call-site
override**). The audit row duplicates attribution already stored in
`qr_codes`; the QR row is the source of truth for the binding, so an audit
write failure must not break the bind.

**B. QR-bind NetBox write target — `custom_fields.qr_id` as design assumption.**
Sprint 4 proceeds with respx-mocked tests (same approach as Sprints 1-3, which
never had a live NetBox available either). The code writes the QR token to
`custom_fields.qr_id`; verification against the deployed NetBox is deferred to
the production deploy phase, where this and Sprint 3's `asset_tag` mapping get
confirmed together (see `parking-lot.md`). If either field name turns out
wrong, only one call site changes — the YAML's `netbox_field` (for
`asset_tag`) or the bind/retire NetBox-write payload (for `qr_id`) —
isolated by design.

**C. QR-retire NetBox write.** BOUND→RETIRED clears the bound device's
`qr_id` custom field via a NetBox PATCH (same write apparatus as bind, in
reverse). FREE→RETIRED is DB-only (the QR was never on a device → no NetBox
write, no journal entry; audit row only).

**D. Combined response — device-fetch failure handling.** If the bound device's
NetBox fetch fails (404 device-gone, 502 NetBox-down), `GET /api/v1/qr/{id}`
returns the QR data with `device: null` plus a soft error indicator. Don't
couple QR lookup availability to NetBox availability — the QR is in our DB and
should always be readable.

**E. Device field set on the combined response — additive on `DeviceData`.**
Sprint 3 Task 5 deferred the full ToR §4.3.3 field set (Identity,
Location/Height, Operational/IPs/Last-Updated/QR-ID, populated Custom Fields)
to Sprint 4. Extend `DeviceData` and `to_device_data` additively — one model,
one parser — so the standalone `GET /api/v1/devices/{id}` from Sprint 3 keeps
working unchanged and Sprint 4's combined response reuses it.

**F. QR-retire of a BOUND QR is allowed as a standalone operation.** The
domain supports it (Sprint 2). Sprint 5's decommission flow will call into the
same retire service rather than implementing its own retire path.

**G. No caching of the device fetch in the combined response.** Same call as
Sprint 3 for standalone device reads. The ≤60s ceiling stays available if
profiling ever shows a need.

**H. `qr_id` in the combined response — from the app DB, not NetBox.** ToR
§4.3.3 lists "QR ID" as a Device Screen field. It is authoritative in our
`qr_codes` table (the binding is owned by the app DB). The combined response
populates it from the QR row in `/qr/{id}` — even if NetBox's custom field
disagrees, the app DB wins (decision B's "journal-compensation" semantics
already accept the asymmetry).

**I. Retire endpoint role: `dcinv-admin`, not `dcinv-mobile-user`.** Bind is a
routine mobile-floor operation (label a new device); its endpoint is
`dcinv-mobile-user`. Retire is destructive (permanently un-attribute a label)
and stays `dcinv-admin` — safer default. Open to `dcinv-mobile-user` later if
the business asks; tightening a role after release is harder than loosening
one.

**J. Service layout — single `QRLifecycleService` in
`app/services/qr/lifecycle.py`.** Bind and retire (BOUND path) share the
SELECT-FOR-UPDATE + NetBox-PATCH-via-write-service + DB-update + compensation
pattern. One service exposing both methods avoids duplicating the compensation
helpers and keeps `app/services/qr/` at four files (`token.py`,
`generation.py`, `lookup.py`, `lifecycle.py`).

## Task list

Each task is detailed below with Goal / Steps / Acceptance criteria /
Anti-criteria / Suggested prompt, mirroring `docs/sprint-3.md`. Per the
working principles, every task gets its own plan-then-confirm gate **before**
any code lands.

---

### Task 1 — QR bind

**Goal:** `POST /api/v1/qr/{qr_token}/bind` performs the atomic `free→bound`
transition with NetBox attribution. Body: `{device_id: int, version: str}`
(device's expected `last_updated`). Returns `QRLookupResponse` (combined
QR+device — model introduced here, reused by Tasks 2 and 3). The
QR-bind atomicity (cross-cutting A) is realised here, including the explicit
three-branch compensation for the DB-commit-after-successful-NetBox-PATCH
asymmetric case.

**Steps:**

1. `app/services/qr/lifecycle.py` — new module. `QRLifecycleService` (DI:
   `NetBoxClient`, `AsyncSession`, `QRCodeRepository`, `AuditLogRepository`,
   `NetBoxWriteService`). Exceptions: `QRNotFoundError`,
   `QRStateConflictError` (carries `current_status`), `QRAlreadyBoundError`,
   `QRBindRolledBackError`, `QRBindInconsistencyError`. Private sentinel
   `_PostNetBoxStateRace` (underscore-prefixed, not exported) flags a
   FOR-UPDATE-time state mismatch from inside the DB-transaction block.
   (Task 2 adds `retire` to the same service + `MissingVersionError`.)

2. `QRLifecycleService.bind(qr_token, device_id, expected_version, user) ->
   tuple[QR, dict]` — **sequential transactions, not nested.**
   `patch_with_attribution` already owns its audit-row `session.begin()`
   (`app/services/netbox_write.py:134`); SQLAlchemy 2.0 async sessions don't
   nest `session.begin()` by default, and decision B says don't fragment the
   apparatus per call-site. So:

   **Step A — cheap pre-validation (no DB tx).**
   - Lookup QR via `QRCodeRepository.get_by_token(qr_token)`. Not found →
     `QRNotFoundError`.
   - If `qr.status != QRStatus.FREE` → `QRStateConflictError(current_status)`.
   - Defensive guard: `if session.in_transaction(): raise
     RuntimeError("bind called inside an active transaction — would conflict
     with patch_with_attribution's audit tx")`. Cheap runtime check that
     survives `python -O` (asserts get optimized out — Sprint 1 M3 fix
     established this pattern; **do not use `assert`**).

   **Step B — NetBox PATCH via `patch_with_attribution`.**
   - `netbox_path=/api/dcim/devices/{device_id}/`,
     `netbox_object_type="dcim.device"`, `netbox_object_id=device_id`,
     `entity_type="qr"`, `operation="qr.bind"`,
     `expected_version=expected_version`,
     `changes={"custom_fields": {"qr_id": qr_token}}`.
   - `WriteConflictError` / `NetBoxNotFound` / `NetBoxClientError` propagate
     — no PATCH happened (or the failure-audit row was already written by
     `patch_with_attribution`); endpoint maps to 409 / 404 / 502.
   - If this returns successfully, NetBox shows
     `custom_fields.qr_id = qr_token`. **From here, any failure demands
     compensation.**

   **Step A↔B race window — accepted.** Step A reads the QR without a row
   lock, so two concurrent binds for the same QR can both pass the
   pre-check and both reach Step B. Mitigations:
   - **Same-device race** (two QRs bound to one device): the
     second-to-commit fails the `qr_one_per_device` partial unique index in
     Step C → routes to Step D compensation → cleared (or no-op if Step D
     sees a different qr_id, see Step E).
   - **Different-device race** (same QR PATCHed onto two devices): Step C's
     FOR UPDATE re-check finds the QR already BOUND for the loser → routes
     to Step D compensation. Step E's conditional clear only PATCHes the
     device whose `qr_id` still equals our token, leaving any concurrent
     winner's binding intact.
   - **Probability:** very low — two operators binding the same physical
     QR sticker to different devices simultaneously is not a realistic
     mobile workflow.

   **Step C — DB transaction: FOR UPDATE + UPDATE qr_codes + commit.**
   ```
   try:
       async with session.begin():
           locked = await qr_repo.get_by_token_for_update(qr_token)
           if locked is None:
               raise _PostNetBoxStateRace("qr_disappeared")
           if locked.status != QRStatus.FREE:
               raise _PostNetBoxStateRace(f"qr_not_free:{locked.status.value}")
           bound = locked.bind(device_id)              # domain re-validation
           await qr_repo.update(bound)
       # __aexit__ runs session.commit() here
       return bound, updated_device                    # Branch 1: HAPPY
   except IntegrityError as race:                      # qr_one_per_device
       await _run_compensation(device_id, qr_token, expected_version, race,
                               terminal_exc=QRAlreadyBoundError)
       raise AssertionError("unreachable")             # mypy
   except Exception as err:                            # _PostNetBoxStateRace,
                                                       # IllegalQRTransition,
                                                       # commit failure
       await _run_compensation(device_id, qr_token, expected_version, err,
                               terminal_exc=QRBindRolledBackError)
       raise AssertionError("unreachable")
   ```

   **Step D — `_run_compensation` three-branch logic.**
   ```
   try:
       outcome = await _compensate_clear_qr(device_id, qr_token)
       # outcome ∈ {"cleared", "noop_different_qr"}
   except Exception as comp_err:
       # ===== Branch 3: COMPENSATION FAILED =====
       logger.critical("qr_bind_inconsistency_unrecoverable",
                       qr_token=..., device_id=..., request_id=...,
                       original_error=..., compensation_error=...)
       await _best_effort_inconsistency_journal(device_id, qr_token,
                                                original_err, comp_err)
       await _best_effort_compensation_audit(
           qr_token, device_id, expected_version,
           failure_stage="compensation",
           original_error=original_err,
           compensation_error=comp_err,
           compensation_outcome="failed",
       )
       raise QRBindInconsistencyError(qr_token, device_id) from comp_err

   # ===== Branch 2: COMPENSATION OK (cleared or no-op) =====
   logger.error("qr_bind_db_failed_netbox_compensated",
                qr_token=..., device_id=..., request_id=...,
                original_error=..., compensation_outcome=outcome)
   await _best_effort_compensation_audit(
       qr_token, device_id, expected_version,
       failure_stage="db_commit",
       original_error=original_err,
       compensation_outcome=outcome,
   )
   raise terminal_exc(qr_token, device_id) from original_err
   ```

3. **`_compensate_clear_qr(device_id, qr_token) -> Literal["cleared",
   "noop_different_qr"]`** — conditional, idempotent.
   - GET the device: `netbox_client.get(f"/api/dcim/devices/{device_id}/")`.
   - Inspect `device["custom_fields"].get("qr_id")`:
     - If it equals `qr_token` → PATCH `{"custom_fields": {"qr_id": None}}`,
       return `"cleared"`.
     - Otherwise (None, or a different token from a concurrent winner) →
       log `qr_bind_compensation_noop` at info level with the observed
       value; return `"noop_different_qr"`. **Do not clear** — we would
       clobber the concurrent winner's binding.
   - Direct client calls — **not via `patch_with_attribution`** (a
     compensation must not write a second journal entry that looks like a
     regular bind; the audit visibility for compensation comes from the
     structured logs and the best-effort `audit_log` row in Step 4).
   - GET or PATCH non-2xx raises — caller routes to Branch 3.
   - Task 2 adds the symmetric `_compensate_restore_qr(device_id,
     qr_token)` for retire's BOUND path with the same conditional pattern.

4. **`_best_effort_compensation_audit(...)`** — forensic record without
   expanding the `AuditResult` enum (no migration needed).
   - `operation="qr.bind"`, `entity_type="qr"`, `entity_id=qr_token`,
     `result=AuditResult.FAILURE` (existing enum value).
   - `before_json = {"qr_token": qr_token, "attempted_device_id":
     device_id, "expected_version": expected_version}`.
   - `after_json = {"failure_stage": "db_commit" | "compensation",
     "original_error": repr(original_err), "compensation_error":
     repr(comp_err) (if Branch 3), "compensation_outcome":
     "cleared" | "noop_different_qr" | "failed"}`.
   - `failure_stage` in `after_json` is what queries use later to count
     compensation events vs. regular failures — no enum expansion.
   - **Best-effort:** wrap in try/except, log
     `compensation_audit_write_failed` on failure, don't raise. Branch
     outcome must not change because the forensic row failed (decision B
     applies — primary op done, attribution best-effort).

5. **`_best_effort_inconsistency_journal(...)`** — Branch 3 only.
   - `POST /api/extras/journal-entries/` with `assigned_object_type =
     "dcim.device"`, `assigned_object_id = device_id`, `kind="danger"`,
     and a comment naming the inconsistency (token, device id, original
     error, compensation error, "manual cleanup required").
   - Failure swallowed + warning logged
     (`qr_bind_inconsistency_journal_failed`). System is already
     inconsistent; we only record additional log entries and let the
     response proceed to its 500.

6. `QRCodeRepository` — add `get_by_token_for_update(token) -> QRCode | None`
   (`select(...).where(token=token).with_for_update()`). Returns `None`
   for not-found (consistent with existing `get_by_token`).

7. `app/services/qr/lookup.py` — define the shared response model
   `QRLookupResponse {qr: QRInfo, device: DeviceData | None, device_error:
   str | None}` here so Tasks 1, 2 (retire returns `QRRetireResponse` —
   separate), and 3 all share one source. Task 3 owns the GET-path device-
   fetch population; Task 1's bind endpoint populates `device` directly
   from `patch_with_attribution`'s return value via `to_device_data`.

8. `app/api/v1/qr.py` — `POST /{qr_token}/bind`:
   - Pydantic `QRBindRequest {device_id: int, version: str}`,
     `extra="forbid"`.
   - Role: `dcinv-mobile-user`.
   - `response_model=QRLookupResponse`,
     `response_model_exclude_none=True`.
   - Build a per-request `QRLifecycleService` from the session +
     singletons (mirrors Sprint 3's `update_device` pattern).
   - Error → HTTP mapping:
     - `QRNotFoundError` → 404 `{error: {code: "QR_NOT_FOUND", ...}}`.
     - `QRStateConflictError` → 409 `{error: {code: "QR_STATE_CONFLICT",
       current_status, ...}}`.
     - `WriteConflictError` → 409 `{error: {code: "DEVICE_CONFLICT",
       current_state: DeviceData, current_version, ...}}` (same shape as
       Sprint 3).
     - `QRAlreadyBoundError` → 409 `{error: {code: "QR_ALREADY_BOUND",
       ...}}` (concurrency-race fallback).
     - `QRBindRolledBackError` → 500 `{error: {code:
       "QR_BIND_ROLLED_BACK", message: "Bind failed (rolled back)"}}`.
     - `QRBindInconsistencyError` → 500 `{error: {code:
       "QR_BIND_INCONSISTENCY", message: "Bind failed, manual cleanup
       required"}}`.
     - `NetBoxNotFound` / `NetBoxClientError` → flow through the existing
       global handlers in `main.py` (404 / 502).

9. Tests:
   - `tests/unit/services/qr/test_lifecycle.py` (bind portion):
     - `test_bind_returns_bound_qr_and_device_on_happy_path` — Branch 1.
     - `test_bind_raises_qr_not_found_when_token_missing`.
     - `test_bind_raises_qr_state_conflict_when_already_bound`.
     - `test_bind_raises_qr_state_conflict_when_retired`.
     - `test_bind_propagates_write_conflict_error` — assert no compensation
       GET/PATCH issued.
     - `test_bind_propagates_netbox_not_found_for_unknown_device`.
     - `test_bind_propagates_netbox_client_error_on_netbox_failure`.
     - `test_bind_raises_runtime_error_when_called_in_active_transaction` —
       the defensive guard.
     - **`test_bind_db_commit_fails_compensation_succeeds_raises_rolled_back`**
       — Branch 2; monkey-patch `session.commit` to raise; assert
       compensation GET hits NetBox; assert compensation PATCH carries
       `{"custom_fields": {"qr_id": None}}` because device shows our
       token; assert log `qr_bind_db_failed_netbox_compensated` at error
       level; assert the compensation `audit_log` row was attempted
       (`entity_type="qr"`, `operation="qr.bind"`,
       `result=AuditResult.FAILURE`, `after_json.failure_stage =
       "db_commit"`, `after_json.compensation_outcome = "cleared"`);
       assert `QRBindRolledBackError` is raised.
     - **`test_bind_db_commit_fails_compensation_fails_raises_inconsistency`**
       — Branch 3; commit raises AND compensation GET (or PATCH) raises;
       assert critical log `qr_bind_inconsistency_unrecoverable`; assert
       journal POST attempted with the inconsistency text
       (`kind="danger"`); assert compensation audit row attempted with
       `after_json.failure_stage = "compensation"`,
       `compensation_outcome = "failed"`, `compensation_error` present;
       if the journal POST itself fails, swallowed + warning logged;
       `QRBindInconsistencyError` raised.
     - **`test_bind_compensation_noop_when_device_already_has_different_qr`**
       — Correction 3; force a state race; compensation GET shows a
       different qr_id on the device; assert **no PATCH** issued; log
       `qr_bind_compensation_noop` at info level; compensation audit row
       carries `compensation_outcome="noop_different_qr"`; still raises
       `QRBindRolledBackError` (the bind itself didn't land for us).
     - `test_bind_state_race_under_lock_triggers_compensation` — FOR
       UPDATE re-check sees `status != FREE` (concurrent bind beat us) →
       Branch 2.
     - `test_bind_compensation_audit_write_failure_is_swallowed` — the
       audit insert raises; the bind response still surfaces the right
       terminal exception (Branch 2 still raises
       `QRBindRolledBackError`); log `compensation_audit_write_failed`
       emitted.
   - `tests/integration/test_qr_bind.py` (real Postgres + respx):
     - `test_bind_persists_bound_state_and_audit_row` — FREE→BOUND with
       `device_id`; audit row `qr.bind`/`success` lands; journal POST
       attempted.
     - `test_bind_write_conflict_leaves_qr_free_and_records_conflict_audit`
       — stale version; QR row unchanged; audit row `qr.bind`/`conflict`.
     - `test_bind_partial_unique_index_race_returns_409` — two concurrent
       binds to the same device; the loser raises `QRAlreadyBoundError`
       from a real `IntegrityError`; compensation `audit_log` row lands
       with `failure_stage="db_commit"`.
   - `tests/unit/api/v1/test_qr_bind.py`:
     - Handler logic by direct `await`: happy / 404 (QR) / 409 (state) /
       409 (device version) / 409 (already bound) / 500 (rolled back) /
       500 (inconsistency).
     - `AsyncClient`: routing (POST registered), role-gating (403
       without `dcinv-mobile-user`), `response_model_exclude_none`
       shaping, 422 on missing / extra body fields.

**Acceptance criteria:**

- `pytest` passes; 100% line + branch coverage on
  `app/services/qr/lifecycle.py`, the new lines in
  `app/services/qr/lookup.py`, `app/db/repositories/qr_code.py`, and
  `app/api/v1/qr.py`.
- ruff + black + mypy clean.
- A successful bind: `qr_codes` row is `BOUND` with `device_id` set, NetBox
  `custom_fields.qr_id == qr_token`, journal entry exists, audit row
  `qr.bind`/`success` exists with the shared `request_id`.
- The three compensation branches all produce the documented behaviour:
  correct log keys, correct response shape, NetBox state consistent in
  Branches 1+2, journal-entry attempt + critical log in Branch 3.
- **Compensation is conditional and idempotent** — if the device's
  `custom_fields.qr_id` doesn't equal our token at compensation time, we do
  not clear it (no clobber of a concurrent winner). The no-op path is
  logged and the audit row reflects `compensation_outcome="noop_different_qr"`.
- **Compensation events land in `audit_log`** as `result=FAILURE` rows with
  `after_json.failure_stage` distinguishing `db_commit` (Branch 2) from
  `compensation` (Branch 3). Best-effort: an audit write failure must not
  change the HTTP response.
- `qr_one_per_device` race returns 409 (`QR_ALREADY_BOUND`), not 500.
- 403 without `dcinv-mobile-user`.

**Anti-criteria:**

- No PUT — PATCH only (CLAUDE.md #3).
- Don't widen `patch_with_attribution` to absorb the QR-row update or to
  take a "use my outer transaction" mode — it stays generic over any NetBox
  PATCH (decision B: don't fragment per call-site).
- Don't route the compensation PATCH through `patch_with_attribution` —
  Architecture §3.1 attribution is for the regular flow, not for
  bookkeeping the original write's effect.
- Don't override `patch_with_attribution`'s best-effort journal/audit
  behaviour. The QR row in `qr_codes` is the source of truth for the
  binding; the regular-flow audit row duplicates it; an audit-write failure
  must not break the bind.
- Don't catch and silence the DB-commit failure — it must propagate (as
  `QRBindRolledBackError` or `QRBindInconsistencyError`) after compensation
  runs.
- Don't expand the `AuditResult` enum for compensation events — distinguish
  via `after_json.failure_stage`. No migration needed.
- Don't clear `custom_fields.qr_id` unconditionally in compensation — the
  conditional GET-then-PATCH protects concurrent winners (Correction 3).
- Don't use `assert session.in_transaction() is False` — use `raise
  RuntimeError(...)`. Asserts are stripped by `python -O` (Sprint 1 M3).
- Don't widen retire's API here — Task 2's scope.

**Suggested prompt:**

```
Implement Sprint 4 Task 1: QR bind. New QRLifecycleService in
app/services/qr/lifecycle.py drives free→bound via SEQUENTIAL
transactions: patch_with_attribution owns its audit-row tx, then a
separate session.begin() block does SELECT FOR UPDATE + qr_codes
UPDATE. Pre-check raises RuntimeError if session.in_transaction()
(not assert — survives python -O). On any post-NetBox failure
(commit fail, IntegrityError, FOR-UPDATE state race) run conditional
compensation: GET the device, only PATCH custom_fields.qr_id=null
if it currently equals our token; otherwise no-op log. Three
branches: happy / rolled-back (Branch 2: compensation ok, audit_log
row with failure_stage="db_commit") / inconsistency (Branch 3:
compensation failed, critical log + best-effort NetBox journal entry
+ audit_log row with failure_stage="compensation"). All compensation
audit writes are best-effort. POST /api/v1/qr/{qr_token}/bind (role
dcinv-mobile-user, body {device_id, version}) returns
QRLookupResponse (new shared model, introduced here, reused by
Tasks 2/3). Implementation order:
1. QRCodeRepository.get_by_token_for_update + its tests,
2. QRLifecycleService skeleton + exceptions + service tests,
3. bind() happy path, then each compensation branch one by one,
4. endpoint last.
TDD throughout, 100% coverage. No override of patch_with_attribution.
```

### Task 2 — QR retire

**Goal:** `POST /api/v1/qr/{qr_token}/retire` retires a QR. FREE→RETIRED is
DB-only (audit row, no NetBox call). BOUND→RETIRED clears
`custom_fields.qr_id` on the bound device through the same write apparatus
and the same three-branch compensation pattern as bind (in reverse —
compensation restores the token). Body: `{version: str | None}` — required
only for BOUND→RETIRED. Role: `dcinv-admin` (decision I).

**Steps:**

1. Extend `QRLifecycleService` with `retire(qr_token, expected_version: str |
   None, user) -> QR`:
   - Lookup QR by token; not found → `QRNotFoundError`.
   - `async with session.begin():`
     - `SELECT ... FOR UPDATE` on `qr_codes`.
     - If `qr.status == RETIRED` → `QRStateConflictError(current_status)`.
     - **FREE branch:**
       - `expected_version` if provided is silently ignored (no warning —
         the body is harmless overhead).
       - Domain `QR.retire()`.
       - `UPDATE qr_codes`: `status=RETIRED`, `retired_at=now()`.
       - Write `qr.retire`/`success` audit row inside the same transaction
         (no NetBox call → no decision-B asymmetry; the audit is part of
         the atomic DB operation here).
     - **BOUND branch:**
       - If `expected_version is None` → `MissingVersionError` (raised
         **before** the FOR UPDATE check on subsequent calls — but inside
         the transaction so the row lock releases cleanly).
       - Call `NetBoxWriteService.patch_with_attribution`:
         - `netbox_path=/api/dcim/devices/{qr.device_id}/`,
         - `entity_type='qr'`, `operation='qr.retire'`, `entity_id=qr_token`,
         - `expected_version=expected_version`,
         - `changes={"custom_fields": {"qr_id": None}}`.
       - Domain `QR.retire()`.
       - `UPDATE qr_codes`: `status=RETIRED`, `device_id=None`,
         `retired_at=now()`.
   - **Commit-with-compensation block** — same pattern as Task 1:
     - On commit failure for the BOUND branch, call
       `_compensate_restore_qr(qr.device_id, qr_token)` (restores
       `custom_fields.qr_id` to the token).
     - Branch 2 (compensation ok) → log error
       `qr_retire_db_failed_netbox_compensated` → raise
       `QRRetireRolledBackError`.
     - Branch 3 (compensation fails) → log critical
       `qr_retire_inconsistency_unrecoverable` → best-effort journal entry
       → raise `QRRetireInconsistencyError`.
     - FREE branch has no NetBox write, so a commit failure on FREE→RETIRED
       just propagates as `QRRetireRolledBackError` with the
       no-NetBox-compensation-needed log (`qr_retire_db_failed_no_netbox`)
       or simply propagates the underlying DB error (TBD in implementation
       plan — confirm during Task 2's plan gate).
2. `_compensate_restore_qr(device_id, qr_token)` — symmetric to Task 1's
   `_compensate_clear_qr`. Direct `netbox_client.patch(...,
   json={"custom_fields": {"qr_id": qr_token}})`. Raises on non-2xx.
3. New `QRLifecycleService` exception: `MissingVersionError`.
4. **Decommission-reuse hook (cross-cutting F):** Sprint 5's decommission flow
   calls `QRLifecycleService.retire(qr_token, expected_version, user)`
   directly — same signature mobile uses. No endpoint-only logic in the
   method.
5. `app/api/v1/qr.py` — `POST /{qr_token}/retire`:
   - Pydantic `QRRetireRequest {version: str | None = None}`,
     `extra="forbid"`.
   - Role: `dcinv-admin` (decision I).
   - `QRRetireResponse {qr: QRInfo}` — no `device` field (the QR is now
     retired; no binding to show).
   - Error → HTTP mapping mirrors Task 1's, plus:
     - `MissingVersionError` → 422 `{error: {code: "VERSION_REQUIRED", ...}}`
       (a BOUND QR needs the device version).
     - `QRRetireRolledBackError` → 500 `{error: {code:
       "QR_RETIRE_ROLLED_BACK", ...}}`.
     - `QRRetireInconsistencyError` → 500 `{error: {code:
       "QR_RETIRE_INCONSISTENCY", ...}}`.
6. Tests:
   - `tests/unit/services/qr/test_lifecycle.py` (retire portion) — FREE
     happy path (assert respx received **zero** NetBox hits); FREE with
     `version` provided still passes (silent ignore); BOUND happy path
     (device's qr_id cleared, audit row); BOUND missing version → 422;
     BOUND NetBox version mismatch → 409 + QR stays BOUND; BOUND NetBox 404;
     BOUND NetBox 502; RETIRED → 409 (`QRStateConflictError`); QR not found;
     **BOUND-path compensation tests for both branches 2 and 3** (mirror
     Task 1's three-branch coverage, here the compensation PATCH restores
     the token).
   - `tests/integration/test_qr_retire.py` — real Postgres: FREE→RETIRED
     audit row + zero NetBox hits; BOUND→RETIRED full path; role gating
     (403 without `dcinv-admin`, including for `dcinv-mobile-user`).
   - `tests/unit/api/v1/test_qr_retire.py` — endpoint scenarios mirroring
     Task 1.

**Acceptance criteria:**

- `pytest` passes; 100% line + branch coverage on the retire portions of
  `app/services/qr/lifecycle.py` and the new endpoint lines in
  `app/api/v1/qr.py`.
- ruff + black + mypy clean.
- FREE→RETIRED triggers **zero** NetBox calls (respx assertion).
- BOUND→RETIRED full path: device's `custom_fields.qr_id` cleared, audit row
  `qr.retire`/`success`, QR row RETIRED with `device_id=NULL`.
- BOUND→RETIRED compensation branches behave identically to bind's three
  branches.
- Role enforcement: `dcinv-admin` only — `dcinv-mobile-user` gets 403.

**Anti-criteria:**

- Don't make FREE→RETIRED touch NetBox.
- Don't reject FREE→RETIRED if `version` was provided — silent ignore.
- Don't duplicate Task 1's compensation logic — symmetric helpers
  (`_compensate_clear_qr` / `_compensate_restore_qr`) share the structure.
- Don't widen `patch_with_attribution` for retire's needs.
- Don't open the retire endpoint to `dcinv-mobile-user` (decision I).

**Suggested prompt:**

```
Implement Sprint 4 Task 2: QR retire. Extend QRLifecycleService with
retire(qr_token, expected_version, user). FREE→RETIRED is DB-only
(audit row, zero NetBox calls); BOUND→RETIRED clears custom_fields.qr_id
via NetBoxWriteService.patch_with_attribution and uses the same three-
branch compensation as bind (here the compensation restores the qr_id).
POST /api/v1/qr/{qr_token}/retire (role dcinv-admin, body {version:
str|null}) returns QRRetireResponse. TDD: BOUND compensation branches
+ FREE/BOUND/RETIRED state coverage + role gating, 100% coverage.
```

### Task 3 — Combined QR+device response

**Goal:** Extend `GET /api/v1/qr/{qr_token}` so a BOUND QR returns the linked
device's full ToR §4.3.3 field set in one call. The standalone `GET
/api/v1/devices/{id}` response shape expands as an accepted side effect (work-
log will note this; mobile must know).

**Steps:**

1. Extend `DeviceData` in `app/services/device.py` additively (decision E):
   - `device_type: ObjectRef`
   - `manufacturer: ObjectRef`
   - `device_role: ObjectRef`
   - `u_height: int | None` (rack-units height)
   - `primary_ip4: str | None`
   - `primary_ip6: str | None`
   - `last_updated: str` (exposed for ToR §4.3.3 — even though it doubles
     the `version` field, the field set is what ToR specifies)
   - `qr_id: str | None` — populated from app DB only (decision H), not
     NetBox. The standalone `GET /api/v1/devices/{id}` leaves it `None`
     (no QR lookup in that path).
   - `custom_fields: dict[str, Any]` — populated keys only (drop keys whose
     NetBox value is `None`, to keep the response compact).
2. Extend `to_device_data(device: dict, *, qr_id: str | None = None) ->
   DeviceData`. The kwarg lets the combined response inject the app DB value;
   the standalone read keeps `qr_id=None`.
3. `app/services/qr/lookup.py` — `QRLookupService.lookup(qr_token, *) ->
   QRLookupResponse` (model introduced in Task 1, populated here):
   - DI: `QRCodeRepository`, `DeviceService` (new injection).
   - FREE or RETIRED → `device=None`, `device_error=None`.
   - BOUND → `await device_service.get_device(qr.device_id)`. On
     `NetBoxNotFound` or `NetBoxClientError` → `device=None`,
     `device_error="device_unavailable"` (categorical string, never
     free-form). Per decision D — QR lookup must not fail when NetBox is
     down.
   - Set `qr_id=qr_token` on the device via the new `to_device_data` kwarg
     in `DeviceService.get_device`-equivalent transform; the cleanest path
     is for `QRLookupService` to call `device_service.get_device(...)` and
     post-process — or to pass `qr_id` through a new `DeviceService` method
     `get_device_with_qr_id(...)`. Exact factoring confirmed during Task 3's
     plan gate.
4. `app/api/v1/qr.py` — `GET /{qr_token}` updated:
   - `response_model=QRLookupResponse`, `response_model_exclude_none=True`.
   - No new exception handlers needed: NetBox failures are swallowed and
     surfaced as `device_error` per decision D.
5. Tests:
   - `tests/unit/services/qr/test_lookup.py` — extend: FREE → device/device_
     error null; BOUND happy path → device populated with new fields incl.
     `qr_id` from app DB; BOUND with NetBox 404 → device null +
     `device_error="device_unavailable"`; BOUND with NetBox 502 → same;
     RETIRED → device/device_error null.
   - `tests/unit/services/test_device.py` — extend: parse all new
     `DeviceData` fields; `custom_fields` filters null values;
     `to_device_data` with explicit `qr_id` argument; `to_device_data`
     without `qr_id` leaves it `None`.
   - `tests/unit/api/v1/test_qr_lookup.py` — endpoint scenarios mirroring
     the service tests; `response_model_exclude_none` keeps the response
     compact.
   - **Update existing Sprint 3 tests** that pin the `DeviceData` shape
     (in `test_device.py` and `test_devices.py`) — the new fields appear
     in the standalone read too. Documented in the work-log.

**Acceptance criteria:**

- `pytest` passes; 100% line + branch coverage on
  `app/services/qr/lookup.py`, the changed lines in `app/services/device.py`,
  and the changed lines in `app/api/v1/qr.py`.
- ruff + black + mypy clean.
- BOUND QR returns device populated with all new fields incl. `qr_id` from
  app DB.
- NetBox failure on the bound device returns `device=null` +
  `device_error="device_unavailable"`, **not** a 5xx from the QR lookup.
- Sprint 3's `GET /api/v1/devices/{id}` still works; response now carries
  the new fields (with `qr_id=null`).

**Anti-criteria:**

- Don't fail QR lookup on NetBox failure (decision D).
- Don't fetch the device for non-BOUND QRs.
- Don't cache the device fetch in the combined response (decision G).
- Don't populate `qr_id` from NetBox (decision H).
- Don't return null `custom_fields` values — filter them.
- Don't introduce a `DeviceDataFull` second model (decision E — extend
  additively, one parser, one shape).

**Suggested prompt:**

```
Implement Sprint 4 Task 3: combined QR+device response. Extend DeviceData
in app/services/device.py with the full ToR §4.3.3 field set
(device_type, manufacturer, device_role, u_height, primary_ip4/6,
last_updated, qr_id, custom_fields). Extend QRLookupService.lookup to
fetch the bound device via DeviceService for BOUND QRs and soft-fail on
NetBox error (device=null + device_error="device_unavailable"). GET
/api/v1/qr/{qr_token} response_model=QRLookupResponse with
response_model_exclude_none. qr_id sourced from app DB, not NetBox.
TDD, 100% coverage. Update the Sprint 3 device-shape tests to match;
note the side effect in the work-log.
```

### Task 4 — Acceptance and close-out

**Goal:** Sprint 4 done means: tests green, quality gates clean, stack still
runs, the cross-cutting and per-task decisions are captured in the work-log,
and the next sprint has a clean handoff.

**Steps:**

1. **Full test run:** `uv run pytest --cov=app --cov-branch
   --cov-fail-under=100`. Expected: ~50+ new tests on top of Sprint 3's 336
   (final count recorded in the work-log).
2. **Quality gates:** `uv run ruff check`, `uv run black --check`,
   `uv run mypy app/`. All clean.
3. **Stack smoke:** `docker compose up -d --build`; `curl localhost:8000/
   health` returns ok; `docker compose down -v` clean.
4. **Work-log entry** in `docs/work-log.md` ("Sprint 4 — QR Lifecycle
   Completion"), mirroring Sprint 3's structure:
   - What shipped (per-task table).
   - Quality bar at close (test count, coverage, lint/types).
   - Pyproject deviations (expected: none — no new deps; flag if any
     emerge).
   - Architectural decisions worth carrying forward — especially: the
     three-branch compensation pattern; the shared `QRLifecycleService`
     shape; `DeviceData` expansion side effect on Sprint 3's endpoint.
   - Sprint 4 retrospective.
   - Discrepancies between ToR / Architecture and what shipped (incl. any
     NetBox custom-field name caveats updated in `docs/parking-lot.md`).
   - Deliberately deferred (Sprint 5 candidates: decommission, device
     create, add-comment).
   - Files added / modified (high-level).
5. **CLAUDE.md** — update the Repository Status paragraph to reflect Sprint
   4 close: business surface now includes QR bind/retire and the combined
   QR+device read; Sprint 5 next.
6. **Memory** — add a `project_sprint_4_status.md` entry mirroring the
   Sprint-2/3 status memories.
7. **Parking lot** — if the NetBox `qr_id` custom-field name verification
   produced any deployment notes, update `docs/parking-lot.md`.

**Acceptance criteria:**

- All four prior tasks' acceptance criteria still hold at end of sprint.
- The full test suite is green at 100% line + branch coverage with no
  `# pragma: no cover` additions in this sprint.
- `docker compose up -d --build` + `/health` works.
- `docs/work-log.md`, `CLAUDE.md`, memory, and (if applicable)
  `docs/parking-lot.md` reflect the close.

**Anti-criteria:**

- Don't ship the close-out before the test/lint/type/stack gates pass.
- Don't drop coverage to "fix" a hard-to-test line — refactor or accept the
  test cost.
- Don't write Sprint 5 scope into the work-log entry — that lives in
  `docs/sprint-5.md` when it gets created.

---

## Working principles (carried from Sprints 1–3)

- **TDD discipline.** Tests first, including failure-mode counterparts. No
  happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get
  explicit "go", then code.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria
  are met. The gate is the user's, not the agent's.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–3.
- **No new dependencies** without explicit approval. Version bumps of existing
  deps are allowed when justified (record the reason in `docs/work-log.md`).
- **Endpoint handler tests:** test handler logic by direct `await` of the
  handler function; use `TestClient`/`AsyncClient` only for routing,
  role-gating, and `response_model` shaping. Same call as Sprint 2/3.
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 4
  exercises #2 (three-record write) and #3 (PATCH-not-PUT) on the bind +
  retire write paths, and #4 (QR state machine enforced by the DB) on every
  state transition.
- **Reuse Sprint 3's pieces.** `NetBoxClient` patch/post/options;
  `NetBoxWriteService` for the journal + audit + re-read + version-compare
  building blocks; `to_device_data` extended additively; the global NetBox
  exception handlers in `main.py`. Don't rewrite the apparatus.

## Reference documents

- ToR §4.2 (QR codes), §4.2.3 (state machine), §4.3.1 (scan flow),
  §4.3.3 (device screen field set — the combined-response target)
- `Architecture_Overview.md` §3 (NetBox interaction), §4 (QR lifecycle —
  the atomicity requirement for free→bound), §3.1 (three-record write)
- `docs/sprint-2.md` — QR domain + DB state machine + lookup endpoint
- `docs/sprint-3.md` — the write apparatus + cross-cutting decisions A/B/C
  this sprint inherits
- `docs/work-log.md` — Sprint 3 retrospective; the deferred items this sprint
  picks up
- `docs/parking-lot.md` — Phase 2 alerting on three-record partial failures;
  NetBox status-config dependency (relevant to Sprint 5's decommission, not
  this sprint); **NetBox custom field name verification** for `asset_tag` +
  `qr_id` — deployment dependency for Sprint 4 (decision B)
- CLAUDE.md cross-cutting rules #2, #3, #4 (the QR state machine is
  particularly active this sprint)
