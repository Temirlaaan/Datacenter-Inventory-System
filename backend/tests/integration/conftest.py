"""Integration test fixtures — gate on real Postgres availability."""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_REQUIRED = (
    "NETBOX_URL",
    "NETBOX_SERVICE_TOKEN",
    "KEYCLOAK_BASE_URL",
    "DATABASE_URL",
)

# Sprint 6 Task 4: canonical "default-test-user" identity + a deterministic
# active-shift UUID. Used by per-file _truncate fixtures to seed an active
# shift before each write-endpoint test so the dep-layer
# ``require_role_with_active_shift`` lookup succeeds.
DEFAULT_USER_KEYCLOAK_ID = UUID("11111111-1111-1111-1111-111111111111")
DEFAULT_SHIFT_SESSION_ID = UUID("33333333-3333-3333-3333-333333333333")


async def seed_default_active_shift(session: AsyncSession) -> UUID:
    """Insert a canonical active shift for the default test user.

    Returns the shift's UUID so integration tests can assert
    ``audit_log.session_id == DEFAULT_SHIFT_SESSION_ID`` for writes made by
    the default user. Caller commits.
    """
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO shift_sessions"
            " (id, user_email, user_keycloak_id, shift_start_at, tablet_id)"
            " VALUES (:sid, 'alice@example.com', :uid, NOW(), 'test-tablet')"
        ),
        {"sid": str(DEFAULT_SHIFT_SESSION_ID), "uid": str(DEFAULT_USER_KEYCLOAK_ID)},
    )
    return DEFAULT_SHIFT_SESSION_ID


@pytest.fixture(autouse=True)
def _require_integration_env() -> None:
    """Skip integration tests unless Postgres + app env vars are set.

    Local: `docker compose -f docker-compose.test.yml up -d` then export DATABASE_URL=
    postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test plus the
    other required vars. CI sets these in the workflow (Sprint 1 Task 8).
    """
    missing = [key for key in _REQUIRED if not os.getenv(key)]
    if missing:
        pytest.skip(f"integration env vars missing: {', '.join(missing)}")
