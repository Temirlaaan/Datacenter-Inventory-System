# DC Inventory — backup + restore

Sprint 9 Task 3. PostgreSQL data lives in a docker volume; if the disk dies,
everything in `qr_codes`, `qr_batches`, `audit_log`, `shift_sessions`,
`idempotency_keys` is gone. NetBox is **not** a backup for these tables — they
hold history NetBox doesn't.

This guide sets up daily `pg_dump` → S3 with health-check freshness reporting.

## What you get

- **`scripts/backup.sh`** — runs `pg_dump --format=custom` inside the
  `dcinv-db` container, writes the dump to a local staging directory,
  uploads to S3 via `aws s3 cp`, then touches a marker file. Keeps the
  last 3 dumps on disk; older ones live only in S3.
- **`scripts/restore.sh`** — fetches a named dump from S3 (or uses a
  local copy if present), prompts for `restore` confirmation, drops and
  recreates the database, runs `pg_restore`.
- **`GET /health.backups`** — informational sub-object reading the
  marker file's mtime. External monitors (Grafana / Prometheus alert
  manager) read this and alert on staleness.

## Why outside the container (decision H)

`scripts/backup.sh` runs on the **host** via cron, not as a FastAPI
background job. Justification: backups must survive an app crash; if a
backup job ran in-process and the app OOM'd, you'd lose the only thing
that could recover it. The script uses `docker exec` to reach `pg_dump`
inside the db container, so it doesn't need a Postgres install on the
host.

## Prerequisites on the host

```bash
sudo apt install awscli       # script invokes `aws s3 cp`
# Verify the dcinv-db container is reachable:
docker exec dcinv-db pg_dump --version
```

Mount or create the local staging directory + give it to the cron user:

```bash
sudo mkdir -p /var/lib/dcinv-backups
sudo chown -R "$USER:$USER" /var/lib/dcinv-backups
```

## S3 bucket

Any S3-compatible target works (real AWS, MinIO, Yandex Object Storage,
Backblaze, etc). The script reads `BACKUP_S3_ENDPOINT_URL` for non-AWS
providers.

Recommended bucket lifecycle:

- Daily backups for 30 days (`/dcinv-YYYYMMDDTHHMMSSZ.dump`)
- Then transition to Glacier / coldline for 90 days
- Then delete

Configure this once via the provider's console; the script doesn't try
to manage retention server-side.

## Environment file

Drop these in `/etc/default/dcinv-backup` (or set per-cron-entry):

```bash
POSTGRES_USER=dcinv
POSTGRES_PASSWORD=<from your .env>
POSTGRES_DB=dcinv
POSTGRES_CONTAINER=dcinv-db
BACKUP_S3_BUCKET=s3://dcinv-backups
# BACKUP_S3_ENDPOINT_URL=https://storage.yandexcloud.net  # only if non-AWS
AWS_ACCESS_KEY_ID=<from your storage provider>
AWS_SECRET_ACCESS_KEY=<from your storage provider>
BACKUP_LOCAL_DIR=/var/lib/dcinv-backups
DCINV_BACKUP_MARKER_PATH=/var/lib/dcinv-backups/last-success-marker
```

The marker path must also be readable by the FastAPI app container so
`/health.backups` can stat its mtime. Easiest: mount
`/var/lib/dcinv-backups:/var/lib/dcinv-backups:ro` into the
`dcinv-backend` service in `docker-compose.yml` and set
`DCINV_BACKUP_MARKER_PATH` in the backend's environment to the same
path.

## Cron entry

```cron
# daily at 03:00 UTC; log to /var/log/dcinv-backup.log
0 3 * * * . /etc/default/dcinv-backup && /opt/dcinv/scripts/backup.sh >>/var/log/dcinv-backup.log 2>&1
```

Verify with:

```bash
sudo systemctl status cron       # cron itself is running
crontab -l                       # the entry is present
sudo /opt/dcinv/scripts/backup.sh # run once manually to confirm S3 perms
```

## Restoring

```bash
# 1. Stop the app (writes during restore = corruption)
docker compose stop dcinv-backend

# 2. Pick a dump from S3
aws s3 ls s3://dcinv-backups/ | tail -5
# dcinv-20260608T030000Z.dump

# 3. Run restore — drops + recreates, prompts for "restore" confirmation
sudo . /etc/default/dcinv-backup
sudo /opt/dcinv/scripts/restore.sh dcinv-20260608T030000Z.dump

# 4. Start the app
docker compose start dcinv-backend

# 5. Smoke-test: dashboard counters should match the dump time
curl https://qr-dc.t-cloud.kz/health | jq .backups
```

## Monitoring

`GET /health` includes a `backups` sub-object:

```json
{
  "backups": {
    "configured": true,
    "last_completed_at": "2026-06-08T03:00:14+00:00",
    "age_seconds": 86400
  }
}
```

Fields:
- `configured: false` — the marker path env var isn't set; this
  deployment has no backup cron. **Set up cron before declaring
  production ready.**
- `configured: true, last_completed_at: null, age_seconds: null` —
  marker path is set but the file doesn't exist yet. Either cron hasn't
  run, or every attempt so far has failed.
- `configured: true, age_seconds: <int>` — last successful run was N
  seconds ago.

The field is **informational only** — `/health` doesn't flip to `503`
on stale backups (mirrors the NetBox circuit pattern from Sprint 8a).
The application can't know whether 30h is acceptable for this
deployment. Alert on `age_seconds > <your threshold>` from an external
monitor (Grafana, Prometheus alert manager, etc.).

Recommended alert thresholds (adjust to your RPO):

| `age_seconds` | Severity |
|---|---|
| > 30 hours (108000) | warning — yesterday's cron run was late or skipped |
| > 50 hours (180000) | page someone — 2+ days without backup |

## What's NOT in scope

- **Point-in-time recovery (WAL archiving)**. `pg_dump --format=custom`
  + daily cron gives you 24-hour RPO. WAL archiving for sub-second RPO
  is a Sprint 10+ item and needs a different operational story (WAL-G,
  pgbackrest).
- **Restore validation cron**. Re-pulling the last dump weekly into a
  scratch postgres and running `pg_restore --schema-only` would catch
  silent corruption. Sprint 10+.
- **Automated S3 lifecycle**. Configure via the provider's console;
  the script doesn't manage retention server-side.
