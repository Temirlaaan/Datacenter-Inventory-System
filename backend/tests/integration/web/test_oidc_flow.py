"""End-to-end OIDC flow tests (Sprint 8b Task 0).

Drives the full ASGI stack via httpx.AsyncClient. Keycloak's token endpoint
is mocked via respx; we construct a fake id_token (unverified signature —
the callback handler trusts its own freshly-completed handshake per decision)
and assert the callback sets the encrypted session cookie correctly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx
from jose import jwt
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
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
        await session.execute(text("TRUNCATE shift_sessions CASCADE"))
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
    """Mint a fresh session cookie for the default user."""
    user = build_session_cookie_payload(
        sub=_USER_SUB, email="alice@example.com", roles=("dcinv-admin",)
    )
    return encode_session_cookie(user)


# ---------- /web/login -------------------------------------------------------


async def test_login_redirects_to_keycloak_with_state_and_nonce(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/web/login", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/realms/" in location  # Keycloak auth URL shape
    assert "response_type=code" in location
    assert "state=" in location
    assert "nonce=" in location
    # state + nonce + next cookies were set so the callback can verify them.
    assert "__dcinv_oidc_state" in resp.cookies
    assert "__dcinv_oidc_nonce" in resp.cookies


async def test_login_preserves_next_path_in_cookie(client: httpx.AsyncClient) -> None:
    resp = await client.get("/web/login?next=%2Fweb%2Fbatches%2F", follow_redirects=False)
    # Starlette wraps cookie values containing slashes in double quotes per
    # RFC 6265; strip them so the assertion targets the actual path.
    assert resp.cookies["__dcinv_oidc_next"].strip('"') == "/web/batches/"


# ---------- /web/oidc/callback ----------------------------------------------


async def test_oidc_callback_rejects_state_mismatch(client: httpx.AsyncClient) -> None:
    """CSRF guard: ?state= must match the __dcinv_oidc_state cookie."""
    client.cookies.set("__dcinv_oidc_state", "expected-state")
    resp = await client.get(
        "/web/oidc/callback?code=fake-code&state=different-state",
        follow_redirects=False,
    )
    assert resp.status_code == 400


async def test_oidc_callback_exchanges_code_and_sets_session_cookie(
    client: httpx.AsyncClient,
) -> None:
    """Happy path: state matches, Keycloak returns id_token, callback sets
    the encrypted session cookie and 302s to `next`."""
    state = "test-state-token"
    nonce = "test-nonce-token"
    client.cookies.set("__dcinv_oidc_state", state)
    client.cookies.set("__dcinv_oidc_nonce", nonce)
    client.cookies.set("__dcinv_oidc_next", "/web/")

    settings = get_settings()
    token_url = f"{settings.keycloak_issuer}/protocol/openid-connect/token"
    # Construct an id_token whose claims include the nonce + sub + roles.
    id_token = jwt.encode(
        {
            "sub": str(_USER_SUB),
            "email": "alice@example.com",
            "nonce": nonce,
            "realm_access": {"roles": ["dcinv-admin"]},
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        "irrelevant-secret-callback-skips-verification",
        algorithm="HS256",
    )
    with respx.mock(assert_all_called=True) as router:
        router.post(token_url).respond(
            json={"id_token": id_token, "access_token": "ignored", "token_type": "Bearer"}
        )
        resp = await client.get(
            f"/web/oidc/callback?code=fake-auth-code&state={state}",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/web/"
    assert SESSION_COOKIE_NAME in resp.cookies
    # Verify the cookie decrypts to the expected user.
    from app.web.auth import decode_session_cookie

    decoded = decode_session_cookie(resp.cookies[SESSION_COOKIE_NAME])
    assert decoded is not None
    assert decoded.sub == _USER_SUB
    assert "dcinv-admin" in decoded.roles


async def test_oidc_callback_rejects_nonce_mismatch(client: httpx.AsyncClient) -> None:
    """Replay guard: id_token's nonce claim must match the cookie nonce."""
    state = "test-state"
    client.cookies.set("__dcinv_oidc_state", state)
    client.cookies.set("__dcinv_oidc_nonce", "expected-nonce")

    settings = get_settings()
    token_url = f"{settings.keycloak_issuer}/protocol/openid-connect/token"
    id_token = jwt.encode(
        {
            "sub": str(_USER_SUB),
            "email": "alice@example.com",
            "nonce": "WRONG-nonce-token",
            "realm_access": {"roles": ["dcinv-admin"]},
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        "irrelevant",
        algorithm="HS256",
    )
    with respx.mock(assert_all_called=True) as router:
        router.post(token_url).respond(json={"id_token": id_token})
        resp = await client.get(
            f"/web/oidc/callback?code=fake&state={state}", follow_redirects=False
        )

    assert resp.status_code == 400


# ---------- /web/ (placeholder dashboard) -----------------------------------


async def test_web_dashboard_requires_session_cookie_else_redirects_to_login(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/web/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/web/login" in resp.headers["location"]
    assert "next=%2Fweb%2F" in resp.headers["location"]


async def test_web_dashboard_renders_with_valid_session_cookie(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/")
    assert resp.status_code == 200, resp.text
    assert "alice@example.com" in resp.text


async def test_web_dashboard_renders_shift_needed_page_when_no_active_shift(
    client: httpx.AsyncClient,
) -> None:
    """Admin authenticated but no active shift → 403 + intermediate page
    with the 'Start admin shift' form."""
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE shift_sessions CASCADE"))
        await session.commit()
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())

    resp = await client.get("/web/")
    assert resp.status_code == 403
    assert "Open an admin shift" in resp.text
    assert 'action="/api/v1/admin/sessions/start"' in resp.text


# ---------- /web/logout ------------------------------------------------------


async def test_logout_clears_session_cookie_and_redirects_to_keycloak_end_session(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/protocol/openid-connect/logout" in resp.headers["location"]
    # Cookie cleared via the response's Set-Cookie header (delete_cookie).
    # httpx represents the deletion as the cookie attribute being absent
    # from resp.cookies but present in raw Set-Cookie headers.
    set_cookie_headers = resp.headers.get_list("set-cookie")
    assert any(SESSION_COOKIE_NAME in h and "Max-Age=0" in h for h in set_cookie_headers)


# Suppress unused-import warning for json/UUID/timedelta — used by inline imports.
_ = json
