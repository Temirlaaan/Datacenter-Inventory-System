# Sprint 8b — User-Facing Deliverables

> **Status:** Planned. Awaiting Task 0 go/no-go. (Per-task detail layered in inline as we get to each task — same rhythm as Sprints 7 + 8a.)
> **Duration target:** 6–7 working days
> **Goal:** Ship the HTML admin web pages per ToR §4.4.2 on top of Sprint 8a's hardened JSON foundation. Web auth via Keycloak OIDC redirect → encrypted session cookie (CLAUDE.md "Backend" section). Add PDF batch label generation (Architecture §6) and CSV export for `/admin/audit` (Sprint 7 decision H deferred this).

## Why this sprint exists

Sprint 8a closed the hardening loop; the JSON API is production-ready. ToR §4.4.2 mandates a web admin surface (`/web/...`) that hasn't been touched since Sprint 1 created the empty `app/web/` package. Admins currently need `curl` or Postman to operate the system. Sprint 8b makes the admin surface usable from a browser.

Sprint 8b ships:

- Keycloak OIDC redirect flow + encrypted session cookies for `/web/*` auth
- Jinja2-templated HTML for four ToR §4.4.2 pages: dashboard, batches, audit, sessions
- New `GET /api/v1/admin/dashboard` counters endpoint backing the dashboard page
- New `GET /api/v1/admin/batches/{id}/labels.pdf` PDF endpoint with `reportlab`-rendered QR labels (one per QR in the batch)
- New `GET /api/v1/admin/audit.csv` streaming CSV export endpoint (NOT content negotiation on `/admin/audit` — separate endpoint per Sprint 7 decision H)
- Inline force-close form on the sessions page that POSTs to `/api/v1/admin/sessions/{id}/force-close`

This completes the ToR §4.4.2 admin surface minus `/web/qr/search` (partly covered by Sprint 7 Task 2's `entity_id` audit filter — dedicated page deferred) and `/web/users/` (needs Keycloak admin client — deferred as its own sprint due to attack-surface increase).

## Scope boundaries

**In scope — 6 tasks:**

0. **Web auth + template scaffolding.** Keycloak OIDC redirect flow: `GET /web/login` → 302 to Keycloak `/protocol/openid-connect/auth` with `response_type=code`. `GET /web/oidc/callback` exchanges the code for tokens, encrypts user claims (`sub`, `email`, `roles`) into a Fernet-encrypted cookie (`cryptography` already in via `python-jose[cryptography]`), 302 to `/web/`. `GET /web/logout` clears the cookie + 302 to Keycloak `/protocol/openid-connect/logout`. New `Jinja2Templates` setup, `templates/_base.html` with header + nav + flash slot, `static/` with minimal CSS (no JS bundler — vanilla HTML). New `require_web_admin` cookie-auth dep — admin role + active shift gate every `/web/*` page; redirects to `/web/login?next=...` on cookie-missing/expired.

1. **`/web/` dashboard + counters endpoint.** New `GET /api/v1/admin/dashboard` returns aggregations: total QR codes (by status: `free`/`bound`/`retired`), batches created in last 30 days, active shifts count, audit-log rows in last 24h. Single SQL query (via a new `DashboardRepository.snapshot()` method) — counters are read from the DB, not computed in Python. The HTML page renders the numbers in a card grid.

2. **`/web/batches/` (list + detail) + PDF labels.** Two pages: `GET /web/batches/` shows recent batches with pagination + click-through; `GET /web/batches/{id}` shows batch metadata + a table of its QR codes with "Download labels" button. New `GET /api/v1/admin/batches/{id}/labels.pdf` returns `Content-Type: application/pdf` + `Content-Disposition: attachment; filename="batch-{id}.pdf"` rendered via `reportlab` — A4 page with N labels per page (each label = QR code + human-readable id), `reportlab.graphics.barcode.qr` for the QR rendering (no separate `qrcode` dep). New `app/services/pdf_labels.py` holds the layout logic.

3. **`/web/audit/` + CSV export.** `GET /web/audit/` is a list view consuming `GET /api/v1/admin/audit` with the 8 existing filters surfaced as form fields (user_keycloak_id, from, to, entity_type, entity_id, operation, session_id, result) + page/page_size; pagination links carry the filters in the query string. Each row links to a single-row detail page `GET /web/audit/{id}` (new endpoint variant of audit query). New `GET /api/v1/admin/audit.csv` accepts the same 8 filters + `page_size=10000` cap and streams CSV via `StreamingResponse` with `Content-Disposition: attachment; filename="audit-{timestamp}.csv"`. CSV columns mirror the JSON envelope's `results[]` shape (id, request_id, timestamp, user_email, user_keycloak_id, session_id, operation, entity_type, entity_id, result + JSON-encoded before_json/after_json). The CSV endpoint produces its own audit-of-audits row per ToR §5.4.6.

4. **`/web/sessions/`.** List view consuming `GET /api/v1/admin/sessions` with filters (user_keycloak_id, from, to, active_only) + pagination. Each row with an active shift has an inline force-close form (reason textarea + submit) that POSTs to `/api/v1/admin/sessions/{id}/force-close` — successful submit redirects back to the list view with a flash message. Already-ended rows show the end_reason + shift_end_at, no form.

5. **Acceptance + close-out.** Work-log entry + CLAUDE.md repository status + parking-lot updates + memory.

**Out of scope (Sprint 9+):**

- **`/web/qr/search`** — Sprint 7 Task 2's `entity_type=qr&entity_id=...` audit filter partially covers the use case via the audit page. Dedicated page can land in a future sprint when a real consumer asks for it.
- **`/web/users/`** — needs Keycloak admin client + `KEYCLOAK_ADMIN_CLIENT_*` env vars (Sprint 6 decision J deliberately avoided). Significant new attack surface; deserves its own sprint.
- **Cluster-wide rate-limit state** (Sprint 8a deferral, still carried).
- **Phase 2 partial-failure alerting** (Architecture §3.1 parking-lot, deferred since Sprint 3).
- **Idempotency-key TTL cleanup job** — pre-existing carry-over from Sprints 2-7.
- **Performance testing against production-like infra** — Sprint 8a Task 4's measure-and-document baseline is dev-loop only.

## Cross-cutting decisions

All confirmed at plan stage; per-task detail layered in during execution.

**A. Web auth uses Keycloak OIDC authorization-code flow with PKCE-skipped (server-side flow, `client_secret`).** Backend acts as a confidential OIDC client; the secret never leaves the server. New `KEYCLOAK_WEB_CLIENT_ID` + `KEYCLOAK_WEB_CLIENT_SECRET` Settings (env-driven, secret-marked). The mobile JWT bearer flow (Sprint 1) stays untouched — web is a separate OIDC client, server-side.

**B. Session cookie is Fernet-encrypted, NOT just signed.** CLAUDE.md mandates "encrypted session cookie." `cryptography.fernet.Fernet` (already transitively pulled via `python-jose[cryptography]`) provides authenticated encryption. Cookie payload is JSON `{"sub", "email", "roles", "exp"}`. New `SESSION_COOKIE_KEY` Setting (env-driven, secret-marked) — operators generate via `Fernet.generate_key()` once at deploy and persist. Cookie attributes: `HttpOnly=True, Secure=True, SameSite=Lax, Max-Age=8h` matching a working shift.

**C. `require_web_admin` cookie-auth dep mirrors `require_role_with_active_shift` for `/api/v1/admin/*`.** Decodes the cookie → reads `sub` + `roles` → checks role + queries `shift_sessions` for the active shift. If no cookie / expired / role mismatch → redirect to `/web/login?next=<current-path>` (NOT 401/403 — web auth uses redirects, not status codes). If no active shift → render an "open admin shift to continue" intermediate page with a "Start shift" button POSTing to `/api/v1/admin/sessions/start` (reuses Sprint 8a Task 0's endpoint).

**D. No SPA, no JS bundler, no compile step.** Plain HTML rendered by Jinja2. Optional vanilla `<script>` for form submission UX (e.g. confirm-on-force-close), kept inline. Pages must work without JavaScript — accessibility + simplicity.

**E. CSS is one hand-written `static/admin.css`.** No Tailwind, no preprocessor. ~200 lines of utility classes + page-specific overrides. Sprint 9+ can introduce a CSS framework if the surface grows.

**F. PDF batch labels use `reportlab` PyPI package** (pre-approved, decision confirmed at plan stage). Pure-Python, no system dependencies. Renders A4 pages of 8x4 labels (32 per page) with QR code + human-readable id underneath each. `reportlab.graphics.barcode.qr` handles the QR rendering — no separate `qrcode` dependency needed. Justification recorded in the Sprint 8b work-log deviations section.

**G. CSV export is a SEPARATE endpoint (`GET /admin/audit.csv`), NOT content negotiation on `/admin/audit`** (Sprint 7 decision H carried). Keeps the JSON contract pure; the CSV endpoint can have its own pagination behavior (`page_size=10000` cap instead of 100) and streaming semantics. Same 8 filters as the JSON endpoint.

**H. CSV endpoint produces its own audit-of-audits row.** Same shape as Sprint 7 Task 2's `/admin/audit` endpoint: `operation="audit.export_csv"`, `entity_type="audit"`, `entity_id="export"`, `after_json={"filters": ..., "rows_exported": N}`. The CSV export IS a sensitive read per ToR §5.4.6.

**I. `/web/*` paths bypass the rate-limit middleware via UNLIMITED classification.** They go through `require_web_admin` → which calls the admin JSON endpoints internally via FastAPI dep injection (not HTTP), so admin-bucket rate limits don't double-fire. Add `/web/` prefix detection to `_classify_request` in Sprint 8a Task 3's middleware. Alternative considered: keep rate limiting on `/web/*` paths but use a 4th `WEB` class — rejected as overkill since one human admin won't hit budgets.

**J. New Jinja2 dep made explicit** (`jinja2>=3.1,<4`) even though FastAPI's `Jinja2Templates` may have it transitively. Explicit pinning avoids surprises if FastAPI ever drops the transitive dep.

**K. Each page is also covered by an integration test.** Tests use `httpx.AsyncClient` + `ASGITransport`, drive the page with a valid cookie, parse the HTML response with a minimal assertion strategy (regex / `in` on snippets) — not a full HTML-parser dep. The point of the integration tests is "the page renders 200 + contains expected text," NOT "the DOM tree matches a schema."

**L. Web auth state DOES NOT use FastAPI dependency_overrides in production-style tests.** Tests construct valid cookies via the same Fernet key the app uses (read from a test-conftest Settings override) and pass them via `httpx.AsyncClient` cookies. This proves the cookie crypto path end-to-end, not just the dep-override seam.

## Task list

Each task gets full Goal / Steps / Acceptance / Anti-criteria / Suggested prompt added during execution. Skeleton names + one-line goals only here.

---

### Task 0 — Web auth + template scaffolding

**Goal:** OIDC redirect flow at `/web/{login,oidc/callback,logout}`, Fernet-encrypted session cookie, `require_web_admin` cookie-auth dep, Jinja2 setup with `_base.html` template, minimal `static/admin.css`, two new Settings (`KEYCLOAK_WEB_CLIENT_ID`, `KEYCLOAK_WEB_CLIENT_SECRET`, `SESSION_COOKIE_KEY`). New `app/web/auth.py` + `app/web/templates/` + `app/web/static/`. Add `/web/` prefix to rate-limit middleware UNLIMITED set. `jinja2` dep pre-approved (decision J).

### Task 1 — `/web/` dashboard + counters endpoint

**Goal:** New `app/db/repositories/dashboard.py` with a `DashboardRepository.snapshot()` returning `{qr_free_count, qr_bound_count, qr_retired_count, batches_last_30_days, active_shifts_count, audit_rows_last_24h}` via a single SQL UNION ALL. New `GET /api/v1/admin/dashboard` endpoint consuming it. Jinja2 template renders the numbers in a card grid. No PDF, no charts; numbers only.

### Task 2 — `/web/batches/` list + detail + PDF labels

**Goal:** Two Jinja2 templates: `batches/list.html` (paginated list), `batches/detail.html` (batch metadata + QR table + Download Labels button). New `GET /api/v1/admin/batches/` list endpoint (paginated). New `GET /api/v1/admin/batches/{id}/labels.pdf` endpoint using `reportlab` to render an A4 PDF with 32 labels per page (8×4 grid). New `app/services/pdf_labels.py` holds layout logic. `reportlab` dep pre-approved (decision F).

### Task 3 — `/web/audit/` + CSV export

**Goal:** Jinja2 templates: `audit/list.html` (filter form + paginated results + CSV download button), `audit/detail.html` (single-row JSON pretty-print). New `GET /api/v1/admin/audit.csv` streaming endpoint with `StreamingResponse`, `page_size=10000` cap, same 8 filters as `/admin/audit`, produces its own audit-of-audits row (`operation="audit.export_csv"` per decision H).

### Task 4 — `/web/sessions/` + inline force-close form

**Goal:** Jinja2 template: `sessions/list.html` (filters + paginated list + per-row force-close form for active shifts). Form action posts to `/api/v1/admin/sessions/{id}/force-close` with the reason; success flashes "Shift force-closed" and redirects back. Already-ended rows show end_reason + shift_end_at + no form.

### Task 5 — Acceptance + close-out

**Goal:** Sprint 8b done means tests green, gates clean, work-log + CLAUDE.md + memory + parking-lot updated. Pyproject deviations recorded (`jinja2`, `reportlab` — both pre-approved at plan stage decisions F + J). Document the Sprint 9+ deferrals: `/web/qr/search`, `/web/users/`, cluster-wide rate-limit state, Phase 2 alerting.

---

## Working principles (carried from Sprints 1–8a)

- **TDD discipline.** Tests first, including failure-mode counterparts. No happy-path-only tests.
- **Plan-then-confirm rhythm.** For each task, write the approach, get explicit "go", then code. Task 0 in particular (new auth surface + cookie crypto + OIDC flow + new env vars) deserves careful pre-implementation review.
- **One task at a time.** Don't start task N+1 until N's acceptance criteria are met.
- **Coverage 100%** on `app/`, per the bar held through Sprints 1–8a. `--cov-fail-under=100` gate at close-out.
- **Two new dependencies pre-approved at plan stage:** `jinja2`, `reportlab`. Both pure-Python, both well-scoped. Justifications recorded in the Sprint 8b work-log deviations section. No other deps without explicit approval.
- **Endpoint handler tests:** test handler logic by direct `await` for JSON endpoints; test HTML pages via `AsyncClient` (full ASGI stack including the cookie-auth dep). Same call as Sprints 2–8a.
- **CLAUDE.md cross-cutting rules #1–#7** are non-negotiable. Sprint 8b exercises #6 (Keycloak auth — web is a new OIDC client surface, but still goes through Keycloak; no local user table).
- **Reuse existing apparatus.** All `/web/*` pages consume EXISTING `/api/v1/admin/*` JSON endpoints internally — no duplicated business logic. PDF + CSV are the only genuinely new write/read paths.
- **`mypy app/ tests/` at every task close-out**, not just `mypy app/`.
- **No new env vars without a `Settings` field + a test** (Sprint 1 lesson, reinforced every sprint). Task 0 adds three new env vars (`KEYCLOAK_WEB_CLIENT_ID`, `KEYCLOAK_WEB_CLIENT_SECRET`, `SESSION_COOKIE_KEY`).

## Reference documents

- `DC_Inventory_ToR_v3.docx`:
  - **§4.4.2** — Web Interface section (Tasks 1-4 contract; lists the exact pages this sprint ships + the ones deferred to Sprint 9+)
  - **§5.4.6** — "Read operations on sensitive endpoints (audit log, user list) are also logged" — load-bearing for Task 3's CSV-export audit row (decision H)
  - **§5.4.7** — rate limiting (Task 0 adds `/web/` UNLIMITED bypass per decision I)
  - **§8.3** — Admin Endpoints table (Tasks 1 + 2 + 3 add three new admin endpoints: `/admin/dashboard`, `/admin/batches/{id}/labels.pdf`, `/admin/audit.csv`)
- `Architecture_Overview.md`:
  - **§6** — PDF generation (Task 2 deliverable)
- `docs/sprint-6.md` — decision J (Keycloak admin client deliberately avoided — load-bearing for `/web/users/` being out of scope)
- `docs/sprint-7.md` — decision H (CSV export as separate endpoint, not content negotiation — Task 3 implements this)
- `docs/sprint-8.md` — Sprint 8a plan; Sprint 8b builds on the hardened foundation
- `docs/work-log.md` — Sprint 8a entry's "Deliberately deferred (Sprint 9+)" list explicitly enumerates Sprint 8b's in-scope items
- `docs/parking-lot.md` — "Admin sessions surface" entry (RESOLVED across Sprints 7 + 8a) — Task 4 surfaces the existing force-close API to the web UI
