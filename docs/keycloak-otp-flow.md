# Mobile OTP flow — operator guide

A TOTP-based mobile login for the `dcinv-mobile` OIDC client. Each operator
sets up their own TOTP (via Google Authenticator / FreeOTP / Microsoft
Authenticator) the first time they log in to the mobile app, then enters a
6-digit time-based code on every subsequent login. The AD-federated
password is **not touched** — it keeps working for the web admin and any
other AD-integrated system.

This replaces an earlier static-PIN approach (see "History" at the bottom).
Same flow infrastructure, simpler operationally, much stronger security.

## How the flow looks to a mobile operator

**First login (one-time setup):**
1. Open the Android app → AppAuth opens Keycloak in a Custom Tab
2. Type short AD-username → Continue
3. Keycloak shows the **TOTP setup page** (`login-config-totp.ftl`,
   already styled in our theme) — operator scans the QR code with their
   authenticator app, types the first 6-digit code, optionally names the
   device → Submit
4. Back to the app with a JWT

**Every subsequent login:**
1. Type AD-username → Continue
2. Open authenticator app on phone, read 6-digit code → type → Войти
3. Back to the app

If they fluff the code 5 times in a row, Keycloak's built-in BruteForce
detector temporarily disables the user (default 5 min wait window).

## What changes vs the old AD-password flow

| | Mobile (`dcinv-mobile`) | Web admin (`dcinv-web`) | Windows / other AD systems |
|---|---|---|---|
| Username | short, e.g. `alice` | full AD username | AD username |
| Password / OTP | **6-digit TOTP** | AD-federated password | AD-federated password |
| Second factor | none (OTP IS the strong factor) | OTP via authenticator app | as-was |
| Audit `sub` in our backend | same Keycloak user | same Keycloak user | n/a |

Same person, same Keycloak user, same `sub` in JWTs across both clients —
so the backend's audit log doesn't care which flow they used.

## Why OTP over a static PIN

- **No Java SPI to maintain.** All Keycloak built-in features.
- **Replay-proof.** A code shown 31 seconds ago is invalid. Shoulder-
  surfing a code gives an attacker 0-30 seconds at best.
- **No admin-provisioned secret.** Users self-register their TOTP via
  the QR-scan page on first login. No "admin types PIN per user" step.
- **Standard recovery story.** Admin removes the OTP credential in
  Admin Console → user re-registers next login.

## Prerequisites — one-time setup on the Keycloak realm

1. **Enable brute-force protection** at realm level so OTP guessing is
   rate-limited (still good practice even though OTP is short-lived):
   - `Realm Settings → Security defenses → Brute Force Detection`
   - Mode: `Lockout temporarily`
   - Max login failures: `5`
   - Wait increment: `5 minutes`
   - Save

2. **Ensure OTP is enabled as a realm credential type:**
   - `Authentication → Required actions` → confirm `Configure OTP` is
     enabled (it is by default in Keycloak 26)

## Deploy the theme files

```bash
# From the project root, ship the theme files to the VM.
scp docs/keycloak/themes/business-green/login/login-username.ftl \
   adminkeycloak@srv-keycloak-ttc:~/keycloak-docker/keycloak-themes/business-green/login/
# login-otp.ftl + login-config-totp.ftl are already deployed from the
# earlier theme commit (c4715d5) — nothing else to ship.
ssh adminkeycloak@srv-keycloak-ttc \
   'cd ~/keycloak-docker && docker compose restart keycloak'
```

No JAR to deploy — we're using Keycloak's built-in OTP authenticator.

## Configure the new flow in Keycloak Admin Console

If you already followed the (now-obsolete) PIN guide and built a
`Mobile PIN Login` flow, edit it in place — just swap the PIN step for
OTP. Otherwise build from scratch:

1. **Duplicate the standard browser flow:**
   - `Authentication → Flows → browser → Action menu → Duplicate`
   - Name: `Mobile OTP Login` (or rename your existing
     `Mobile PIN Login` — Action → Rename)
   - Save

2. **Inside the duplicated flow:**
   - In the `forms` subflow:
     - **Remove** `Username Password Form` (trash icon)
     - **Add step** → pick `Username Form` (the standalone one) →
       Requirement = `Required`
     - **Add step** → pick `OTP Form` → Requirement = `Required`
   - The order **must** be `Username Form` first, then `OTP Form` —
     Keycloak's OTP step needs `context.getUser()` set by the username
     step.

3. **Bind the flow to the mobile client only:**
   - `Clients → dcinv-mobile → Advanced`
   - `Authentication Flow Overrides → Browser Flow = Mobile OTP Login`
   - Save

`dcinv-web` is **not** touched — it keeps using the default `browser`
flow with full AD password + OTP.

## First-time TOTP setup per operator

The first time an operator opens the Android app after the flow goes
live:

1. App opens Keycloak login → operator types AD username → Continue
2. Keycloak detects no TOTP is configured → shows the `login-config-totp.ftl`
   page (TTC-styled, with QR + manual code fallback)
3. Operator installs an authenticator app on their phone:
   - **Google Authenticator** (Android / iOS)
   - **Microsoft Authenticator** (Android / iOS)
   - **FreeOTP** (open-source, Android / iOS)
4. Operator scans the QR code in the app → app shows a rotating 6-digit
   code → operator types the current code into Keycloak + optionally
   names the device (e.g. "Pixel 8") → Submit
5. Operator lands in the Android app with a JWT
6. **Subsequent logins:** username → 6-digit code from the same
   authenticator app → in. No more QR scan.

The QR scan is one-time per phone. If the operator switches phones, an
admin must reset their OTP (see below) so they can re-register.

## Resetting a user's OTP (lost phone, new device)

Admin Console:

```
Users → [find the user] → Credentials tab
   Find the "otp" credential row → trash icon → Delete
```

Next time the user logs in via the mobile app, they'll see the TOTP
setup page again. No password change required.

## Rollback — 30 seconds

If anything goes wrong post-deploy, revert the client binding:

```
Clients → dcinv-mobile → Advanced → Authentication Flow Overrides
   Browser Flow = (none, i.e. default)
   Save
```

This puts the mobile client back on the standard `browser` flow (with
AD password + OTP — i.e. the original 30-char-AD-password experience).
Nothing else needs to change.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Operator gets "Invalid Username or Password" after typing the OTP code | Code expired (>30 sec stale) | Just retry — codes refresh every 30 sec. |
| Operator's phone clock drifted → all codes rejected | Time-sync issue | Set phone time to "automatic" / NTP. Codes are time-based. |
| Operator's `Configure OTP` page renders with default Keycloak styling | Theme not deployed | Check `login-config-totp.ftl` is in `~/keycloak-docker/keycloak-themes/business-green/login/`. |
| First-time setup page never shown — operator goes straight to OTP entry but has no app configured | They have an old OTP credential from another realm context | Admin: Users → user → Credentials → delete `otp` row. |
| `Configure OTP` listed under Required actions but never triggers | Flow puts OTP step in wrong order | `Username Form` must execute before `OTP Form`. |

## Security notes

- **TOTP secret is generated and stored in Keycloak's database** during
  the first-login setup. Plain Keycloak credential, hashed with the
  realm's credential policy.
- **Each code is valid for 30 seconds** + a small clock-skew window
  (Keycloak default ±1 step = 30 sec on each side).
- **Per-user lockout** via Keycloak's built-in BruteForceProtector —
  same mechanism that protects password attempts.
- **No fallback** to the AD password for mobile login. If a user's OTP
  is broken, they cannot use AD password as a backup on `dcinv-mobile`
  — admin must reset OTP first. This is intentional: a fallback would
  let attackers degrade strong auth back to weak.

## History — why this replaced a static PIN approach

Briefly explored a custom Java SPI (`MobilePinAuthenticator`) that
validated a per-user `mobile_pin` attribute. Code was written, deployed,
and verified end-to-end on prod — see commits `1810087` and `a236f84`
in git history if it ever needs resurrecting.

Decided to replace with TOTP after the first end-to-end test because:
1. A static 4-digit PIN is weak even behind VPN — shoulder-surfing or
   any leak is permanent.
2. Java SPI is one more thing to rebuild on every Keycloak upgrade.
3. Operators already carry phones; the authenticator-app step adds <5
   seconds vs typing a PIN.
4. Provisioning shifts from "admin sets PIN per user" to "user scans
   QR once" — less admin work.

The PIN SPI directory `keycloak-providers/dcinv-mobile-pin/` was
removed at the switch. The user-attribute declaration `mobile_pin` in
the realm's User Profile may also be deleted (no longer used) — it's
harmless if left for now.

## Future work

- WebAuthn / passkeys as an alternative second factor (Keycloak 26
  supports both as built-in authenticators).
- Per-user OTP rotation reminders via Keycloak Required Action.
- Tracking which operator logged in from which kiosk via custom JWT
  claim (probably overkill — audit log already captures this).
