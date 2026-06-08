# DC Inventory — Mobile API Guide

Documentation for the Android (Kotlin + Jetpack Compose) mobile app that
consumes the backend's `/api/v1/*` JSON endpoints. Hand this to your
mobile developer (or to Gemini in Android Studio) as the contract spec.

The backend itself is already running. Endpoint shapes are also browsable
live at:

- **Swagger UI**: `https://qr.dc.t-cloud.kz/docs`
- **OpenAPI JSON**: `https://qr.dc.t-cloud.kz/openapi.json`

This file is curated and opinionated; the OpenAPI doc is exhaustive and
machine-readable. Use both.

---

## 1. Stack + non-negotiables

- **Language:** Kotlin
- **UI:** Jetpack Compose
- **Form factor:** Android phone, kiosk-mode (Device Owner via Android
  Enterprise) — single-app lock.
- **Camera + QR scanning:** CameraX + ML Kit's barcode detector. QR
  payload format = the raw `DCQR-XXXXXXXX` string. Anything else =
  scan-error, ask to scan again.
- **Network:** VPN-only deployment. No public internet. Backend lives
  at `https://qr.dc.t-cloud.kz` (replace if your env differs).
- **Auth:** Keycloak OIDC. The mobile app uses the **PUBLIC** Keycloak
  client (`dcinv-mobile` or whatever it's named) with PKCE. The
  backend's `/web/*` flow is separate (server-side confidential client)
  and not the mobile app's concern.

### Cross-cutting backend invariants the mobile app must respect

1. **NetBox is the source of truth.** Don't cache device data locally
   for more than 60 seconds — if the user is on the device screen and
   NetBox changes, the screen is stale.
2. **The form is server-driven.** Don't hardcode field names. Fetch
   `GET /api/v1/meta/device-form` (or `/device-create-form`), render
   fields generically by `type`, submit the keyed values back as a
   PATCH/POST body.
3. **Every write needs an active shift.** Before any write endpoint
   call, ensure the user has called `POST /api/v1/sessions/start`. If
   the backend returns `409 NO_ACTIVE_SHIFT`, prompt the user to start
   a shift, then retry.
4. **All device updates are PATCH, never PUT.** Send only the fields
   the user actually changed.
5. **Optimistic concurrency via `version`.** Device reads return a
   `version` string (NetBox's `last_updated`). PATCH must include it as
   the `If-Unmodified-Since` HTTP header. Backend returns 409 if NetBox
   has changed in the meantime — re-fetch + ask the user to
   re-confirm.

---

## 2. Authentication

### Roles

| Role | What it lets you do |
|---|---|
| `dcinv-mobile-user` | Open/end shifts, scan QRs, bind/unbind QRs, read devices, update devices, create devices, add comments. |
| `dcinv-admin` | Everything above + generate batches of QR codes + decommission devices + force-close other people's shifts + view audit log. |

Roles come from the JWT's `realm_access.roles` claim. A user can have
both. Most field engineers have `dcinv-mobile-user`; team leads have
both.

### Login flow

Use AppAuth-Android (or any standard OIDC PKCE library):

1. Discovery: `GET https://sso-ttc.t-cloud.kz/realms/prod-v1/.well-known/openid-configuration`
2. Authorization endpoint: open in a Custom Tab.
3. Token exchange: backend doesn't proxy this; talk directly to
   Keycloak's `/protocol/openid-connect/token`.
4. Result: an `access_token` (JWT) + `refresh_token`. Store securely
   (Android Keystore-backed).

### Calling the backend

Every API call:

```
GET /api/v1/qr/DCQR-ABCD1234 HTTP/1.1
Host: qr.dc.t-cloud.kz
Authorization: Bearer <access_token>
```

The backend verifies the JWT signature against Keycloak's JWKS (cached
server-side for 1h). Bad/expired token → `401 Unauthorized`.

### Refreshing

Standard OIDC refresh. The backend doesn't care — it just sees a fresh
access token. Keycloak default access-token lifetime is 5 min; refresh
proactively.

---

## 2.5 Idempotency contract (Sprint 9 Task 0)

**Send `Idempotency-Key: <UUID>` on every write retry.** Datacenter
wifi drops; without it, your retry creates duplicates that you can't
undo from the mobile side.

### Which endpoints accept it

All 9 write endpoints: every `POST`/`PATCH` under `/api/v1/`. Reads
(`GET`) don't accept the header — they're already idempotent.

| Endpoint | Why it matters |
|---|---|
| `POST /sessions/start`, `POST /sessions/end` | Avoid duplicate-shift / double-end |
| `POST /qr/{id}/bind`, `POST /qr/{id}/retire` | DB partial unique index also protects, but the retry without idempotency surfaces 409 instead of the original 200 |
| `POST /devices/` | **Critical — NetBox has no native dedupe; without idempotency a retry creates a second device** |
| `PATCH /devices/{id}` | Optimistic-concurrency catches the second write but mobile sees confusing 409 instead of the original 200 |
| `POST /devices/{id}/comments` | **Critical — NetBox doesn't dedupe journal entries, so a retry leaves a duplicate comment row** |
| `POST /devices/{id}/decommission` | OCC catches duplicates but retry surfaces 409 instead of original 200 |
| `POST /admin/batches/` | Sprint 5 — already integrated; same contract |

### Client rules

1. **Generate a fresh UUIDv4 per logical action.** Tapping "Bind" once
   → one key. Tapping "Bind" again to bind a different QR → a
   different key. Tapping "Bind" again to retry the SAME action
   because of network failure → **the same key**.
2. **Persist the key locally** until you receive a 2xx/4xx response.
   If the app is killed mid-flight, the next launch should retry the
   stored key, not generate a new one.
3. **Max length 255 chars.** UUIDv4 = 36 chars, well within bounds.
4. **Same key + same payload → identical response.** Replay returns
   the exact (status, body) the original call produced, including
   4xx error bodies. Don't expect a "fresh" answer on retry.
5. **Same key + different payload → 422** with
   `{"detail": "Idempotency-Key reused with a different request payload"}`.
   This is a client bug — surface it loudly during dev.
6. **No key sent = no idempotency.** Server treats it as a fresh
   call every time. Acceptable for one-off curl scripts; **not
   acceptable for the mobile app in production**.

### Server semantics

- Replay window is the lifetime of the `idempotency_keys` row (a
  cleanup job removing rows > 24h old is in the Sprint 10 parking
  lot — until then, replay works forever).
- Idempotency layer uses a separate session from the actual work; in
  the rare race where two concurrent retries arrive simultaneously,
  the loser sees the winner's cached response. Both clients see
  consistent answers.
- The key is namespaced per user (`user_keycloak_id`, `key`) — two
  different engineers can use the same UUID without collision.

### Example

```http
POST /api/v1/qr/QR-7F3A2B/bind
Authorization: Bearer <JWT>
Idempotency-Key: 3d4f8e21-9a7c-4b6d-8e1f-2c5a9b8d7e6f
Content-Type: application/json

{"device_id": 1042, "version": "2026-06-08T12:34:56.789Z"}
```

Retry (network drop, no response received):

```http
POST /api/v1/qr/QR-7F3A2B/bind
Authorization: Bearer <JWT>
Idempotency-Key: 3d4f8e21-9a7c-4b6d-8e1f-2c5a9b8d7e6f   ← SAME UUID
Content-Type: application/json

{"device_id": 1042, "version": "2026-06-08T12:34:56.789Z"}
```

Second call returns the EXACT response of the first — whether that
was a 200 (bind succeeded) or a 409 `QR_ALREADY_BOUND` (someone else
got there first). Mobile UI flow is identical in both cases: the
user sees "Bound" or "Already bound — fetch and re-scan", they don't
need to know whether the network round-trip actually completed.

---

## 3. Endpoint catalogue

Grouped by the user journey, not alphabetically. For wire shapes,
look at `/openapi.json` (or copy-paste the FastAPI `BaseModel`
classes from `app/api/v1/`).

### 3.1 Shift session — start your day

**Start a shift** (mobile-user only):

```http
POST /api/v1/sessions/start
{"tablet_id": "tablet-04"}
```

`tablet_id` is whatever string uniquely identifies the device (Android
ID, MDM-assigned name, anything). Returns 200 + the shift object on
success, **409 SESSION_ALREADY_ACTIVE** if the user already has one
open (the response body contains the existing shift so you can show
"you have an open shift from 10:42 on tablet-03").

**Check current shift**:

```http
GET /api/v1/sessions/active
```

Returns `{"session": {...}}` or `{"session": null}`. Use on app launch
to decide whether to show the start-shift screen.

**End a shift**:

```http
POST /api/v1/sessions/end
{"end_reason": "manual"}
```

`end_reason` must be `"manual"` (user tapped end) or `"auto_timeout"`
(if you implement a client-side idle timer; the backend has its own
12-hour server-side safety net regardless).

### 3.2 QR scan — what is this QR?

```http
GET /api/v1/qr/DCQR-ABCD1234
```

Returns:

```json
{
  "qr": {
    "id": "DCQR-ABCD1234",
    "status": "free|bound|retired",
    "bound_to_device_id": 1042,    // null when status != bound
    "bound_at": "2026-06-04T10:30:00Z"  // null when status != bound
  },
  "device": {
    // populated only when qr.status == "bound"
    // shape per /api/v1/devices/{id} below
  }
}
```

Three branches:

- `status: "free"` → show "Bind to a device?" CTA.
- `status: "bound"` → show device details.
- `status: "retired"` → show "this code was retired on {retired_at},
  reason: {retired_reason}". Don't allow bind/unbind.
- 404 → unknown QR. Show "Not in registry — admin needs to generate
  a batch first."

### 3.3 Bind QR ↔ device

After scanning a FREE QR + the user selects a device to bind:

```http
POST /api/v1/qr/DCQR-ABCD1234/bind
{"device_id": 1042}
```

Returns 200 + the new state. Failure modes:

- `409 QR_NOT_FREE` — somebody else bound it between your read + write.
  Refresh + show the new state.
- `409 DEVICE_ALREADY_BOUND` — that device already has a different QR.
  Show which one.
- `404` — unknown QR or unknown device id.
- `422` — NetBox rejected the device update (e.g., invalid custom
  field). Body has NetBox's actual error message in
  `error.netbox_detail`.

### 3.4 Retire a QR (admin only)

```http
POST /api/v1/qr/DCQR-ABCD1234/retire
{"reason": "damaged sticker"}
```

Returns 200. If the QR was BOUND, also clears the device's `qr_id`
field as part of the same atomic operation.

### 3.5 Read device

```http
GET /api/v1/devices/1042
```

Returns:

```json
{
  "data": {
    "id": 1042,
    "name": "sw-rack-42-01",
    "status": {"value": "active", "label": "Active"},
    "site": {"id": 1, "name": "DC-1"},
    "rack": {"id": 7, "name": "R-42"},
    "position": 12,
    "serial": "ABC123",
    "asset_tag": "AT-9001",
    "comments": "core switch",
    "device_type": {"id": 11, "name": "C9300-48U"},
    "manufacturer": {"id": 21, "name": "Cisco"},
    "device_role": {"id": 31, "name": "Access Switch"},
    "u_height": null,           // empty on NetBox 4.x; see parking-lot
    "primary_ip4": "10.0.0.5/24",
    "primary_ip6": null,
    "last_updated": "2026-06-04T09:00:00Z",
    "qr_id": "DCQR-ABCD1234",   // null if no bound QR
    "custom_fields": null       // any extra cf NetBox has (besides qr_id)
  },
  "version": "2026-06-04T09:00:00Z"  // pass back as If-Unmodified-Since
}
```

### 3.6 Update device — server-driven form

Step 1: fetch the form config (cache client-side; the `version` field
on the response tells you when to refetch):

```http
GET /api/v1/meta/device-form
```

Returns the field list — keys, types, labels, validation, choices
endpoints. Mobile renders fields by `type`:

- `choice` → dropdown; choices from `choices_endpoint`
- `reference` → search-as-you-type; entries from `search_endpoint`
- `integer` → number input with `min` / `max_from`
- `text` → single-line text with `max_length`
- `multiline_text` → textarea
- `boolean` → toggle

Step 2: when the user submits, PATCH only the fields they changed:

```http
PATCH /api/v1/devices/1042
If-Unmodified-Since: 2026-06-04T09:00:00Z   ← from the device read's version
{"position": 14, "comments": "moved up two U"}
```

Failure modes:

- `409 VERSION_CONFLICT` — NetBox changed underneath you. Re-fetch,
  ask the user to re-confirm.
- `422 NetBoxValidationError` — NetBox rejected the payload. Show
  `error.netbox_detail` to the user.

### 3.7 Create device

Step 1: fetch `GET /api/v1/meta/device-create-form` (different field
set from the edit form — adds `device_type_id` + `role_id` which are
required at creation but immutable after).

Step 2:

```http
POST /api/v1/devices/
Idempotency-Key: <client-generated-UUID>   ← optional but recommended
{ ...form values keyed by field key... }
```

The `Idempotency-Key` header makes safe-retry possible — if the
network blips between request and 201 response, retrying with the
same key returns the original response without creating a second
device.

### 3.8 Add a comment (read-only on the device, append-only journal)

```http
POST /api/v1/devices/1042/comments
{"comment": "rebooted at 14:32, fans spinning"}
```

Appends to the device's NetBox journal entries. Doesn't change device
fields. Use this when the engineer wants to record a note that
doesn't fit any form field.

### 3.9 Decommission (admin only)

```http
POST /api/v1/devices/1042/decommission
{"reason": "EOL, removed from rack"}
```

Behind the scenes: retires the bound QR (if any), sets NetBox status
to `Decommissioning`, writes a journal entry, writes an audit row.
Returns 200.

### 3.10 Static lookups (for choice/reference fields)

```http
GET /api/v1/meta/sites             ← cache 5 min
GET /api/v1/meta/racks?site_id=1   ← cache 5 min, parameterised by site
GET /api/v1/meta/statuses          ← cache 5 min
```

These come from NetBox via a 5-min server-side cache (cheap).

### 3.11 Generate QR batch (admin only)

```http
POST /api/v1/admin/batches/
Idempotency-Key: <client-uuid>
{"count": 32, "comment": "Q3 replenishment for rack-42"}
```

Returns 201 + the new batch's id + the QR ids. The admin uses the web
UI (`/web/batches/{id}`) to download the printable PDF — mobile app
doesn't need to expose this. But it CAN expose batch generation as an
admin action.

---

## 4. Error model

All structured errors return:

```json
{
  "error": {
    "code": "MACHINE_READABLE_CODE",
    "message": "Human-readable explanation",
    // optional extras depending on the error...
    "retry_after_seconds": 30,
    "netbox_detail": {...}
  }
}
```

| Code | HTTP | Meaning + action |
|---|---|---|
| `NO_ACTIVE_SHIFT` | 409 | User must `POST /sessions/start` first. |
| `SESSION_ALREADY_ACTIVE` | 409 | Body contains the existing shift. Show "you have a shift open since X." |
| `QR_NOT_FREE` | 409 | Refresh QR state. |
| `DEVICE_ALREADY_BOUND` | 409 | Show which other QR is bound. |
| `VERSION_CONFLICT` | 409 | Re-fetch device, ask user to re-confirm. |
| `NetBoxValidationError` | 422 | Surface `netbox_detail` to user. |
| `RATE_LIMIT_EXCEEDED` | 429 | Show `Retry-After`. |
| `NETBOX_CIRCUIT_OPEN` | 503 | NetBox is down; show "service degraded, try again in {retry_after_seconds}s." |
| (any) | 502 | NetBox returned a bad response; retry once, then surface as "NetBox upstream error." |

For 401 / 403, refresh the token and retry once; if still failing,
re-authenticate.

---

## 5. UX patterns the backend assumes

- **Scan first, decide later.** Every QR scan hits `GET /api/v1/qr/{id}`.
  The response shape tells you what screen to show next.
- **Start a shift on app launch.** Persisting an "active shift" client
  side is fine, but always verify with `GET /api/v1/sessions/active`
  on app foreground.
- **Cache static lookups for 5 minutes.** Sites, racks, statuses,
  device-types. The backend's own cache is also 5 minutes; respect
  it to keep the round-trip count low.
- **Don't cache device reads beyond 60 seconds.** NetBox is the
  source of truth; staleness over a minute is unacceptable.
- **Form version field**: cache the form config client-side. On each
  config fetch, compare the returned `version` to your cached one;
  if changed, invalidate the rendered form.

---

## 6. What's NOT in scope for the mobile app

- The web admin pages (`/web/*`). Those are browser-only for desk
  admins. Mobile never opens a webview to them.
- PDF batch labels. Web-only download.
- Audit log browsing + CSV export. Web-only.
- Force-closing other people's shifts. Web-only.
- Cluster-wide rate-limit state, NetBox circuit-breaker internals.

---

## 7. Sample API contracts

### Wire-shape for `POST /api/v1/qr/{id}/bind`

Request body:
```json
{"device_id": 1042}
```

Success (200):
```json
{
  "qr": {
    "id": "DCQR-ABCD1234",
    "status": "bound",
    "bound_to_device_id": 1042,
    "bound_at": "2026-06-04T10:30:00Z"
  },
  "device": { ... full device shape ... }
}
```

Failure (409 QR_NOT_FREE):
```json
{
  "error": {
    "code": "QR_NOT_FREE",
    "message": "QR is currently bound|retired; refresh and try again.",
    "current_status": "bound"
  }
}
```

### Wire-shape for `PATCH /api/v1/devices/{id}`

Request headers:
```
Authorization: Bearer ...
If-Unmodified-Since: 2026-06-04T09:00:00Z
Content-Type: application/json
```

Request body (only changed keys):
```json
{"position": 14, "comments": "moved up two U"}
```

Success (200):
```json
{
  "data": { ... full updated device shape ... },
  "version": "2026-06-04T10:31:00Z"
}
```

Failure (409 VERSION_CONFLICT):
```json
{
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "Device was updated after you read it; refresh and try again.",
    "current_version": "2026-06-04T10:30:45Z"
  }
}
```

---

## 8. Open questions for the mobile dev to answer

- Offline mode? The backend has no offline-sync support — every write
  hits NetBox synchronously. If the field has spotty connectivity,
  decide whether to queue writes client-side or to require a live
  connection for any state-changing operation.
- ML Kit barcode detector vs ZXing — preference is up to mobile.
- Idle-timeout duration for the client-side "end shift on idle"
  prompt (the server's 12-hour safety net catches forgotten shifts;
  client-side idle is a UX nicety on top).

When in doubt, ask. The backend is the source of truth for what's
allowed; mobile UI choices are a separate conversation.
