"""Integration test fixtures — gate on real Postgres availability."""

from __future__ import annotations

import os

import pytest

_REQUIRED = (
    "NETBOX_URL",
    "NETBOX_SERVICE_TOKEN",
    "KEYCLOAK_BASE_URL",
    "DATABASE_URL",
)


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
