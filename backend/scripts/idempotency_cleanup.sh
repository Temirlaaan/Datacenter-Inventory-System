#!/usr/bin/env bash
#
# idempotency_cleanup.sh — Sprint 10 Task 0.
#
# Daily sweep of stale rows from idempotency_keys. The table grows by
# ~1 row per mobile write; without a TTL cleanup it grows unbounded.
#
# Replay is only meaningful within a mobile client's retry window —
# at most a few minutes. The 24h TTL is generous (handles offline-queue
# pile-up after extended wifi outages) while keeping the table bounded.
#
# Runs OUTSIDE the FastAPI app process (decision A — same justification
# as backup.sh). Designed for host-cron invocation:
#
#   30 3 * * * /opt/dcinv/scripts/idempotency_cleanup.sh \
#               >>/var/log/dcinv-idempotency-cleanup.log 2>&1
#
# 03:30 UTC = 30 min after backup.sh at 03:00, so the deleted rows
# are already preserved in the day's dump.
#
# Environment variables (set in /etc/default/dcinv-backup, same file
# the backup script reads):
#
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_CONTAINER
#
# (No S3 vars needed — this script never touches S3.)

set -euo pipefail

POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER is required}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
POSTGRES_DB="${POSTGRES_DB:-dcinv}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-dcinv-db}"
TTL_INTERVAL="${IDEMPOTENCY_TTL_INTERVAL:-24 hours}"

echo "[$(date -Iseconds)] sweeping idempotency_keys older than $TTL_INTERVAL"

# RETURNING gives us a row count we can log. The created_at index
# (Sprint 5 migration) makes the WHERE predicate cheap.
result="$(docker exec -e "PGPASSWORD=$POSTGRES_PASSWORD" "$POSTGRES_CONTAINER" \
    psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align <<SQL
WITH deleted AS (
    DELETE FROM idempotency_keys
    WHERE created_at < NOW() - INTERVAL '$TTL_INTERVAL'
    RETURNING 1
)
SELECT COUNT(*) FROM deleted;
SQL
)"

deleted="${result//[$'\t\r\n ']/}"
echo "[$(date -Iseconds)] cleanup OK; deleted $deleted rows"
