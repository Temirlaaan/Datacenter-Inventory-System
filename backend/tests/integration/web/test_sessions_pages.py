"""Integration tests for the ``/web/sessions/`` page + inline force-close form
(Sprint 8b Task 4).

Drives the full ASGI stack with a valid session cookie. Task 0's OIDC flow
suite covers auth-failure modes; the focus here is the list-page rendering
(active rows show the form, ended rows show end_reason chip), the
filter-form + pagination, and the force-close POST round-trip (redirect +
flash + DB state mutation + audit row).
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
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.shift_session import ShiftEndReason, ShiftSession
from app.main import app
from app.web.auth import (
    SESSION_COOKIE_NAME,
    build_session_cookie_payload,
    encode_session_cookie,
    reset_web_auth_cache,
)
from tests.integration.conftest import (
    DEFAULT_SHIFT_SESSION_ID,
    DEFAULT_USER_KEYCLOAK_ID,
    seed_default_active_shift,
)

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[3]
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
        await session.execute(text("TRUNCATE shift_sessions, audit_log CASCADE"))
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
        sub=DEFAULT_USER_KEYCLOAK_ID, email="alice@example.com", roles=("dcinv-admin",)
    )
    return encode_session_cookie(user)


async def _seed_target_shift(*, active: bool, user_id: UUID | None = None) -> UUID:
    """Insert a second shift (NOT the admin's own) so force-close has a
    distinct target. ``user_id`` defaults to a fresh UUID to avoid colliding
    with the partial-unique-index 'one active per user'."""
    target_id = uuid4()
    actual_user = user_id or uuid4()
    shift = ShiftSession(
        id=target_id,
        user_email="bob@example.com",
        user_keycloak_id=actual_user,
        shift_start_at=_NOW - timedelta(hours=3),
        shift_end_at=None if active else _NOW - timedelta(hours=1),
        tablet_id="tablet-bob",
        end_reason=None if active else ShiftEndReason.MANUAL,
    )
    async with get_sessionmaker()() as session:
        await ShiftSessionRepository(session).insert(shift)
        await session.commit()
    return target_id


# ---------- /web/sessions/ list ---------------------------------------------


async def test_sessions_list_renders_admin_shift_with_active_state_and_filter_form(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/sessions/")
    assert resp.status_code == 200, resp.text
    # The default-seeded admin shift surfaces.
    assert "alice@example.com" in resp.text
    assert "active" in resp.text
    # Filter form fields all rendered.
    for name in (
        'name="user_keycloak_id"',
        'name="from"',
        'name="to"',
        'name="active_only"',
    ):
        assert name in resp.text


async def test_sessions_list_renders_force_close_form_on_active_rows_only(
    client: httpx.AsyncClient,
) -> None:
    """Active rows show <form action='/web/sessions/{id}/force-close'>;
    ended rows do NOT."""
    target_id = await _seed_target_shift(active=False)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/sessions/")
    assert resp.status_code == 200
    # No force-close form for the ENDED row.
    assert f"/web/sessions/{target_id}/force-close" not in resp.text
    # The default admin shift IS active → it does have a form.
    assert f"/web/sessions/{DEFAULT_SHIFT_SESSION_ID}/force-close" in resp.text


async def test_sessions_list_shows_end_reason_badge_for_ended_shifts(
    client: httpx.AsyncClient,
) -> None:
    await _seed_target_shift(active=False)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/sessions/")
    assert resp.status_code == 200
    assert "end-reason-badge--manual" in resp.text
    assert "bob@example.com" in resp.text


async def test_sessions_list_active_only_filter_narrows_results(
    client: httpx.AsyncClient,
) -> None:
    await _seed_target_shift(active=False)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/sessions/?active_only=true")
    assert resp.status_code == 200
    # The ended bob shift drops out; only the admin's active shift remains.
    assert "bob@example.com" not in resp.text
    assert "alice@example.com" in resp.text


async def test_sessions_list_shows_flash_message_when_query_param_present(
    client: httpx.AsyncClient,
) -> None:
    """Post-force-close redirects pass ``?flash=...`` so the operator sees
    confirmation. _base.html's flash slot reads from context."""
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/sessions/?flash=Shift+force-closed&flash_kind=info")
    assert resp.status_code == 200
    assert "Shift force-closed" in resp.text
    assert "flash-info" in resp.text


async def test_sessions_list_pagination_carries_filter_query_string(
    client: httpx.AsyncClient,
) -> None:
    """21 ended shifts → page 1 has a Next link that preserves active_only."""
    for _ in range(21):
        await _seed_target_shift(active=False)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/sessions/?active_only=true")
    assert resp.status_code == 200
    # active_only=true means only 1 row remains (the admin's), so no Next.
    # Switch off the filter to actually paginate.
    resp_all = await client.get("/web/sessions/")
    assert resp_all.status_code == 200
    assert "/web/sessions/?page=2" in resp_all.text


# ---------- POST /web/sessions/{id}/force-close ------------------------------


async def test_force_close_post_redirects_with_success_flash_and_ends_target(
    client: httpx.AsyncClient,
) -> None:
    target_id = await _seed_target_shift(active=True)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())

    resp = await client.post(
        f"/web/sessions/{target_id}/force-close",
        data={"reason": "operator stepped away without ending the shift"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/web/sessions/?")
    assert "flash=Shift+force-closed" in location
    assert "flash_kind=info" in location

    # Target shift is now ended with end_reason=forced.
    async with get_sessionmaker()() as session:
        row = await session.execute(
            text("SELECT shift_end_at, end_reason::text FROM shift_sessions WHERE id = :id"),
            {"id": str(target_id)},
        )
        end_at, reason = row.one()
    assert end_at is not None
    assert reason == "forced"


async def test_force_close_post_writes_audit_row_attributed_to_admin_shift(
    client: httpx.AsyncClient,
) -> None:
    """Audit row's session_id = the admin's own shift (NOT the target's).
    Sprint 6 decision: audit attribution is the actor's shift."""
    target_id = await _seed_target_shift(active=True)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())

    await client.post(
        f"/web/sessions/{target_id}/force-close",
        data={"reason": "test attribution"},
        follow_redirects=False,
    )

    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT session_id, after_json, result::text"
                " FROM audit_log WHERE operation = 'shift_session.force_close'"
                " AND entity_id = :eid"
            ),
            {"eid": str(target_id)},
        )
        records = rows.fetchall()
    assert len(records) == 1
    sess, after, result = records[0]
    assert sess == DEFAULT_SHIFT_SESSION_ID
    assert after["reason"] == "test attribution"
    assert after["end_reason"] == "forced"
    assert result == "success"


async def test_force_close_post_unknown_id_redirects_with_error_flash(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.post(
        f"/web/sessions/{uuid4()}/force-close",
        data={"reason": "this shift doesn't exist"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "flash=Shift+not+found" in location
    assert "flash_kind=error" in location


async def test_force_close_post_idempotent_on_already_ended_target(
    client: httpx.AsyncClient,
) -> None:
    """Sprint 7 contract: force-closing an already-ended shift returns 200
    with a CONFLICT audit row. The web layer treats that as success — the
    operator sees the same success flash, the JSON layer's audit row
    captures the no-op."""
    target_id = await _seed_target_shift(active=False)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())

    resp = await client.post(
        f"/web/sessions/{target_id}/force-close",
        data={"reason": "trying anyway"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "flash=Shift+force-closed" in resp.headers["location"]

    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT result::text, (after_json->>'no_op')::text"
                " FROM audit_log WHERE operation = 'shift_session.force_close'"
                " AND entity_id = :eid"
            ),
            {"eid": str(target_id)},
        )
        records = rows.fetchall()
    assert len(records) == 1
    result, no_op = records[0]
    assert result == "conflict"
    assert no_op == "true"


async def test_force_close_post_missing_reason_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """FastAPI's Form(min_length=1) rejects an empty / missing reason."""
    target_id = await _seed_target_shift(active=True)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.post(
        f"/web/sessions/{target_id}/force-close",
        data={"reason": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 422


async def test_force_close_post_preserves_filter_qs_in_redirect(
    client: httpx.AsyncClient,
) -> None:
    """Hidden inputs on the form echo the filter context so the operator
    lands back on the same filtered view after the redirect."""
    target_id = await _seed_target_shift(active=True)
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())

    resp = await client.post(
        f"/web/sessions/{target_id}/force-close",
        data={
            "reason": "with filter context",
            "active_only": "true",
            "user_keycloak_id": "11111111-1111-1111-1111-111111111111",
            "from": "",
            "to": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "active_only=true" in location
    assert "user_keycloak_id=11111111" in location


# ---------- auth smoke (regression guard) -----------------------------------


async def test_sessions_list_redirects_to_login_without_cookie(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/web/sessions/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/web/login" in resp.headers["location"]
