#!/usr/bin/env bash
#
# restore_validate.sh — Sprint 10 Task 0.
#
# Weekly proof that the latest backup can actually be restored. Without
# this, the only validation of backup.sh is "the file landed in S3";
# silent corruption (truncated upload, bit rot in the archive, NetBox
# schema-version drift) goes undetected until you need the backup.
#
# Strategy (decision B): NEVER touch the production database. Spin up an
# EPHEMERAL postgres:15 container on tmpfs, restore the latest dump
# schema-only into it, check the exit code, tear it all down. Schema-only
# (not full data) because:
#
# - We're validating the dump's archive integrity + the schema's
#   restorability, not the data.
# - Schema restore completes in seconds even for production-sized dumps.
# - Avoids needing tens of GB of tmpfs.
#
# Runs OUTSIDE the FastAPI app process. Designed for weekly host-cron:
#
#   0 4 * * 0 /opt/dcinv/scripts/restore_validate.sh \
#              >>/var/log/dcinv-restore-validate.log 2>&1
#
# Sundays at 04:00 UTC = after Saturday night's backup.
#
# Environment variables (same /etc/default/dcinv-backup file):
#   BACKUP_S3_BUCKET, BACKUP_S3_ENDPOINT_URL (optional),
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
#   POSTGRES_USER (the dump uses this as the role to OWN objects)
#   DCINV_RESTORE_MARKER_PATH (default: /var/lib/dcinv-backups/last-restore-validate)

set -euo pipefail

BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET is required}"
POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER is required}"
MARKER_PATH="${DCINV_RESTORE_MARKER_PATH:-/var/lib/dcinv-backups/last-restore-validate}"
SCRATCH_PG_IMAGE="${SCRATCH_PG_IMAGE:-postgres:15}"

aws_args=()
if [[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ]]; then
    aws_args+=(--endpoint-url "$BACKUP_S3_ENDPOINT_URL")
fi

echo "[$(date -Iseconds)] finding latest dump in $BACKUP_S3_BUCKET"
latest="$(aws "${aws_args[@]}" s3 ls "$BACKUP_S3_BUCKET/" \
    | awk '{print $NF}' \
    | grep -E '^dcinv-[0-9TZ]+\.dump$' \
    | sort \
    | tail -1)"
if [[ -z "$latest" ]]; then
    echo "[$(date -Iseconds)] FAILED: no dumps found in $BACKUP_S3_BUCKET" >&2
    exit 1
fi
echo "[$(date -Iseconds)] selected $latest"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
scratch="$(mktemp -d -t dcinv-restore-validate-XXXXXX)"
trap 'rm -rf "$scratch"' EXIT

local_dump="$scratch/$latest"
echo "[$(date -Iseconds)] downloading to $local_dump"
aws "${aws_args[@]}" s3 cp "$BACKUP_S3_BUCKET/$latest" "$local_dump"

container="dcinv-restore-validate-$ts"
scratch_password="$(openssl rand -hex 16)"

echo "[$(date -Iseconds)] starting ephemeral $SCRATCH_PG_IMAGE container ($container)"
docker run --rm -d \
    --name "$container" \
    --tmpfs /var/lib/postgresql/data:rw,size=2G \
    -e "POSTGRES_USER=$POSTGRES_USER" \
    -e "POSTGRES_PASSWORD=$scratch_password" \
    -e POSTGRES_DB=dcinv \
    "$SCRATCH_PG_IMAGE" >/dev/null

cleanup_container() {
    docker stop "$container" >/dev/null 2>&1 || true
}
trap 'cleanup_container; rm -rf "$scratch"' EXIT

# Wait for postgres to be ready (max ~30s).
for _ in {1..30}; do
    if docker exec -e "PGPASSWORD=$scratch_password" "$container" \
        pg_isready --username="$POSTGRES_USER" --dbname=dcinv --quiet; then
        break
    fi
    sleep 1
done

echo "[$(date -Iseconds)] running pg_restore --schema-only"
# --no-owner / --no-acl so the dump's role assumptions don't fight
# the ephemeral container's user setup.
docker exec -i -e "PGPASSWORD=$scratch_password" "$container" \
    pg_restore --username="$POSTGRES_USER" --dbname=dcinv \
    --schema-only --no-owner --no-acl \
    <"$local_dump"

# Spot-check: pg_dump archives are valid even when empty; confirm the
# restored schema has at least one of our expected tables.
table_count="$(docker exec -e "PGPASSWORD=$scratch_password" "$container" \
    psql --username="$POSTGRES_USER" --dbname=dcinv --tuples-only --no-align \
    -c "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN ('qr_codes','qr_batches','audit_log','shift_sessions','idempotency_keys');")"
table_count="${table_count//[$'\t\r\n ']/}"
if [[ "$table_count" -lt 5 ]]; then
    echo "[$(date -Iseconds)] FAILED: expected 5 dcinv tables, found $table_count" >&2
    exit 1
fi

# Marker only after BOTH pg_restore exit-0 AND table-count check pass.
mkdir -p "$(dirname "$MARKER_PATH")"
touch "$MARKER_PATH"

echo "[$(date -Iseconds)] restore validation OK ($table_count/5 dcinv tables restored)"
