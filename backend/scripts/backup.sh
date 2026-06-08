#!/usr/bin/env bash
#
# backup.sh — Sprint 9 Task 3.
#
# Daily pg_dump → S3 cron driver for the dcinv-db container.
#
# Runs OUTSIDE the FastAPI app process (decision H — backups must run
# even when the app is down). Designed for host-cron invocation:
#
#   0 3 * * * /opt/dcinv/scripts/backup.sh >>/var/log/dcinv-backup.log 2>&1
#
# Environment variables consumed (set in /etc/default/dcinv-backup or
# the cron entry's environment):
#
#   POSTGRES_USER             dcinv-db role with permission to dump
#   POSTGRES_PASSWORD         password (passed via .pgpass-style env)
#   POSTGRES_DB               database name to dump (default: dcinv)
#   POSTGRES_CONTAINER        docker container name (default: dcinv-db)
#   BACKUP_S3_BUCKET          target bucket (e.g. s3://dcinv-backups)
#   BACKUP_S3_ENDPOINT_URL    optional, for MinIO / Yandex Object Storage
#   AWS_ACCESS_KEY_ID         IAM access key for the bucket
#   AWS_SECRET_ACCESS_KEY     IAM secret key
#   BACKUP_LOCAL_DIR          local staging dir (default: /var/lib/dcinv-backups)
#   DCINV_BACKUP_MARKER_PATH  touched on success — /health backup sub-object
#                             reads its mtime (default: <local_dir>/last-success-marker)
#
# On success: writes dcinv-<UTC timestamp>.dump to BACKUP_LOCAL_DIR,
# uploads via `aws s3 cp` to BACKUP_S3_BUCKET, then touches the
# marker file. Exits 0.
#
# On failure: leaves the local file (don't delete partial dumps),
# does NOT touch the marker. /health.backups.age_seconds will keep
# growing until the next successful run.

set -euo pipefail

POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER is required}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
POSTGRES_DB="${POSTGRES_DB:-dcinv}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-dcinv-db}"
BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET is required}"
BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-/var/lib/dcinv-backups}"
MARKER_PATH="${DCINV_BACKUP_MARKER_PATH:-$BACKUP_LOCAL_DIR/last-success-marker}"

mkdir -p "$BACKUP_LOCAL_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="$BACKUP_LOCAL_DIR/dcinv-$timestamp.dump"

echo "[$(date -Iseconds)] starting pg_dump → $dump_file"

# pg_dump runs *inside* the container so it speaks the same socket as
# the running database. --format=custom is compressible + supports
# selective restore via pg_restore.
docker exec -e "PGPASSWORD=$POSTGRES_PASSWORD" "$POSTGRES_CONTAINER" \
    pg_dump --username="$POSTGRES_USER" --format=custom --dbname="$POSTGRES_DB" \
    >"$dump_file"

echo "[$(date -Iseconds)] dump complete ($(stat -c '%s' "$dump_file") bytes); uploading"

aws_args=()
if [[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ]]; then
    aws_args+=(--endpoint-url "$BACKUP_S3_ENDPOINT_URL")
fi
aws "${aws_args[@]}" s3 cp "$dump_file" "$BACKUP_S3_BUCKET/dcinv-$timestamp.dump"

echo "[$(date -Iseconds)] upload complete"

# Touch the marker only after both pg_dump AND s3 upload succeed.
# /health.backups.last_completed_at reads this mtime.
touch "$MARKER_PATH"

# Local retention: keep the last 3 dumps on disk; older ones are in S3
# only. Adjust to the host's disk budget. S3 retention is a separate
# bucket-lifecycle policy.
find "$BACKUP_LOCAL_DIR" -maxdepth 1 -name 'dcinv-*.dump' -type f \
    -printf '%T@ %p\n' \
    | sort -rn \
    | tail -n +4 \
    | cut -d' ' -f2- \
    | xargs -r rm --

echo "[$(date -Iseconds)] backup OK"
