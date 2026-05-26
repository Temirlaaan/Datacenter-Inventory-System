#!/usr/bin/env bash
# Container entrypoint: run pending migrations, then exec the CMD (uvicorn).
#
# `set -e` aborts on migration failure — serving traffic against the wrong schema
# would corrupt data. CLAUDE.md cross-cutting #7 requires destructive migrations to
# be split across two releases so this auto-upgrade stays rollback-safe.

set -euo pipefail

echo "entrypoint: running alembic upgrade head"
alembic upgrade head

echo "entrypoint: starting application"
exec "$@"
