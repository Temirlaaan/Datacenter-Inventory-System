# Local Code Review — Sprint 4 (QR Lifecycle Completion)

**Reviewed**: 2026-05-25
**Author**: self
**Scope**: Sprint 4 changes (Tasks 1-3 + close-out). Sprint 2-3 already had their own close-out reviews; this review focuses on what landed since Sprint 3 closed.
**Decision**: **REQUEST CHANGES** — one HIGH (real bug, easy fix); the rest is MEDIUM/LOW polish.

## Summary

Sprint 4's three-branch compensation apparatus is solid. 460 tests pass at 100% line + branch coverage, all quality gates clean. One real bug in the bind endpoint (combined response forgets to pass `qr_id` into `to_device_data`, leaving `device.qr_id=None` post-bind — inconsistent with the GET path that DOES populate it). A handful of MEDIUM items around log-event-name greppability, sensitive data in inconsistency journal entries, and the misleading `QRStatus.RETIRED` fallback in `_retire_free`'s unreachable branch.

## Findings

### CRITICAL

None.

### HIGH

**H1 — Bind response leaves `device.qr_id` as `None` instead of the freshly-bound token.**
- File: [backend/app/api/v1/qr.py:241](backend/app/api/v1/qr.py#L241)
- After a successful bind, the combined response should carry `device.qr_id == bound_qr.id` (same value the GET path would return for the now-bound QR). Currently `to_device_data(device_dict)` is called WITHOUT the `qr_id` kwarg, so `device.qr_id` defaults to `None` and gets dropped by `response_model_exclude_none=True`.
- Impact: mobile clients reading the bind response see no `device.qr_id`; if they refetch via `GET /api/v1/qr/{id}` they get the token. Inconsistent contract.
- Test gap: `test_post_bind_endpoint_returns_200_on_happy_path` asserts `body["device"]["id"]` but never asserts `body["device"]["qr_id"]` — that's why coverage didn't catch this.
- Fix:
  ```python
  return QRLookupResponse(
      qr=to_qr_info(bound_qr, batch),
      device=to_device_data(device_dict, qr_id=bound_qr.id),  # was: missing qr_id
      device_error=None,
  )
  ```
  Add assertion to the endpoint happy-path test: `assert body["device"]["qr_id"] == _QR_ID`.

### MEDIUM

**M1 — Inconsistency journal entry leaks `repr(original_err)` and `repr(comp_err)` to NetBox.**
- File: [backend/app/services/qr/lifecycle.py:618-624](backend/app/services/qr/lifecycle.py#L618-L624)
- `repr(exception)` can include stack-trace-adjacent context (DB connection strings, SQL fragments, internal file paths). The NetBox journal is internal-only but readable by every NetBox user; for a single-DC operator-only tool this is probably acceptable, but worth noting for any future multi-tenant deployment.
- Same exposure exists in `audit_log.after_json.original_error` / `compensation_error` (lines 660, 664). Consistent across both surfaces.
- Suggested fix (if tightening): redact known sensitive prefixes, or use `type(exception).__name__ + ": " + str(exception)` instead of `repr` (loses traceback hint but avoids the internal-state-dump risk). Defer if the operator-only assumption holds.

**M2 — `_retire_free` raises `QRStateConflictError(QRStatus.RETIRED)` when `locked is None`.**
- File: [backend/app/services/qr/lifecycle.py:438-439](backend/app/services/qr/lifecycle.py#L438-L439)
- The TODO comment documents this as unreachable per the Sprint 2 invariant ("QR IDs are never reused / never deleted"). But the fallback `current_status=QRStatus.RETIRED` would mislead any caller if it ever fires (they'd think the QR is retired, when in fact it disappeared).
- Suggested fix: either (a) raise `QRNotFoundError(qr_id)` for `locked is None` (more honest), or (b) add an explicit `QRDisappearedUnderLockError` so the misleading status string is impossible. Option (a) is one line. Will only matter if the invariant breaks, but cheap insurance.

**M3 — Log event names are constructed dynamically via f-strings.**
- File: [backend/app/services/qr/lifecycle.py:368, 397, 629](backend/app/services/qr/lifecycle.py#L368)
- `f"{op_prefix}_inconsistency_unrecoverable"` etc. produces `qr_bind_inconsistency_unrecoverable` and `qr_retire_inconsistency_unrecoverable`. Greppability suffers — searching for the literal event name in source returns nothing.
- Mitigated by the docstring documenting the pattern and tests pinning exact strings, but ops looking at production logs and grepping the codebase for "where is this event raised?" will be confused.
- Suggested fix: define module-level constants per (operation, branch) pair, or pass the event name as an explicit parameter. Or accept the trade-off and add a code comment near each f-string listing the actual event names produced.

**M4 — `_run_compensation` is 75 lines (exceeds 50-line guideline).**
- File: [backend/app/services/qr/lifecycle.py:341-415](backend/app/services/qr/lifecycle.py#L341-L415)
- The CLAUDE.md best-practice threshold is 50 lines. This function is a single coherent three-branch flow; splitting it (e.g. `_branch_2`, `_branch_3` helpers) would harm clarity by spreading the control flow across multiple methods.
- Suggested fix: keep as-is and acknowledge the guideline violation in the docstring, OR factor the audit-row construction into a helper to trim a dozen lines. My lean: keep as-is — the structure mirrors the documented design.

### LOW

**L1 — Defensive `assert` for type narrowing where `cast()` would be more honest.**
- Files: [backend/app/services/qr/lifecycle.py:299](backend/app/services/qr/lifecycle.py#L299), [backend/app/services/qr/lifecycle.py:459](backend/app/services/qr/lifecycle.py#L459), [backend/app/api/v1/qr.py:238](backend/app/api/v1/qr.py#L238), [backend/app/api/v1/qr.py:329](backend/app/api/v1/qr.py#L329)
- The `assert X is not None` pattern was discussed and accepted in the Task 1 plan: it's for mypy narrowing, not invariant enforcement. The code is correct even when asserts are stripped under `python -O` (the variable is assigned in the only path that reaches the assert). Project rule about `assert` only applies to runtime invariants (which use `RuntimeError`).
- No change needed; flagged so a future reviewer doesn't mistake these for runtime checks.

**L2 — `QRCodeRepository.update` doesn't check rowcount.**
- File: [backend/app/db/repositories/qr_code.py:86-106](backend/app/db/repositories/qr_code.py#L86-L106)
- If the row doesn't exist (concurrent delete), the UPDATE silently affects 0 rows. In practice the FOR UPDATE lock taken by the orchestration prevents this — so the gap is theoretical. Defense-in-depth would be `result.rowcount == 1`, but it's paranoia given the lock contract.

**L3 — `to_device_data` mixes defensive `.get()` (Task 3 fields) with strict `device[key]` (Sprint 3 fields).**
- File: [backend/app/services/device.py:96, 144](backend/app/services/device.py#L96)
- `device["rack"]` and `device["custom_fields"]` will KeyError on a malformed NetBox payload. Sprint 3 behavior preserved; tests always include these. Inconsistency is cosmetic — bringing Sprint 3 fields to defensive extraction is a minor refactor that could come with a real shape-mismatch incident.

**L4 — Dynamic log event names aren't `Final[str]` constants.**
- File: [backend/app/services/qr/lifecycle.py](backend/app/services/qr/lifecycle.py) (various)
- See M3. Stricter typing (`Final[str]`) would help if migrated to constants.

**L5 — `.claude/settings.json` accumulated 18 fine-grained Bash allowlist entries during Sprint 4.**
- File: [.claude/settings.json](.claude/settings.json)
- Each entry is a specific pytest invocation with hardcoded test env vars (junk values, no real secrets). No security risk. Could be consolidated with a `Bash(uv run pytest *)` wildcard, but per-command listing is more explicit. Acceptable accumulation; might want to compact between sprints.

**L6 — Bind/retire endpoint try/except chains are 70+ lines each.**
- File: [backend/app/api/v1/qr.py:169-243](backend/app/api/v1/qr.py#L169-L243), [backend/app/api/v1/qr.py:263-326](backend/app/api/v1/qr.py#L263-L326)
- Each branch maps one exception type to one HTTP response. Coherent and matches Sprint 3's `update_device` pattern. Could be factored via an exception-to-response table, but the explicit structure makes the wire-format-to-exception correspondence trivial to audit. Keep as-is.

## Validation Results

| Check | Result |
|---|---|
| Type check (`mypy app/`) | **Pass** — no issues in 48 source files |
| Lint (`ruff check app/ tests/`) | **Pass** |
| Format (`black --check app/ tests/`) | **Pass** — 104 files, no reformatting needed |
| Tests (`pytest tests/`) | **Pass** — **460 passed**, 9 warnings (jose's deprecated `datetime.utcnow()` — third-party) |
| Coverage | **100%** line + branch on `app/` (1461 stmts, 158 branches) |

## Files Reviewed (Sprint 4 surface)

| File | Change | Lines |
|---|---|---|
| `backend/app/services/qr/lifecycle.py` | Added (Task 1 + 2) | 694 |
| `backend/app/services/qr/lookup.py` | Modified (Task 3) | 138 |
| `backend/app/services/qr/__init__.py` | Modified (drop QRLookupResult) | 14 |
| `backend/app/services/device.py` | Modified (Task 3) | 234 |
| `backend/app/services/netbox_write.py` | Modified (entity_id param) | 229 |
| `backend/app/db/repositories/qr_code.py` | Modified (Task 1) | 106 |
| `backend/app/api/v1/qr.py` | Modified (bind, retire, lookup endpoints) | 330 |
| `backend/tests/unit/services/qr/test_lifecycle.py` | Added | 1100+ |
| `backend/tests/unit/services/qr/test_lookup.py` | Added | 280 |
| `backend/tests/unit/services/test_device.py` | Modified (+21 tests) | 415 |
| `backend/tests/unit/api/v1/test_qr_bind.py` | Added | 320 |
| `backend/tests/unit/api/v1/test_qr_retire.py` | Added | 360 |
| `backend/tests/unit/api/v1/test_qr_lookup.py` | Rewritten | 290 |
| `backend/tests/unit/api/v1/conftest.py` | Modified (get_settings cache clear) | 130 |
| `backend/tests/integration/test_qr_bind.py` | Added | 340 |
| `backend/tests/integration/test_qr_retire.py` | Added | 310 |
| `backend/tests/integration/test_repositories.py` | Modified (+6 tests) | 500 |
| `backend/tests/integration/test_lookup.py` | Rewritten | 180 |
| `docs/sprint-4.md` | Added | 660+ |
| `docs/work-log.md` | Modified (Sprint 4 retro) | +120 |
| `docs/parking-lot.md` | Modified (NetBox shape entry) | +33 |
| `CLAUDE.md` | Modified (status line) | +2/-1 |
| `.claude/settings.json` | Modified (test Bash allowlist) | +18 |

## Next Steps

1. **Fix H1** (one-line change in `app/api/v1/qr.py:241` + one test assertion). Recommended before commit.
2. **Decide on M1-M3**: M2 is a one-line fix (raise QRNotFoundError for `locked is None`), worth doing. M1 / M3 are operator-context-dependent — defer or accept based on production deploy plans.
3. After H1 + M2: commit, push, and (when ready) open Sprint 5 with device decommission + device create + add-comment.
