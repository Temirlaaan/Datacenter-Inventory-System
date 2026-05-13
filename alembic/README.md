# Alembic migrations — DC Inventory backend

Migrations live here. The DB URL is loaded from `app.config.Settings.database_url`
at runtime — do not put it in `alembic.ini`.

## Destructive migrations policy (CLAUDE.md cross-cutting #7)

The container entrypoint runs `alembic upgrade head` on startup (see Sprint 1 Task 7).
A single release that drops a column would block rollback to the previous version,
because the old code still expects the column to exist.

**Always split destructive changes across two releases:**

1. **Release N** — stop using the column/table in code (no reads, no writes). Ship.
   Verify it is stable in production for at least one deploy cycle.
2. **Release N+1** — drop it.

This keeps every release individually rollback-safe.

Examples of destructive migrations:
- `DROP COLUMN`, `DROP TABLE`, `DROP INDEX` (when an active query depends on it)
- `RENAME COLUMN`/`RENAME TABLE` — split into add-new + dual-write + drop-old
- Type changes that lose precision (`numeric(10,2)` → `integer`, `text` → `varchar(50)`)

## Common commands

```bash
# Apply all pending migrations
uv run alembic upgrade head

# Empty migration template (use when writing migration code by hand)
uv run alembic revision -m "short description"

# Autogenerate from current model state vs current DB schema
uv run alembic revision --autogenerate -m "short description"

# Roll back the last migration (only safe if it was non-destructive)
uv run alembic downgrade -1

# Show migration graph
uv run alembic history

# Render upgrade SQL without touching the DB (CI sanity check)
uv run alembic upgrade head --sql > /tmp/migration.sql
```

## Local Postgres for migration testing

```bash
docker compose -f docker-compose.test.yml up -d
# wait ~3 seconds for Postgres to accept connections
uv run alembic upgrade head
```

## Async configuration

`env.py` uses `async_engine_from_config` + `connection.run_sync()` because all our
runtime code is async (CLAUDE.md stack constraint). Offline mode (`alembic upgrade
--sql`) keeps the sync code path because it never opens a connection.
