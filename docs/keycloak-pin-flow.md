# Mobile PIN flow — operator guide

A 4-digit PIN login for the `dcinv-mobile` OIDC client. Each operator's PIN
is stored in their Keycloak user profile as the `mobile_pin` attribute and
validated by the [`dcinv-mobile-pin`](../keycloak-providers/dcinv-mobile-pin/)
Authenticator SPI. The AD-federated password is **not touched** — it keeps
working for the web admin and any other AD-integrated system.

## How the flow looks to a mobile operator

1. Open the Android app → AppAuth opens Keycloak in a Custom Tab
2. Keycloak shows the standard login (login.ftl) — operator types short
   AD-username (e.g. `alice`) + clicks Next
3. Keycloak shows the **Mobile PIN page** — operator types 4 digits + Войти
4. Back to the app with a JWT

If they fluff the PIN 5 times in a row, Keycloak's built-in BruteForce
detector temporarily disables the user (default 5 min wait window).

## What changes vs the current AD-password flow

| | Mobile (`dcinv-mobile`) | Web admin (`dcinv-web`) | Windows / other AD systems |
|---|---|---|---|
| Username | short, e.g. `alice` | full AD username | AD username |
| Password / PIN | **4-digit PIN** | AD-federated password | AD-federated password |
| OTP | **removed** | required | as-was |
| Audit `sub` in our backend | same Keycloak user | same Keycloak user | n/a |

Same person, same Keycloak user, same `sub` in JWTs across both clients —
so the backend's audit log doesn't care which flow they used.

## Prerequisites — one-time setup on the Keycloak realm

1. **Declare the `mobile_pin` attribute** in the User Profile so Keycloak
   knows it's a valid attribute (not random unmanaged data):
   - `Realm Settings → User Profile → Create attribute`
   - Name: `mobile_pin`
   - Display name: `Mobile PIN`
   - Permissions: Admin view + Admin edit (NOT user view/edit — operators
     must not be able to read or change their own PIN through Account
     Console)
   - Validations (optional but recommended): `pattern = ^\d{4}$`
   - Save

2. **Enable brute-force protection** at realm level so PIN guessing is
   rate-limited:
   - `Realm Settings → Security defenses → Brute Force Detection`
   - Mode: `Lockout temporarily`
   - Max login failures: `5`
   - Wait increment: `5 minutes`
   - Save

## Build the SPI JAR

You don't need Java/Maven installed locally — the build runs inside a
Maven Docker image and writes the JAR to `target/`.

```bash
cd keycloak-providers/dcinv-mobile-pin
docker run --rm \
  -v "$(pwd)":/build \
  -v "$HOME/.m2":/root/.m2 \
  -w /build \
  maven:3.9-eclipse-temurin-17 \
  mvn clean package
# → target/dcinv-mobile-pin-1.0.0.jar
```

The `~/.m2` mount caches downloaded dependencies between runs — first build
takes ~30 s for the dependency download, subsequent builds are ~5 s.

## Deploy the JAR to the Keycloak VM

```bash
# 1. Ship the JAR.
scp keycloak-providers/dcinv-mobile-pin/target/dcinv-mobile-pin-1.0.0.jar \
   adminkeycloak@srv-keycloak-ttc:~/keycloak-docker/providers/
```

Where `providers/` is the directory that's bind-mounted into the Keycloak
container at `/opt/keycloak/providers/`. If you don't have a `providers/`
mount yet, see "First-time deploy" below.

```bash
# 2. Rebuild Keycloak's augmented server JAR so it picks up the new SPI.
#    Keycloak 24 requires this after dropping a provider — it caches the
#    SPI registry at build time.
ssh adminkeycloak@srv-keycloak-ttc \
   'cd ~/keycloak-docker && docker compose exec keycloak /opt/keycloak/bin/kc.sh build && docker compose restart keycloak'
```

(If you're on Quarkus dev mode / `start-dev`, the build step isn't needed
— Keycloak hot-reloads providers. Production usually runs `start`, which
requires the build.)

Ship the theme template + messages bundle alongside (same theme deploy
flow as docs/keycloak-theme.md):

```bash
scp docs/keycloak/themes/business-green/login/login-mobile-pin.ftl \
   adminkeycloak@srv-keycloak-ttc:~/keycloak-docker/keycloak-themes/business-green/login/
scp -r docs/keycloak/themes/business-green/login/messages \
   adminkeycloak@srv-keycloak-ttc:~/keycloak-docker/keycloak-themes/business-green/login/
ssh adminkeycloak@srv-keycloak-ttc \
   'cd ~/keycloak-docker && docker compose restart keycloak'
```

### First-time deploy: mount the providers directory

If `~/keycloak-docker/providers/` doesn't exist yet, add a volume mount to
`docker-compose.yml`:

```yaml
services:
  keycloak:
    volumes:
      - ./providers:/opt/keycloak/providers:ro
      - ./keycloak-themes:/opt/keycloak/themes:ro   # already there
```

Then `mkdir ./providers && docker compose up -d` and ship the JAR.

## Configure the new flow in Keycloak Admin Console

After the JAR is loaded, our authenticator appears in the dropdown when
you build authentication flows. Pick it carefully — small details matter.

1. **Duplicate the standard browser flow:**
   - `Authentication → Flows → browser → Action menu → Duplicate`
   - Name: `Mobile PIN Login`
   - Save

2. **Replace the password step with our PIN step:**
   - Open the new `Mobile PIN Login` flow
   - Find the `forms` subflow → expand it
   - Find `Username Password Form` (Required) → click the **trash icon** to remove it
   - Click `Add step` inside the `forms` subflow
   - From the dropdown, pick `Username Form` (the standard one — collects username only)
   - Set it to `Required`
   - Click `Add step` again inside `forms`
   - Pick `DC Inventory Mobile PIN` (our SPI)
   - Set it to `Required`

3. **Disable OTP for this flow:**
   - Find `Browser - Conditional OTP` (or whatever your realm calls the
     OTP subflow) → set to `Disabled`
   - Save

4. **Bind the flow to the mobile client only:**
   - `Clients → dcinv-mobile`
   - `Advanced` tab → `Authentication Flow Overrides`
   - `Browser Flow` = `Mobile PIN Login`
   - Save

`dcinv-web` is **not** touched — it keeps using the default `browser` flow
with full AD password + OTP.

## Provision a PIN for an operator

```
Users → [find the user] → Details tab
```

Scroll down the user form — you'll see a **Mobile PIN** field (because
you declared it as a User Profile attribute). Type 4 digits, Save.

The user can now log in via the mobile app with their AD username + this
PIN. Their AD password keeps working for the web admin.

Same flow to **change** or **clear** a PIN — change the field value, or
empty it to disable mobile access for that user.

## Rollback — 30 seconds

If anything goes wrong post-deploy, revert the client binding:

```
Clients → dcinv-mobile → Advanced → Authentication Flow Overrides
   Browser Flow = (none, i.e. default)
   Save
```

This puts the mobile client back on the standard browser flow (with AD
password + OTP). Nothing else needs to change. No JAR removal, no
container restart — the SPI just stops being invoked.

If you also want to remove the SPI entirely:

```bash
ssh adminkeycloak@srv-keycloak-ttc
cd ~/keycloak-docker
rm providers/dcinv-mobile-pin-1.0.0.jar
docker compose exec keycloak /opt/keycloak/bin/kc.sh build
docker compose restart keycloak
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `DC Inventory Mobile PIN` doesn't appear in the Add Step dropdown | SPI not loaded | Check `docker compose logs keycloak | grep -i mobile-pin`; rerun `kc.sh build`. |
| `Mobile PIN` field doesn't appear in user form | Attribute not declared in User Profile | Realm Settings → User Profile → Create attribute (see Prerequisites). |
| Operator gets stuck on PIN page with "Wrong username or PIN" even though PIN is correct | User has no `mobile_pin` attribute set, or it was saved as multi-value | Open user → check field; if multi-value showed, single-value the attribute and re-enter. |
| BruteForce lockout fires too early / too late | Wrong realm-level settings | Realm Settings → Security defenses → Brute Force Detection. |
| Mobile app gets stuck on the username screen | Username Form step not included or set to Alternative | Authentication → Flows → Mobile PIN Login → Username Form must be Required. |

## Security notes

- **PIN is stored as plain text** in the `mobile_pin` user attribute. The
  Keycloak DB is behind VPN, and 4 decimal digits = ~13 bits of entropy —
  hashing offers minimal real protection (any leak of the DB lets an
  attacker brute-force all 10 000 combos in milliseconds). The architectural
  defense is brute-force lockout + VPN, not PIN hashing.
- **Generic error messages.** The Java authenticator never says "wrong PIN"
  vs "no PIN configured" vs "user disabled" — same message every time, so
  the page doesn't enumerate which usernames have mobile access.
- **Constant-time PIN compare** to avoid prefix-timing leaks.
- **Per-user lockout** via Keycloak's built-in BruteForceProtector — a
  failed PIN counts toward the user's failed-attempts budget exactly like
  a failed password attempt.

## Future work (not now)

- PBKDF2-hashed PIN storage. The two helper methods are easy to swap in —
  `recordFailedAttempt` and the `constantTimeEquals` line stay the same;
  only the comparison changes.
- Per-user PIN rotation reminders (Keycloak Required Action that prompts
  every N days). Useful once operator count grows past ~30.
- Custom PIN setup UI in our web admin (`/web/operators/{id}/pin`).
  Currently provisioning is admin-only via the Keycloak Admin Console;
  that's enough for ~10-50 operators.
