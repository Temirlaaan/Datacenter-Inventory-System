# Local Review: Sprint 5 Task 4 — Device Decommission (commit c317e86)

**Reviewed**: 2026-05-28
**Commit**: c317e86 — `feat(s5-t4): device decommission endpoint with QR-first ordering`
**Scope**: 12 files (4 production, 8 tests); 1784 insertions / 21 deletions
**Decision**: APPROVE with comments — 0 CRITICAL, 0 HIGH, 3 MEDIUM, 2 LOW

## Summary
Task 4 implements the device-decommission flow with QR-first ordering and three-branch re-bind compensation per the locked sprint plan. The service mirrors Sprint 4's `QRLifecycleService` pattern closely (defensive `in_transaction()` guard, conditional compensation, best-effort danger journal on Branch 3) and is fully covered (100% line + branch). The findings below are quality polish, not correctness blockers — most are pattern consistency with Sprint 4 idioms.

## Findings

### CRITICAL
None.

### HIGH
None.

### MEDIUM

**M1. `_compensate_rebind_or_inconsistency` should be annotated `-> NoReturn`**
- **File**: [backend/app/services/device_decommission.py:186-194](backend/app/services/device_decommission.py#L186-L194)
- **Issue**: The helper always raises (line 227 `raise DeviceDecommissionInconsistencyError(...)` or line 237 `raise DeviceDecommissionRolledBackError(...)`) but is annotated `-> None`. Sprint 4's `QRLifecycleService._run_compensation` is annotated `-> NoReturn` ([backend/app/services/qr/lifecycle.py:353](backend/app/services/qr/lifecycle.py#L353)) — the pattern was already established and this drift breaks it.
- **Consequences**: (a) `decommission()` line 184 `return DeviceResponse(...)` relies on the unstated invariant that the except-block compensation path always raises; mypy can't prove `updated` is bound on the return line, so a future refactor that adds a return-without-raise branch to the helper would silently fall through to `UnboundLocalError`. (b) Pattern divergence makes the codebase harder to scan.
- **Fix**: Change line 194 from `) -> None:` to `) -> NoReturn:` and import `NoReturn` from `typing`. Locks in the "always raises" contract; brings file in line with Sprint 4.

**M2. Bound logger created on line 117 but only used on line 142; subsequent log calls use the module-level logger with redundant kwargs**
- **File**: [backend/app/services/device_decommission.py:117-122](backend/app/services/device_decommission.py#L117-L122), [backend/app/services/device_decommission.py:213-220](backend/app/services/device_decommission.py#L213-L220), [backend/app/services/device_decommission.py:230-236](backend/app/services/device_decommission.py#L230-L236), [backend/app/services/device_decommission.py:266-272](backend/app/services/device_decommission.py#L266-L272)
- **Issue**: `log = logger.bind(device_id=..., expected_version=..., reason=..., request_id=...)` is created in `decommission()` but only consumed once (line 142 `log.critical(...)`). All four other log calls — `device_decommission_inconsistency_unrecoverable`, `device_decommission_db_failed_qr_recompensated`, `device_decommission_inconsistency_journal_failed` — use the module-level `logger` directly and pass `qr_id`, `device_id`, `request_id` explicitly each time. `expected_version` and `reason` are dropped from those calls entirely.
- **Consequences**: Operators grepping a Branch 2/3 log line don't see `expected_version` or `reason` even though they were bound at the top of the request. Easy to drift further over time.
- **Fix**: Either (a) thread `log` into `_compensate_rebind_or_inconsistency` and `_best_effort_inconsistency_journal` and use it consistently, or (b) drop the bound logger entirely and pass the kwargs explicitly at each call site. The current half-and-half is the worst of both.

**M3. Missing behavioral test for `WriteConflictError` from the status PATCH after a successful retire**
- **File**: [backend/tests/unit/services/test_device_decommission.py:412-432](backend/tests/unit/services/test_device_decommission.py#L412-L432)
- **Issue**: The Branch 2 test uses `NetBoxClientError("transient netbox 5xx on status patch")` as the device-PATCH failure trigger. There's no test pinning the behavior when `WriteConflictError` (a different exception) fires from the status PATCH — that scenario is structurally important because it documents the intentional service-layer decision: even a stale-version conflict on the status PATCH triggers re-bind compensation rather than propagating to the endpoint's 409 handler. (Coverage is 100% because the same `except Exception` branch catches both; this is purely a behavioral / regression-protection gap.)
- **Consequences**: A future change that splits the `except Exception` into more specific handlers (e.g. "let WriteConflictError through as 409") would silently leave the QR retired with no compensation. A pinned test would catch that.
- **Fix**: Add `test_decommission_write_conflict_on_status_patch_after_retire_compensates_via_rebind` mirroring the existing Branch 2 test but seeding `write_service.raises = WriteConflictError(current_object=..., current_version=...)`. Assert the same `DeviceDecommissionRolledBackError` outcome.

### LOW

**L1. `expected_version_for_patch` fallback uses `or`, not explicit `is not None` check**
- **File**: [backend/app/services/device_decommission.py:154](backend/app/services/device_decommission.py#L154)
- **Issue**: `expected_version_for_patch = post_retire_version or expected_version` — if `post_retire_version` were ever an empty string (impossible in practice since NetBox's `last_updated` is always populated), it would fall through to the caller-provided version.
- **Fix (optional)**: `expected_version_for_patch = post_retire_version if post_retire_version is not None else expected_version`. Stricter, matches the surrounding `is None` / `is not None` style.

**L2. Integration test mock setup ordering is fragile — relies on respx's `side_effect` list ordering matching the service's call sequence**
- **File**: [backend/tests/integration/test_device_decommission.py:228-243](backend/tests/integration/test_device_decommission.py#L228-L243)
- **Issue**: The bound-device happy-path test configures `get_route.side_effect = [Response(...), Response(...)]` and `patch_route.side_effect = [Response(...), Response(...)]`. If the service ever changes the order (e.g. re-reads twice in Step B), the test would fail with a confusing "no more responses" error rather than a clear mock mismatch.
- **Fix (optional)**: Either add a comment naming each response position (e.g. `# [0] Step B retire re-read; [1] Step C status PATCH re-read`) or use distinct routes per call by URL parameter (not practical here since both target `_DEVICE_PATH`). The current comment on line 229-231 helps; making it inline with each list entry would be even clearer.

## Validation Results

| Check | Result |
|---|---|
| Type check (mypy app/ tests/) | Pass (at commit c317e86 during close-out — `.venv` removed by subsequent flatten; not re-runnable until `uv sync`) |
| Lint (ruff) | Pass (at c317e86) |
| Format (black) | Pass (at c317e86) |
| Tests (pytest) | 589 pass, 100% line + branch coverage on `app/`, `--cov-fail-under=100` gate met |

## Files Reviewed

| File | Change Type |
|---|---|
| backend/app/services/device_decommission.py | Added |
| backend/app/api/v1/devices.py | Modified |
| backend/app/services/qr/lifecycle.py | Modified (Step 0 — `retire` return signature) |
| backend/app/api/v1/qr.py | Modified (destructure new tuple) |
| backend/app/db/repositories/qr_code.py | Modified (+`find_by_bound_device_id`) |
| backend/tests/unit/services/test_device_decommission.py | Added (11 tests) |
| backend/tests/unit/api/v1/test_device_decommission.py | Added (13 tests) |
| backend/tests/integration/test_device_decommission.py | Added (4 tests) |
| backend/tests/integration/test_repositories.py | Modified (+3 tests for new repo method; drop unused type-ignore) |
| backend/tests/unit/api/v1/test_qr_retire.py | Modified (stub signature) |
| backend/tests/unit/services/qr/test_lifecycle.py | Modified (return-value assertions) |
| backend/tests/unit/services/test_device.py | Modified (drive-by mypy fix) |

## Strengths Worth Noting

- **Pattern fidelity to Sprint 4**: `in_transaction()` guard with `RuntimeError` (not assert) per Sprint 1 M3; compensation routed through raw `NetBoxClient.post` (not `patch_with_attribution`) to avoid the misleading second journal; best-effort journal swallows on failure with a structured warning log. All consistent.
- **Deterministic OCC token capture**: `post_retire_version = updated_device["last_updated"]` (line 149) threaded into the re-bind (line 208) means a concurrent edit during decommission produces a `WriteConflictError` from re-bind → Branch 3 inconsistency, which is the right operational signal (logged critical, danger journal posted). This was the locked-in resolution of Task 4 Q3 TBD and the code matches.
- **Three-branch log keys at the right severity**: Branch 2 → `error`, Branch 3 → `critical`, Correction 4 abort → `critical`. Matches Sprint 4's severity convention.
- **`assert` only used for mypy type-narrowing**, never for runtime invariant enforcement (which would be stripped by `python -O`). Lines 148 and 175 narrow `dict | None` to `dict`; the runtime invariants are enforced by `bound_qr is not None` and the BOUND-path contract of `retire`.

## Recommended Follow-up

The three MEDIUM findings are small, focused, and could land as a single follow-up commit (call it `chore(s5-t4): code-review polish`). The LOW findings are optional and not worth a dedicated commit.

If you want me to apply the fixes inline, say the word — they're each <10 lines and locally contained.
