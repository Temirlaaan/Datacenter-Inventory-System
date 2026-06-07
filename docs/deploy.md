# First deployment — operator checklist

Goal: get a working DC Inventory backend running against the real NetBox
(`https://web-netbox.t-cloud.kz/`) and Keycloak (`https://sso-ttc.t-cloud.kz/`)
in your environment.

Verified prerequisites (2026-06-04):

- NetBox 4.4.5, token valid, 762 devices loaded.
- `qr_id` custom field on Device — present.
- `asset_tag` — both native + custom field exist; code uses **native** (verified).
- Device status `Decommissioning` — present (user confirmed).
- Keycloak realm `prod-v1` reachable, JWKS resolves, OIDC discovery doc OK.

---

## 1. Keycloak — what to set up on the SSO side

You said `qr_id` is created and the web client secret exists. Still missing
(or unverified):

| What | How to do it in Keycloak admin UI |
|---|---|
| Web client `dcinv-web` | **Clients → Create client** → ID `dcinv-web`, type `confidential`. Settings tab → **Valid Redirect URIs**: `https://<your-backend-public-url>/web/oidc/callback`. **Web Origins**: `https://<your-backend-public-url>`. **Access Type**: confidential. Credentials tab → copy the secret into `KEYCLOAK_WEB_CLIENT_SECRET`. |
| Mobile client (for the Android app, can wait) | **Clients → Create client** → ID e.g. `dcinv-mobile`, type `public`. Authentication flow: standard + direct access grants. PKCE required. No secret. |
| Admin CLI client (for `/web/users/`, optional) | **Clients → Create client** → ID `dcinv-admin-cli`, type `confidential`. Authentication flow: **service accounts roles only** (no direct access, no standard flow). Credentials tab → copy the secret into `KEYCLOAK_ADMIN_CLIENT_SECRET`. Service Account Roles tab → assign **`realm-management.view-users`**. Leave the secret unset to disable `/web/users/`; the page renders a friendly "not configured" notice. |
| Roles | **Realm Roles → Create role** twice: `dcinv-admin` and `dcinv-mobile-user`. |
| Role mapping | Either assign roles to AD groups via group mappers, or assign directly to specific users. **You'll need `dcinv-admin` on your own account for the first smoke.** |
| Token-claim shape | Default Keycloak puts roles under `realm_access.roles`. Our code reads exactly that — no extra mapper needed. |

Quick check (from your laptop): hit `/web/login` of the running backend → it
should 302 to Keycloak, you authenticate, Keycloak 302s back to
`/web/oidc/callback`, and you land on `/web/`.

---

## 2. `.env` — what you currently have

Your `.env` (committed but secrets gitignored):

| Var | Status | Note |
|---|---|---|
| `NETBOX_URL` | ✓ set | `https://web-netbox.t-cloud.kz` |
| `NETBOX_SERVICE_TOKEN` | ✓ set | rotate after first deploy |
| `KEYCLOAK_BASE_URL` | ✓ set | `https://sso-ttc.t-cloud.kz` |
| `KEYCLOAK_REALM` | ✓ set | `prod-v1` |
| `KEYCLOAK_WEB_CLIENT_ID` | ✓ default | `dcinv-web` |
| `KEYCLOAK_WEB_CLIENT_SECRET` | ✓ set | confidential client secret |
| `SESSION_COOKIE_KEY` | ✓ set | Fernet key, generated 2026-06-04 |
| `COOKIE_SECURE` | ✓ set `false` | **flip to `true` once you're behind TLS** |
| `KEYCLOAK_ADMIN_CLIENT_ID` | optional | defaults to `dcinv-admin-cli`; set if the admin CLI client uses a different id |
| `KEYCLOAK_ADMIN_CLIENT_SECRET` | optional | secret for the `dcinv-admin-cli` confidential client; required to enable `/web/users/`. Leave unset → page shows "not configured" notice |
| `POSTGRES_PASSWORD` | ⚠️ `replace-me` | pick a real password before deploy |
| `DATABASE_URL` | (auto-assembled) | docker-compose constructs from `POSTGRES_*` |

---

## 3. Local smoke (before remote deploy)

```bash
cd backend
# Pick a real Postgres password first; edit .env, then:
docker compose up -d --build
# Wait ~10s for healthchecks to settle.
docker compose ps        # both services 'healthy'
curl -sS http://localhost:8000/health | jq .
docker compose logs -f dcinv-backend
```

If `/health` returns `ok` for `db / netbox / keycloak`, the stack is wired
correctly. If any sub-check fails, the JSON tells you which one + why.

To hit the OIDC web flow locally: open `http://localhost:8000/web/` in a
browser. Keycloak's `Valid Redirect URI` must include
`http://localhost:8000/web/oidc/callback` for this to work. **For
production**, configure the public URL instead.

---

## 4. Production deployment — what's still missing on your side

In rough order:

1. **A host.** VM with Docker, or Kubernetes cluster. Compose YAML works
   on a single VM as-is.
2. **DNS** for the backend public URL — e.g.
   `https://dcinv.t-cloud.kz` → your host.
3. **TLS termination.** Run nginx / Caddy / Traefik in front of the
   backend container; terminate HTTPS, proxy to `http://dcinv-backend:8000`.
   The backend itself listens plain on `:8000` inside the docker network —
   the reverse proxy adds TLS.
4. **Flip `COOKIE_SECURE=true`** in production `.env` once TLS is in
   front (browsers drop Secure cookies over plain HTTP; only flip after
   TLS is working).
5. **Update Keycloak `dcinv-web` redirect URI** to the public URL +
   `/web/oidc/callback`. (You can have BOTH localhost and prod URIs
   registered during the rollout.)
6. **VPN access** — the existing infra already requires VPN per ToR.
   Confirm the backend host is on the VPN-only network and the admins'
   browsers can reach it.

---

## 5. First-time admin onboarding

After deploy:

1. Authenticate to `https://<your-backend-public-url>/web/` →
   Keycloak login → land on the dashboard.
2. **You won't have an active admin shift yet.** The page renders the
   intermediate "Open admin shift" form. Type a workstation id (any
   string) + submit.
3. Now `/web/batches/`, `/web/audit/`, `/web/sessions/` all work.
4. Create your first QR batch via the mobile API or curl:
   ```bash
   curl -X POST https://<backend>/api/v1/admin/batches/ \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer <your-admin-jwt>" \
     -d '{"count": 32}'
   ```
5. Go to `/web/batches/`, click the batch, Download Labels (PDF), print.

---

## 6. Operational hygiene

- **Rotate secrets after first deploy:** `NETBOX_SERVICE_TOKEN`,
  `KEYCLOAK_WEB_CLIENT_SECRET`, and `SESSION_COOKIE_KEY` were shared in
  development chat or repo history; treat them as compromised.
- **`SESSION_COOKIE_KEY` rotation invalidates every active admin session
  on the spot** — operators re-login. Plan a maintenance window.
- **Backups:** `dcinv-db-data` Docker volume holds QR registry +
  `audit_log` + `shift_sessions`. NetBox is the source of truth for
  device data; the app DB is the only place for QR lifecycle + forensics.
  Set up regular `pg_dump` against the volume.
- **Monitor `/health`.** The endpoint returns `503` when any of
  db/netbox/keycloak is down. Wire your operational monitor (Zabbix?
  you already integrate with NetBox) to scrape it every 30–60s.
- **Watch `netbox_circuit` + `auto_end_job` sub-objects in `/health`.**
  Informational only — they DON'T flip overall status, but a sustained
  `netbox_circuit.state: OPEN` or `auto_end_job.last_iteration_at`
  going stale means something needs attention.

---

## 7. Carry-forwards (already on the parking-lot)

These don't block first deploy:

- `/web/qr/search` page (audit log's `entity_type=qr` filter partly covers)
- `/web/users/` page (needs Keycloak admin client)
- Cluster-wide rate-limit state (only matters multi-replica)
- CSRF token for `/web/*` form POSTs (relies on `SameSite=Lax` today)
- PDF download audit row
- `device_type.u_height` cosmetic regression on NetBox 4.x's slim device
  serializer (mobile device screen shows empty U value; second NetBox
  round-trip would fix it)
- Mobile Android app (separate workstream; backend's `/api/v1/*`
  contract is complete + documented via `/docs`).
