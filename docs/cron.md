# DC Inventory — host cron jobs

Three operational cron entries run on the host (NOT inside any docker
container) per Sprint 9/10 decision A: ops scripts must survive an
app crash, so they can't depend on the FastAPI process being up.

| When | Script | Purpose | Doc |
|---|---|---|---|
| Daily 03:00 UTC | `scripts/backup.sh` | `pg_dump --format=custom` → S3 | `backup.md` |
| Daily 03:30 UTC | `scripts/idempotency_cleanup.sh` | Delete `idempotency_keys` rows > 24h | this file |
| Weekly Sun 04:00 UTC | `scripts/restore_validate.sh` | Prove the latest dump restores cleanly | this file |

`/health` surfaces freshness of two of the three via informational
sub-objects (`backups` + `restore_validation`). The idempotency
cleanup runs to completion every night and is bounded; if it ever
stops running, the table size starts climbing — alert on that with
`SELECT pg_total_relation_size('idempotency_keys')` from your
existing Postgres monitoring instead of adding a third
`/health.idempotency_cleanup` field.

## Shared environment

All three scripts read the same env file. Drop it at
`/etc/default/dcinv-backup` so the systemd / cron user has it on
PATH:

```bash
# /etc/default/dcinv-backup
POSTGRES_USER=dcinv
POSTGRES_PASSWORD=<from your .env>
POSTGRES_DB=dcinv
POSTGRES_CONTAINER=dcinv-db

# backup.sh + restore_validate.sh:
BACKUP_S3_BUCKET=s3://dcinv-backups
# BACKUP_S3_ENDPOINT_URL=https://storage.yandexcloud.net   # for non-AWS
AWS_ACCESS_KEY_ID=<from your storage provider>
AWS_SECRET_ACCESS_KEY=<from your storage provider>
BACKUP_LOCAL_DIR=/var/lib/dcinv-backups

# Marker paths read by /health:
DCINV_BACKUP_MARKER_PATH=/var/lib/dcinv-backups/last-success-marker
DCINV_RESTORE_MARKER_PATH=/var/lib/dcinv-backups/last-restore-validate
```

The marker files must be readable by the FastAPI app container so
`/health.backups` + `/health.restore_validation` can stat their mtime.
Mount the directory read-only into `dcinv-backend`:

```yaml
# docker-compose.yml — dcinv-backend service
volumes:
  - /var/lib/dcinv-backups:/var/lib/dcinv-backups:ro
environment:
  DCINV_BACKUP_MARKER_PATH: /var/lib/dcinv-backups/last-success-marker
  DCINV_RESTORE_MARKER_PATH: /var/lib/dcinv-backups/last-restore-validate
```

## Crontab

Put this in the user's crontab (`crontab -e`):

```cron
# DC Inventory — backups + cleanup + restore validation.
# All scripts read /etc/default/dcinv-backup for shared env.

# Daily pg_dump → S3 at 03:00 UTC.
0 3 * * * . /etc/default/dcinv-backup && /opt/dcinv/scripts/backup.sh \
    >>/var/log/dcinv-backup.log 2>&1

# Daily idempotency_keys sweep at 03:30 UTC (30 min after backup so the
# deleted rows are already in the day's dump).
30 3 * * * . /etc/default/dcinv-backup && /opt/dcinv/scripts/idempotency_cleanup.sh \
    >>/var/log/dcinv-idempotency-cleanup.log 2>&1

# Weekly restore-validate every Sunday at 04:00 UTC (after Saturday's
# backup) — spins up an ephemeral postgres:15 container, runs
# pg_restore --schema-only against the latest S3 dump, asserts five
# expected tables exist.
0 4 * * 0 . /etc/default/dcinv-backup && /opt/dcinv/scripts/restore_validate.sh \
    >>/var/log/dcinv-restore-validate.log 2>&1
```

## What each script does

### `backup.sh` (Sprint 9 Task 3)

`pg_dump --format=custom` inside the dcinv-db container → local
staging dir → S3. Touches the marker on success. Keeps the last 3
dumps locally. Full operator guide in `backup.md`.

### `idempotency_cleanup.sh` (Sprint 10 Task 0)

```sql
DELETE FROM idempotency_keys WHERE created_at < NOW() - INTERVAL '24 hours';
```

That's it. No marker file, no /health field — if cleanup stops
running, the table-size monitoring catches it. The 24-hour TTL is
generous: mobile retries happen within seconds; 24h handles even an
extended offline-queue pile-up. The cleanup happens INSIDE
`docker exec dcinv-db psql` (same approach as backup), so no Postgres
install needed on the host.

Adjust the TTL via `IDEMPOTENCY_TTL_INTERVAL` env var (default
`24 hours`, accepts any Postgres interval string e.g. `48 hours`,
`3 days`).

### `restore_validate.sh` (Sprint 10 Task 0)

The key operational question this answers: **can the backup
actually be restored?** Without this script, the only validation of
`backup.sh` is "the upload succeeded" — silent corruption goes
undetected until you actually need the backup.

What it does:

1. Find the latest `dcinv-*.dump` in the S3 bucket
2. Download to `tmpfs` (no disk writes)
3. Spin up an ephemeral `postgres:15` container (`--tmpfs`,
   `--rm`, random password — NEVER touches the production db)
4. `pg_restore --schema-only --no-owner --no-acl`
5. Assert at least 5 of our expected tables exist (`qr_codes`,
   `qr_batches`, `audit_log`, `shift_sessions`, `idempotency_keys`)
6. Touch the marker on success
7. `docker stop` the ephemeral container (always, via trap)

Schema-only (not full data) because:

- We're validating the dump's archive integrity + the schema's
  restorability, not the data
- Schema restore completes in seconds even for prod-sized dumps
- Avoids needing tens of GB of tmpfs

`/health.restore_validation.age_seconds` should stay below
`8 * 24 * 3600` = 691200 seconds. Alert above that — the weekly cron
hasn't completed in over a week.

## Verifying everything is wired up

After setting up cron, prove each piece works:

```bash
# 1. Manual one-shot runs.
sudo /opt/dcinv/scripts/backup.sh
sudo /opt/dcinv/scripts/idempotency_cleanup.sh
sudo /opt/dcinv/scripts/restore_validate.sh

# 2. Markers should both exist + be recent.
ls -la /var/lib/dcinv-backups/last-success-marker
ls -la /var/lib/dcinv-backups/last-restore-validate

# 3. /health reflects both.
curl -s https://qr-dc.t-cloud.kz/health | jq '.backups, .restore_validation'
# {
#   "configured": true,
#   "last_completed_at": "2026-06-08T03:00:14+00:00",
#   "age_seconds": 86400
# }
# {
#   "configured": true,
#   "last_completed_at": "2026-06-08T04:02:47+00:00",
#   "age_seconds": 82660
# }

# 4. The cron is scheduled.
crontab -l | grep dcinv
```

## Suggested external monitor alerts

```yaml
# Prometheus / Grafana alertmanager rules (conceptual — adjust to
# your scraping setup).

# Backup hasn't completed in over 30h → warning.
- alert: DCInvBackupStale
  expr: dcinv_backups_age_seconds > 108000
  for: 5m
  severity: warning

# Backup hasn't completed in over 50h → page someone.
- alert: DCInvBackupCritical
  expr: dcinv_backups_age_seconds > 180000
  for: 5m
  severity: critical

# Restore validation hasn't completed in over 8 days → warning.
- alert: DCInvRestoreValidateStale
  expr: dcinv_restore_validation_age_seconds > 691200
  for: 5m
  severity: warning
```

The metric exporter is out of scope for Sprint 10 — for now, scrape
`/health` directly and translate the sub-object fields. A proper
Prometheus exporter is Sprint 12+ work.
