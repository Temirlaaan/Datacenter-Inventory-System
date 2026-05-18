# Parking lot

Cross-sprint items that aren't part of any current sprint plan: deployment
dependencies owned by people outside the codebase, and Phase 2 hardening that
the MVP deliberately skips. Sprint plans (`sprint-N.md`) and the work-log cover
what's *in* a sprint; this file holds what's parked.

---

## Pending NetBox configuration (deployment dependency for Sprint 4+)

Per ToR §4.3.7, NetBox currently has only `Active` / `Offline` device statuses.
Before the Decommission use case ships, the NetBox admin must add the standard
NetBox statuses:

- `Staged`, `Decommissioning`, `Inventory`, `Failed`

**Owner:** NetBox admin (user)
**Blocker for:** Sprint 4 (Decommission flow — needs `Decommissioning`)
**Not a blocker for:** Sprint 3 (the Update flow uses statuses discovered
dynamically from NetBox via the `/api/v1/meta/statuses` endpoint — it does not
hardcode the status set).

---

## Phase 2: alerting on three-record partial failures

Sprint 3 cross-cutting Decision B: the three-record write is **NetBox-write-first,
best-effort attribution**. If the NetBox device PATCH succeeds but the NetBox
journal POST or the app-DB `audit_log` row write fails, the backend logs loudly
but does **not** roll back the NetBox change (no distributed transaction exists).
This is acceptable for the MVP.

**Phase 2 must add:** alerting on these partial-failure events — e.g. a count of
`result='partial_failure'` `audit_log` rows per hour, surfaced to whoever owns
operational monitoring. Until then, partial failures are visible only in logs.
