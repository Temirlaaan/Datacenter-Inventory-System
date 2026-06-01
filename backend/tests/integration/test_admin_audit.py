"""End-to-end integration tests for GET /api/v1/admin/audit (Sprint 7 Task 2).

The endpoint unit tests in tests/unit/api/v1/test_admin_audit.py already cover
the handler logic, failure path, and routing/gating against the test conftest's
seeded shift. This file exercises a few full-stack flows against a fresh DB
to lock in:
- Pagination across multiple pages (page=1, page=2, has_more transitions)
- The audit-of-audits row is itself queryable (entity_type=audit&entity_id=search)
- Filtering returns only the matching rows
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.repositories.audit_log import AuditLogRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from app.main import app
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_KEYCLOAK_ID = "11111111-1111-1111-1111-111111111111"


def _alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=_BACKEND_DIR,
        timeout=30,
    )
    assert (
        result.returncode == 0
    ), f"alembic {args!r} failed: stdout={result.stdout!r} stderr={result.stderr!r}"


@pytest.fixture(scope="module", autouse=True)
def _clean_schema() -> Generator[None, None, None]:
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


@pytest.fixture(autouse=True)
async def _truncate_and_seed_shift() -> AsyncGenerator[None, None]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE audit_log, shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
async def admin_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """AsyncClient with a canned admin AuthUser injected via dependency override."""
    admin = AuthUser(
        sub=_USER_KEYCLOAK_ID,
        email="alice@example.com",
        roles=("dcinv-admin",),
        session_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _seed_entry(
    *,
    timestamp: datetime,
    entity_type: str = "qr",
    entity_id: str = "DCQR-0001",
    operation: str = "qr.bind",
    result: AuditResult = AuditResult.SUCCESS,
) -> AuditLogEntry:
    return AuditLogEntry(
        request_id=uuid4(),
        timestamp=timestamp,
        user_email="alice@example.com",
        user_keycloak_id=UUID(_USER_KEYCLOAK_ID),
        session_id=None,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json={},
        after_json={},
        result=result,
    )


async def _insert_all(entries: list[AuditLogEntry]) -> None:
    async with get_sessionmaker()() as db:
        repo = AuditLogRepository(db)
        for e in entries:
            await repo.insert(e)
        await db.commit()


async def test_admin_audit_query_walks_pages_with_has_more(
    admin_client: httpx.AsyncClient,
) -> None:
    base = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
    # 25 qr rows spaced 1 minute apart so timestamp ordering is deterministic.
    await _insert_all(
        [
            _seed_entry(
                timestamp=base - timedelta(minutes=i),
                entity_id=f"DCQR-{i:03d}",
            )
            for i in range(25)
        ]
    )

    page1 = await admin_client.get("/api/v1/admin/audit?entity_type=qr&page_size=10&page=1")
    assert page1.status_code == 200
    p1 = page1.json()
    assert len(p1["results"]) == 10
    assert p1["has_more"] is True
    assert p1["page"] == 1

    page2 = await admin_client.get("/api/v1/admin/audit?entity_type=qr&page_size=10&page=2")
    p2 = page2.json()
    assert len(p2["results"]) == 10
    assert p2["has_more"] is True
    assert p2["page"] == 2

    page3 = await admin_client.get("/api/v1/admin/audit?entity_type=qr&page_size=10&page=3")
    p3 = page3.json()
    assert len(p3["results"]) == 5
    assert p3["has_more"] is False

    # All 25 distinct ids across the three pages.
    all_ids = {r["id"] for r in p1["results"] + p2["results"] + p3["results"]}
    assert len(all_ids) == 25


async def test_admin_audit_query_is_itself_queryable_via_entity_type_audit(
    admin_client: httpx.AsyncClient,
) -> None:
    """Decision I: audit-of-audits rows use entity_type='audit',
    entity_id='search' so a follow-up query surfaces them deterministically."""
    # First query writes an audit-of-audits row.
    first = await admin_client.get("/api/v1/admin/audit?entity_type=qr")
    assert first.status_code == 200

    # Second query, filtered to the audit-of-audits namespace, surfaces the first.
    second = await admin_client.get("/api/v1/admin/audit?entity_type=audit&entity_id=search")
    assert second.status_code == 200
    body = second.json()
    # Two rows: the first call's audit-of-audits row + this very call's row.
    # Either way the operation is audit.query.
    assert all(r["operation"] == "audit.query" for r in body["results"])
    assert all(r["entity_type"] == "audit" for r in body["results"])
    assert all(r["entity_id"] == "search" for r in body["results"])
    assert len(body["results"]) >= 1


async def test_admin_audit_query_filter_by_operation_returns_only_matches(
    admin_client: httpx.AsyncClient,
) -> None:
    base = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
    await _insert_all(
        [
            _seed_entry(timestamp=base - timedelta(minutes=1), operation="qr.bind"),
            _seed_entry(timestamp=base - timedelta(minutes=2), operation="qr.retire"),
            _seed_entry(timestamp=base - timedelta(minutes=3), operation="device.update"),
        ]
    )

    resp = await admin_client.get("/api/v1/admin/audit?operation=qr.bind")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["operation"] == "qr.bind"
