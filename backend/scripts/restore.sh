#!/usr/bin/env bash
#
# restore.sh — Sprint 9 Task 3.
#
# Restore a dcinv-db backup from S3. Inverse of backup.sh.
#
# Usage:
#   ./restore.sh dcinv-20260608T030000Z.dump
#
# DESTRUCTIVE — restoring drops + recreates the target database. Run
# only when the app is stopped (docker compose stop dcinv-backend)
# and you've taken a fresh "before" backup of whatever state you're
# overwriting.
#
# Required env (same set as backup.sh):
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_CONTAINER
#   BACKUP_S3_BUCKET, BACKUP_S3_ENDPOINT_URL (optional),
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
#   BACKUP_LOCAL_DIR (default: /var/lib/dcinv-backups)

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <dump-file-name>" >&2
    exit 2
fi

DUMP_NAME="$1"
POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER is required}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
POSTGRES_DB="${POSTGRES_DB:-dcinv}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-dcinv-db}"
BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET is required}"
BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-/var/lib/dcinv-backups}"

mkdir -p "$BACKUP_LOCAL_DIR"
LOCAL_PATH="$BACKUP_LOCAL_DIR/$DUMP_NAME"

if [[ ! -f "$LOCAL_PATH" ]]; then
    echo "[$(date -Iseconds)] fetching $DUMP_NAME from S3"
    aws_args=()
    if [[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ]]; then
        aws_args+=(--endpoint-url "$BACKUP_S3_ENDPOINT_URL")
    fi
    aws "${aws_args[@]}" s3 cp "$BACKUP_S3_BUCKET/$DUMP_NAME" "$LOCAL_PATH"
fi

echo
echo "About to drop and recreate database '$POSTGRES_DB' on container"
echo "'$POSTGRES_CONTAINER', then restore from $LOCAL_PATH."
echo "All current data in $POSTGRES_DB will be lost."
read -r -p "Type 'restore' to proceed: " confirm
if [[ "$confirm" != "restore" ]]; then
    echo "aborted" >&2
    exit 1
fi

echo "[$(date -Iseconds)] dropping and recreating $POSTGRES_DB"
docker exec -e "PGPASSWORD=$POSTGRES_PASSWORD" "$POSTGRES_CONTAINER" \
    psql --username="$POSTGRES_USER" --dbname=postgres \
    -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\";"
docker exec -e "PGPASSWORD=$POSTGRES_PASSWORD" "$POSTGRES_CONTAINER" \
    psql --username="$POSTGRES_USER" --dbname=postgres \
    -c "CREATE DATABASE \"$POSTGRES_DB\";"

echo "[$(date -Iseconds)] restoring from $LOCAL_PATH"
docker exec -i -e "PGPASSWORD=$POSTGRES_PASSWORD" "$POSTGRES_CONTAINER" \
    pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
    --clean --if-exists --no-owner --no-acl \
    <"$LOCAL_PATH"

echo "[$(date -Iseconds)] restore complete; restart dcinv-backend now"
