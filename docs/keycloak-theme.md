# Keycloak theme — `business-green`

Source of truth for the custom Keycloak login theme used by this project.
Lives at [docs/keycloak/themes/business-green/](keycloak/themes/business-green/);
gets `scp`'d to the Keycloak VM and mounted into the Keycloak container.

## What this theme covers

| Page | File | State |
|---|---|---|
| Main login (username + password) | `login.ftl` | Original |
| OTP input | `login-otp.ftl` | Original |
| Reset password | `login-reset-password.ftl` | Original |
| Update password | `login-update-password.ftl` | Original |
| TOTP setup | `login-config-totp.ftl` | Original |
| Logout confirm | `logout-confirm.ftl` | **Added 2026-06-11** |
| Generic error | `error.ftl` | **Added 2026-06-11** |
| Info message | `info.ftl` | **Added 2026-06-11** |
| Session expired | `login-page-expired.ftl` | **Added 2026-06-11** |
| 2FA method selector | `select-authenticator.ftl` | **Added 2026-06-11** |

Anything else falls back to the parent Keycloak theme (`keycloak/base`).

## Required images (NOT in this repo)

The `.ftl` files reference these paths — they must exist in
`resources/img/` on the Keycloak VM, else the layout breaks:

| Path | Purpose |
|---|---|
| `resources/img/ttc.logo2.svg` | Company logo (180px tall on login pages) |
| `resources/img/mountain.jpg` | Left-panel background on the main login |
| `resources/img/favicon.png` | Browser tab favicon |

Binary assets aren't committed (they don't belong in source control). If
the VM ever needs to be rebuilt from scratch, copy them from the
existing `business-green-backup/` directory or restore from VM backup.

## Deploying changes to the VM

The Keycloak VM is `srv-keycloak-ttc`. The theme is bind-mounted at
`~/keycloak-docker/keycloak-themes/business-green/`.

```bash
# 1. From the project root, ship the updated theme to the VM.
scp -r docs/keycloak/themes/business-green/login/*.ftl \
   adminkeycloak@srv-keycloak-ttc:~/keycloak-docker/keycloak-themes/business-green/login/

scp docs/keycloak/themes/business-green/login/theme.properties \
   adminkeycloak@srv-keycloak-ttc:~/keycloak-docker/keycloak-themes/business-green/login/

scp docs/keycloak/themes/business-green/login/resources/css/*.css \
   adminkeycloak@srv-keycloak-ttc:~/keycloak-docker/keycloak-themes/business-green/login/resources/css/

# 2. Restart the Keycloak container so it re-reads templates.
ssh adminkeycloak@srv-keycloak-ttc \
   'cd ~/keycloak-docker && docker compose restart keycloak'
```

Keycloak caches templates, so the restart is required.

## Rollback

The VM keeps a snapshot at `keycloak-themes/business-green-backup/`.
To revert:

```bash
ssh adminkeycloak@srv-keycloak-ttc
cd ~/keycloak-docker/keycloak-themes
rm -rf business-green
cp -r business-green-backup business-green
cd ~/keycloak-docker && docker compose restart keycloak
```

Then check what's in git: `git log -- docs/keycloak/themes/`.

## Testing locally before pushing to the VM

If you have docker + a local Keycloak instance:

```bash
docker run -p 8080:8080 \
   -v "$(pwd)/docs/keycloak/themes/business-green:/opt/keycloak/themes/business-green:ro" \
   -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD=admin \
   quay.io/keycloak/keycloak:24.0.3 start-dev
```

Then create a test realm at `http://localhost:8080`, set its Login theme
to `business-green` in `Realm Settings → Themes`, and walk through the
pages you care about.

## What I deliberately did NOT touch

- The `*.css-bc*` backup files under `resources/css/` on the VM — those
  are your manual safety net, none of my business.
- `resources/js/totp-polisfah.jss` — looks abandoned (`.jss` extension,
  no reference from any `.ftl`). Left alone.
- The PIN-flow / mobile-PIN auth changes — that's a separate workstream,
  not included in this theme.

## Decision history

- **2026-06-11**: imported live theme from VM under git; added 5 missing
  pages (logout-confirm + error + info + login-page-expired +
  select-authenticator) which previously fell back to the default
  Keycloak theme and looked broken. Removed `.pf-c-button { background:
  none !important }` rule from CSS that was killing fallback-page button
  styling. Scoped the universal `* { margin: 0; padding: 0 }` reset to
  card components so default `<h1>` / `<p>` on fallback pages don't sit
  flush in the top-left corner. Bumped logo height 150 → 180 px on
  desktop. Removed unused social-provider block from `login.ftl`.
