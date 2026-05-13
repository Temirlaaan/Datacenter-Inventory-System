"""Alembic migration environment — async engine via asyncpg.

DB URL is loaded from app.config (Settings) so the same source of truth drives
both runtime and migrations. Online mode uses the async engine; offline mode keeps
the sync code path for `alembic upgrade --sql` rendering.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.models import Base

config = context.config

# Set up Python logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime DB URL. Override the placeholder so async_engine_from_config picks it up.
config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without a DB connection — used by `alembic upgrade --sql`."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect via the async engine, then run migrations on a sync-bridged connection."""
    section: dict[str, Any] = config.get_section(config.config_ini_section, {})
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        # NullPool: migrations are single-shot; no need to keep connections around.
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
