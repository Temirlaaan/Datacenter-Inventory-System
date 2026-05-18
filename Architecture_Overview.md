# DC Inventory System — Architecture Overview

> **Audience**: developers, ops engineers, security reviewers.
> **Companion to**: `DC_Inventory_ToR_v2.docx` (functional and acceptance requirements).
> **Status**: Draft for review.

This document captures the *how* of the DC Inventory system. The Terms of Reference cover *what* and *why*; here we cover technical decisions, data flows, and code-level patterns the implementation team needs.

---

## 1. System Diagram

```
                    ┌──────────────────────────────────────────────┐
                    │           Corporate Network                  │
                    │                                              │
  ┌──────────────┐  │  ┌─────────────────────┐                     │
  │  Mobile App  │──┼──┤    Backend API      │                     │
  │  (Android,   │  │  │    (FastAPI)        │◄────┐               │
  │   tablet)    │  │  │                     │     │ validate JWT  │
  └──────────────┘  │  │  ┌───────────────┐  │     │ via JWKS      │
                    │  │  │ /api/v1/*     │  │     │               │
  ┌──────────────┐  │  │  │ (mobile)      │  │  ┌──┴──────────┐    │
  │  Web Browser │──┼──┤  └───────────────┘  │  │  Keycloak   │    │
  │  (admin PC)  │  │  │  ┌───────────────┐  │  │  (existing) │    │
  └──────────────┘  │  │  │ /web/*        │  │  └──────┬──────┘    │
                    │  │  │ (admin UI)    │  │         │           │
                    │  │  └───────────────┘  │         │ AD sync   │
                    │  │                     │  ┌──────┴──────┐    │
                    │  │   ┌────────────┐    │  │ Active Dir. │    │
                    │  │   │ NetBox     │    │  │ (existing)  │    │
                    │  │   │ client     │────┼─►└─────────────┘    │
                    │  │   │ (httpx)    │    │                     │
                    │  │   └────────────┘    │     ┌─────────────┐ │
                    │  │                     │────►│  NetBox     │ │
                    │  └─────────┬───────────┘     │  (existing) │ │
                    │            │                 └─────────────┘ │
                    │  ┌─────────┴──────────┐                      │
                    │  │  PostgreSQL        │                      │
                    │  │  (Application DB)  │                      │
                    │  └────────────────────┘                      │
                    │                                              │
                    │  Promtail ─────────► Loki + Grafana (VM 2)   │
                    └──────────────────────────────────────────────┘
```

External access is only via corporate VPN. No component is exposed to the public internet.

---

## 2. Authentication Flow

### 2.1 Mobile login (Authorization Code Flow with PKCE)

```
Mobile App                 Keycloak              Backend API           NetBox
    │                         │                       │                  │
    │ 1. Open Custom Tab      │                       │                  │
    │    → /auth/realms/...   │                       │                  │
    ├────────────────────────►│                       │                  │
    │                         │                       │                  │
    │ 2. User enters AD       │                       │                  │
    │    credentials + 2FA    │                       │                  │
    │◄────────────────────────┤                       │                  │
    │                         │                       │                  │
    │ 3. Redirect with code   │                       │                  │
    │◄────────────────────────┤                       │                  │
    │                         │                       │                  │
    │ 4. POST /token          │                       │                  │
    │    code + verifier      │                       │                  │
    ├────────────────────────►│                       │                  │
    │                         │                       │                  │
    │ 5. access + refresh JWT │                       │                  │
    │◄────────────────────────┤                       │                  │
    │                         │                       │                  │
    │  ┌──────────────────────┴───────┐               │                  │
    │  │ Store in EncryptedSharedPrefs│               │                  │
    │  └──────────────────────────────┘               │                  │
    │                                                 │                  │
    │ 6. API call with Bearer <jwt>                   │                  │
    ├────────────────────────────────────────────────►│                  │
    │                         │                       │                  │
    │                         │ 7. Fetch JWKS (cached)│                  │
    │                         │◄──────────────────────┤                  │
    │                         │                       │                  │
    │                         │       ┌───────────────┴────────┐         │
    │                         │       │ Validate signature,    │         │
    │                         │       │ check exp, extract sub │         │
    │                         │       │ + email + roles claims │         │
    │                         │       └───────────────┬────────┘         │
    │                         │                       │                  │
    │                         │                       │ 8. NetBox call   │
    │                         │                       │    with service  │
    │                         │                       │    token         │
    │                         │                       ├─────────────────►│
    │                         │                       │                  │
    │                         │                       │  9. Response     │
    │                         │                       │◄─────────────────┤
    │ 10. Response            │                       │                  │
    │◄────────────────────────┴───────────────────────┤                  │
```

### 2.2 Web login (Authorization Code Flow + session cookie)

Same as mobile through step 5, except Backend stores user identity in an encrypted session cookie. Subsequent web requests carry the cookie; the backend does not need to validate a JWT on every request, only when the cookie expires (4 hours).

### 2.3 Token lifetimes

| Token | Lifetime | Storage |
|-------|----------|---------|
| Mobile access token (JWT) | 15 minutes | `EncryptedSharedPreferences`, refreshed automatically |
| Mobile refresh token | 12 hours | `EncryptedSharedPreferences`, used to mint new access tokens |
| Web session cookie | 4 hours sliding | `HttpOnly`, `Secure`, `SameSite=Lax` |
| JWKS cache (backend) | 1 hour | In-process memory |
| Idempotency key window | 24 hours | Redis (Phase 2) or PostgreSQL table (MVP) |

---

## 3. NetBox Interaction Patterns

### 3.1 Service token with per-operation attribution

Backend holds a single NetBox service token in environment. Every write operation has two steps:

```python
async def update_device(device_id: int, changes: dict, user: AuthUser, req_id: str):
    # Step 1: actual update
    response = await netbox.patch(
        f"/api/dcim/devices/{device_id}/",
        json=changes,
        headers={"If-Unmodified-Since": original_last_updated, "X-Request-ID": req_id},
    )
    if response.status_code == 412:
        raise ConflictError(...)
    
    # Step 2: attribution via journal entry
    diff_text = format_diff(changes_before=original, changes_after=response.json())
    await netbox.post(
        "/api/extras/journal-entries/",
        json={
            "assigned_object_type": "dcim.device",
            "assigned_object_id": device_id,
            "kind": "info",
            "comments": (
                f"Modified by {user.email} via mobile app.\n"
                f"Request ID: {req_id}\n"
                f"Session: {user.session_id}\n"
                f"Changes:\n{diff_text}"
            ),
        }
    )
    
    # Step 3: audit log in app DB
    await audit_log.record(
        request_id=req_id,
        user=user,
        operation="device.update",
        entity_id=device_id,
        before=original,
        after=response.json(),
    )
```

Three independent records of the same event: NetBox device data (the source of truth), NetBox journal entry (visible to anyone in the NetBox UI), and Application DB audit log (queryable for security forensics).

### 3.2 Optimistic concurrency control

NetBox stores `last_updated` on every object. The Backend uses it as an ETag-style version:

```python
# On read:
device = await netbox.get(f"/api/dcim/devices/{id}/")
return {
    "data": device,
    "version": device["last_updated"],
}

# On write (mobile sends If-Unmodified-Since: <version>):
@router.patch("/devices/{id}")
async def update_device(id: int, payload: UpdateDevicePayload,
                        if_unmodified_since: str = Header(...)):
    current = await netbox.get(f"/api/dcim/devices/{id}/")
    if current["last_updated"] != if_unmodified_since:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "DEVICE_CONFLICT",
                    "message": "Device was modified after you read it.",
                    "current_state": current,
                    "current_version": current["last_updated"],
                }
            }
        )
    # proceed with update
```

The mobile client handles 409 by showing a conflict dialog with the current values and allowing the user to either abort or retry with the new version.

### 3.3 NetBox client resilience

Wrap the httpx client in a retry-with-backoff helper:

- **Retry**: connection errors and 5xx (except 501) up to 3 times with exponential backoff (200ms, 600ms, 1800ms).
- **Don't retry**: 4xx (caller's fault), 501 (NetBox doesn't support).
- **Circuit breaker**: if 5+ failures occur within 30 seconds, return 503 immediately for the next 60 seconds. Prevents thundering herd when NetBox is degraded.
- **Timeout**: 5 seconds for reads, 10 seconds for writes.

---

## 4. QR Code Lifecycle (State Machine)

```
                  ┌─────────┐
       generate   │         │   bind to device (UC-2 or UC-3)
   ───────────────► free    ├──────────────────────────────┐
                  │         │                              │
                  └─────────┘                              │
                                                           ▼
                                                      ┌─────────┐
                              decommission device     │         │
                  ┌───────────────────────────────────┤  bound  │
                  ▼                                   │         │
                  ┌─────────┐                         └─────────┘
                  │         │
                  │ retired │   (terminal state)
                  │         │
                  └─────────┘
```

Transitions are guarded by database constraints:

```sql
ALTER TABLE qr_codes
  ADD CONSTRAINT qr_state_consistency CHECK (
    (status = 'free'    AND bound_to_device_id IS NULL AND retired_at IS NULL)
 OR (status = 'bound'   AND bound_to_device_id IS NOT NULL AND retired_at IS NULL)
 OR (status = 'retired' AND retired_at IS NOT NULL)
  );

-- Only one QR per device at a time
CREATE UNIQUE INDEX qr_one_per_device
  ON qr_codes (bound_to_device_id)
  WHERE status = 'bound';
```

The transition `free → bound` happens inside a database transaction together with the NetBox write, so partial states are impossible. If the NetBox write fails, the QR remains free; if the DB write fails, the NetBox change is rolled back via journal entry compensation.

---

## 5. Server-Driven Form Configuration

The Mobile App builds the device edit form dynamically from a config delivered by the Backend. The MVP source is a YAML file packaged with the Backend; Phase 2 moves it to the database.

### 5.1 Backend endpoint

```
GET /api/v1/meta/device-form
```

Returns:

```json
{
  "version": "2026-05-12.1",
  "fields": [
    {
      "key": "status",
      "label": "Status",
      "type": "choice",
      "required": true,
      "choices_endpoint": "/api/v1/meta/statuses",
      "confirmation": null
    },
    {
      "key": "site",
      "label": "Site",
      "type": "reference",
      "required": true,
      "search_endpoint": "/api/v1/meta/sites",
      "confirmation": {
        "trigger": "value_changed_from_initial",
        "message": "Changing site is a significant move. Confirm you are physically relocating this device."
      }
    },
    {
      "key": "rack",
      "label": "Rack",
      "type": "reference",
      "required": false,
      "search_endpoint": "/api/v1/meta/racks",
      "depends_on": ["site", "location"]
    },
    {
      "key": "position",
      "label": "Position (U)",
      "type": "integer",
      "min": 1,
      "max_from": "selected_rack.u_height",
      "depends_on": ["rack"]
    },
    {
      "key": "cf_asset_tag",
      "label": "Asset Tag",
      "type": "text",
      "max_length": 50,
      "netbox_field": "custom_fields.asset_tag"
    }
  ]
}
```

### 5.2 Mobile rendering

The mobile app has generic renderers for each `type` (`choice`, `reference`, `integer`, `text`, `multiline_text`, `boolean`). It does not know which fields exist by name. Adding a new editable field means editing the YAML on the Backend; no mobile release is needed.

### 5.3 Versioning

The `version` field changes whenever the config changes. The mobile app caches the form config locally and only re-fetches when the version differs. This avoids round-trips on every screen open.

---

## 6. Mobile App Internals (Kotlin / Jetpack Compose)

### 6.1 Key dependencies

- `androidx.camera:camera-camera2` + `androidx.camera:camera-lifecycle` — camera pipeline
- `com.google.mlkit:barcode-scanning` — QR decoding
- `androidx.security:security-crypto` — `EncryptedSharedPreferences`
- `net.openid:appauth-android` — OIDC client (handles PKCE)
- `com.squareup.okhttp3:okhttp` — HTTP with certificate pinning
- `com.squareup.retrofit2:retrofit` — typed API client
- `androidx.compose.*` — UI
- `androidx.navigation:navigation-compose` — navigation

### 6.2 Module structure

```
app/
├── auth/           # OIDC login, token storage, refresh logic
├── pin/            # PIN setup, verification, lockscreen
├── session/        # Shift start/end, idle timer
├── scanner/        # CameraX + ML Kit integration
├── api/            # Retrofit interfaces, OkHttp configuration, pinning
├── domain/         # Device, QR, Rack, etc. — pure Kotlin types
├── repository/     # Cached form config, NetBox lookups
├── ui/
│   ├── home/       # Scan button, shift indicator
│   ├── freeQr/     # Free QR screen
│   ├── device/     # Device view + edit
│   ├── search/     # Device search for binding
│   └── kiosk/      # Lock task management
└── kiosk/          # Device Owner mode helpers
```

### 6.3 Certificate pinning

```kotlin
val pinner = CertificatePinner.Builder()
    .add("dcinv-api.corp.local", "sha256/CURRENT_CERT_PIN=")
    .add("dcinv-api.corp.local", "sha256/NEXT_CERT_PIN=")   // for seamless rotation
    .build()

val client = OkHttpClient.Builder()
    .certificatePinner(pinner)
    .addInterceptor(authInterceptor)      // attaches Bearer token
    .addInterceptor(requestIdInterceptor) // X-Request-ID header
    .build()
```

### 6.4 Idle timeout

A single `IdleWatcher` observes user interactions (via Compose's `pointerInput` modifiers wrapping the entire NavHost). After 60 seconds of inactivity → PIN lock. After 10 minutes total (including time on the PIN screen) → full logout.

```kotlin
@Composable
fun rememberIdleWatcher(onIdle: () -> Unit, timeoutMs: Long): IdleWatcher {
    val lastTouch = remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(5_000)
            if (System.currentTimeMillis() - lastTouch.longValue > timeoutMs) {
                onIdle()
            }
        }
    }
    return IdleWatcher { lastTouch.longValue = System.currentTimeMillis() }
}
```

---

## 7. Backend Internals (Python / FastAPI)

### 7.1 Project structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # pydantic Settings
│   ├── auth/
│   │   ├── jwks.py          # JWKS cache + JWT validation
│   │   ├── dependencies.py  # FastAPI deps: current_user, require_role
│   │   └── keycloak.py      # token revocation, admin API calls
│   ├── netbox/
│   │   ├── client.py        # httpx wrapper with retry/circuit-breaker
│   │   └── models.py        # pydantic models for NetBox responses
│   ├── api/
│   │   ├── v1/
│   │   │   ├── qr.py        # /qr endpoints
│   │   │   ├── devices.py   # /devices endpoints
│   │   │   ├── meta.py      # /meta/* endpoints
│   │   │   ├── sessions.py  # /sessions endpoints
│   │   │   └── admin.py     # /admin/* endpoints
│   ├── web/
│   │   ├── routes.py        # HTML routes
│   │   └── templates/       # Jinja2
│   ├── db/
│   │   ├── models.py        # SQLAlchemy models
│   │   ├── session.py       # async session factory
│   │   └── repositories/    # data access per aggregate
│   ├── domain/
│   │   ├── qr.py            # state machine, business rules
│   │   ├── device.py        # editable fields policy
│   │   └── events.py        # domain event definitions
│   ├── services/
│   │   ├── qr_service.py    # use cases: generate batch, bind, retire
│   │   ├── device_service.py
│   │   ├── pdf_service.py
│   │   └── audit_service.py
│   └── observability/
│       ├── logging.py       # structlog config
│       └── metrics.py       # prometheus_client setup
├── alembic/                 # migrations
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── docker/
    ├── Dockerfile
    └── docker-compose.yml
```

### 7.2 Dependency injection

```python
# app/auth/dependencies.py

async def get_current_user(
    authorization: str = Header(...),
    jwks: JWKSCache = Depends(get_jwks_cache),
) -> AuthUser:
    token = parse_bearer(authorization)
    claims = jwks.verify(token)
    return AuthUser(
        sub=claims["sub"],
        email=claims["email"],
        roles=claims.get("realm_access", {}).get("roles", []),
        session_id=claims.get("session_state"),
    )

def require_role(role: str):
    async def check(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if role not in user.roles:
            raise HTTPException(403, "Forbidden")
        return user
    return check

# app/api/v1/devices.py
@router.patch("/{id}")
async def update_device(
    id: int,
    payload: UpdateDevicePayload,
    if_unmodified_since: str = Header(...),
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    netbox: NetBoxClient = Depends(get_netbox_client),
    audit: AuditService = Depends(get_audit_service),
):
    ...
```

### 7.3 Structured logging

Every log entry is JSON, with required fields injected by middleware:

```python
import structlog

logger = structlog.get_logger()

@app.middleware("http")
async def log_request(request, call_next):
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    structlog.contextvars.bind_contextvars(
        request_id=req_id,
        method=request.method,
        path=request.url.path,
    )
    start = time.monotonic()
    response = await call_next(request)
    logger.info("request_completed",
                status=response.status_code,
                latency_ms=int((time.monotonic() - start) * 1000))
    return response
```

Output (one line per event):

```json
{"event":"request_completed","request_id":"abc-123","method":"PATCH","path":"/api/v1/devices/42","status":200,"latency_ms":347,"user":"jane@corp","timestamp":"2026-05-12T14:23:11Z"}
```

---

## 8. Database Migrations and Backups

### 8.1 Migrations

Alembic, autogenerated from SQLAlchemy models. Migrations run automatically at container startup:

```bash
# In Dockerfile entrypoint
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Production rule: never run destructive migrations (drop column, drop table) in autoupgrade. They must be split into two releases — first soft-deprecate (stop using), then drop in the next release after verifying no rollback is needed.

### 8.2 Backups

```bash
# /etc/cron.hourly/dcinv-backup
#!/bin/bash
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=/var/backups/dcinv
mkdir -p $BACKUP_DIR
docker exec dcinv-db pg_dump -U dcinv dcinv > $BACKUP_DIR/dcinv-$TIMESTAMP.sql
# Keep 7 days local
find $BACKUP_DIR -name 'dcinv-*.sql' -mtime +7 -delete
# Offsite copy (daily)
if [ "$(date +%H)" = "03" ]; then
    rsync -a $BACKUP_DIR/dcinv-$TIMESTAMP.sql backup-host:/dcinv/
fi
```

PDF artifacts on the application volume are regenerable from the database (the YAML rendering logic is deterministic given the QR list), so they need only weekly backup.

---

## 9. Deployment Compose File (Skeleton)

```yaml
# docker-compose.yml
version: "3.9"
services:
  dcinv-db:
    image: postgres:15
    environment:
      POSTGRES_DB: dcinv
      POSTGRES_USER: dcinv
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    volumes:
      - dcinv-db-data:/var/lib/postgresql/data
    secrets:
      - db_password
    restart: unless-stopped

  dcinv-backend:
    image: registry.corp.local/dcinv/backend:${VERSION:-latest}
    environment:
      DATABASE_URL: postgresql+asyncpg://dcinv:${DB_PASS}@dcinv-db/dcinv
      NETBOX_URL: https://netbox.corp.local
      KEYCLOAK_BASE_URL: https://sso-ttc.t-cloud.kz
      KEYCLOAK_REALM: prod-v1
      LOG_LEVEL: INFO
    secrets:
      - netbox_service_token
      - db_password
    depends_on:
      - dcinv-db
    volumes:
      - dcinv-pdfs:/var/lib/dcinv/pdfs
    restart: unless-stopped

  dcinv-proxy:
    image: caddy:2
    ports:
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    depends_on:
      - dcinv-backend
    restart: unless-stopped

  promtail:
    image: grafana/promtail:latest
    volumes:
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - ./promtail-config.yml:/etc/promtail/config.yml:ro
    command: -config.file=/etc/promtail/config.yml
    restart: unless-stopped

secrets:
  netbox_service_token:
    file: ./secrets/netbox_service_token
  db_password:
    file: ./secrets/db_password

volumes:
  dcinv-db-data:
  dcinv-pdfs:
  caddy-data:
```

---

## 10. Testing Strategy

### 10.1 Backend

- **Unit tests** (`tests/unit`): pure domain logic with no external dependencies. QR state machine, validation rules, diff calculation. Fast, 100% deterministic.
- **Integration tests** (`tests/integration`): hit a real Postgres test container and a recorded NetBox API mock (using `respx` or `vcrpy`). Run on every CI build.
- **Contract tests**: validate request/response schemas against the OpenAPI spec. Catches drift between code and the contract.
- **End-to-end smoke** (`tests/smoke`): launches the full docker-compose stack against a test NetBox sandbox and verifies key flows. Run before release.

### 10.2 Mobile

- **Unit tests**: business logic in `domain/` and `repository/`. Standard JUnit.
- **UI tests**: Espresso/Compose UI tests for critical screens (login, scan, edit, save).
- **Manual checklist**: scanner behavior in low light, with damaged labels, with non-DCQR codes. Network failure scenarios.

### 10.3 Security tests

- Static analysis: `bandit` for Python, `detekt` for Kotlin.
- Dependency scanning: `pip-audit`, `gradle dependencyCheck`.
- JWT validation: tests for tampered tokens, expired tokens, wrong audience, wrong issuer.
- TLS pinning: test that the app rejects connections with wrong certificates.

---

## 11. Open Questions for Implementation Team

These are deliberately left to the implementation team to resolve during sprint planning, since they involve trade-offs that depend on developer familiarity:

1. **PDF library choice**: `reportlab` (mature, complex) vs `weasyprint` (HTML/CSS-based, simpler) vs `fpdf2` (lightweight). Recommend `reportlab` for precise label layout.
2. **Form rendering on mobile**: roll your own Compose form engine, or use a library like `form-builder-compose`? MVP can be hand-rolled given the small field set.
3. **Idempotency storage**: PostgreSQL table or Redis? MVP can use PostgreSQL; Redis is overkill for the load.
4. **PDF storage**: local volume (simple) vs object storage like MinIO (better for multi-instance scaling, not needed in MVP). Use local volume.
5. **Migration of existing 500 devices to QR labels**: ad-hoc script vs feature in the web admin. Defer to Phase 2 unless the operations team needs it sooner.

---

*End of architecture overview. Refer to the Terms of Reference for functional requirements, acceptance criteria, and security model.*
