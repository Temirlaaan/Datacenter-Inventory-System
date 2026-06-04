"""Integration tests for the ``/web/audit/`` list + detail pages
(Sprint 8b Task 3).

Drives the full ASGI stack with a valid session cookie. Task 0's OIDC flow
suite covers the auth-failure modes (no cookie / no shift); the focus here
is page-specific rendering: filter form preserves submitted values,
pagination + CSV-download links carry the filter context, detail page
pretty-prints JSON, unknown id → custom 404.
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

from app.config import get_settings
from app.db.repositories.audit_log import AuditLogQueryFilters, AuditLogRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from app.main import app
from app.web.auth import (
    SESSION_COOKIE_NAME,
    build_session_cookie_payload,
    encode_session_cookie,
    reset_web_auth_cache,
)
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[3]
_USER_SUB = UUID("11111111-1111-1111-1111-111111111111")
_FERNET_KEY = "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo="
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


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
async def _setup(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None, None]:
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_ID", "dcinv-web")
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "test-web-client-secret")
    monkeypatch.setenv("SESSION_COOKIE_KEY", _FERNET_KEY)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_web_auth_cache()
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
    reset_web_auth_cache()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _valid_session_cookie() -> str:
    user = build_session_cookie_payload(
        sub=_USER_SUB, email="alice@example.com", roles=("dcinv-admin",)
    )
    return encode_session_cookie(user)


def _seed_entry(
    *,
    timestamp: datetime,
    entity_type: str = "qr",
    entity_id: str = "DCQR-0001",
    operation: str = "qr.bind",
    result: AuditResult = AuditResult.SUCCESS,
    before_json: dict | None = None,
    after_json: dict | None = None,
) -> AuditLogEntry:
    return AuditLogEntry(
        request_id=uuid4(),
        timestamp=timestamp,
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        session_id=None,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=before_json or {},
        after_json=after_json or {"k": "v"},
        result=result,
    )


# ---------- /web/audit/ list -------------------------------------------------


async def test_audit_list_renders_seeded_row_with_filter_form_and_csv_link(
    client: httpx.AsyncClient,
) -> None:
    async with get_sessionmaker()() as session:
        await AuditLogRepository(session).insert(
            _seed_entry(timestamp=_NOW - timedelta(minutes=1), entity_id="DCQR-WEB001")
        )
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/audit/")
    assert resp.status_code == 200, resp.text
    # Row appears.
    assert "DCQR-WEB001" in resp.text
    assert "qr.bind" in resp.text
    # Filter form is rendered with all 8 fields.
    for name in (
        'name="user_keycloak_id"',
        'name="from"',
        'name="to"',
        'name="entity_type"',
        'name="entity_id"',
        'name="operation"',
        'name="session_id"',
        'name="result"',
    ):
        assert name in resp.text
    # Download CSV button is present (without filters yet).
    assert "/api/v1/admin/audit/csv" in resp.text


async def test_audit_list_shows_empty_state_when_no_rows(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/audit/")
    assert resp.status_code == 200
    assert "No audit rows match these filters." in resp.text


async def test_audit_list_filter_form_preserves_submitted_values(
    client: httpx.AsyncClient,
) -> None:
    """Submitting filters re-renders them in the form inputs so the operator
    can tweak instead of re-typing."""
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get(
        "/web/audit/?entity_type=qr&entity_id=DCQR-X&operation=qr.bind&result=success"
    )
    assert resp.status_code == 200
    assert 'value="qr"' in resp.text  # entity_type
    assert 'value="DCQR-X"' in resp.text
    assert 'value="qr.bind"' in resp.text
    # Result select preselects "success".
    assert 'value="success" selected' in resp.text


async def test_audit_list_pagination_and_csv_links_carry_filter_query_string(
    client: httpx.AsyncClient,
) -> None:
    """Pagination + Download CSV must include the operator's filter context
    so the next page (or the CSV) doesn't silently widen the scope."""
    async with get_sessionmaker()() as session:
        repo = AuditLogRepository(session)
        # 21 rows so page_size=20 yields has_more on page 1.
        for i in range(21):
            await repo.insert(
                _seed_entry(
                    timestamp=_NOW - timedelta(minutes=i + 1),
                    entity_id=f"DCQR-P{i:03d}",
                )
            )
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/audit/?entity_type=qr")
    assert resp.status_code == 200
    # Next link carries the filter forward.
    assert "/web/audit/?page=2&amp;entity_type=qr" in resp.text or (
        "/web/audit/?page=2" in resp.text and "entity_type=qr" in resp.text
    )
    # CSV link carries the filter too.
    assert "/api/v1/admin/audit/csv?entity_type=qr" in resp.text


async def test_audit_list_filters_by_operation_narrows_results(
    client: httpx.AsyncClient,
) -> None:
    """Assert via distinct entity_ids — the placeholder text in the filter
    form contains the canonical operation names, so substring-matching the
    operation itself would false-positive."""
    async with get_sessionmaker()() as session:
        repo = AuditLogRepository(session)
        await repo.insert(
            _seed_entry(
                timestamp=_NOW - timedelta(minutes=1),
                operation="qr.bind",
                entity_id="DCQR-MATCH",
            )
        )
        await repo.insert(
            _seed_entry(
                timestamp=_NOW - timedelta(minutes=2),
                operation="device.update",
                entity_id="DCDEV-NOMATCH",
            )
        )
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/audit/?operation=qr.bind")
    assert resp.status_code == 200
    assert "DCQR-MATCH" in resp.text
    assert "DCDEV-NOMATCH" not in resp.text


# ---------- /web/audit/{id} detail ------------------------------------------


async def test_audit_detail_renders_all_columns_and_pretty_prints_json(
    client: httpx.AsyncClient,
) -> None:
    async with get_sessionmaker()() as session:
        repo = AuditLogRepository(session)
        await repo.insert(
            _seed_entry(
                timestamp=_NOW,
                entity_id="DCQR-DETAIL1",
                before_json={"old": 1},
                after_json={"new": 2, "nested": {"k": "v"}},
            )
        )
        await session.commit()
        rows, _ = await repo.query(filters=AuditLogQueryFilters(), page=1, page_size=20)
        seeded_id = rows[0].id
        assert seeded_id is not None

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get(f"/web/audit/{seeded_id}")
    assert resp.status_code == 200, resp.text
    assert "DCQR-DETAIL1" in resp.text
    assert "alice@example.com" in resp.text
    # JSON pretty-printed (indent=2 → newline + spaces).
    assert "&#34;old&#34;" in resp.text or '"old"' in resp.text


async def test_audit_detail_renders_404_for_unknown_id(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/audit/999999999")
    assert resp.status_code == 404
    assert "Not found" in resp.text
    assert "audit row 999999999" in resp.text


# ---------- auth smoke (one regression guard) -------------------------------


async def test_audit_list_redirects_to_login_without_cookie(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/web/audit/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/web/login" in resp.headers["location"]
