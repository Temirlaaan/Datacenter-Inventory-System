"""Unit tests for app.web.auth (Sprint 8b Task 0).

Covers cookie encode/decode round-trip + tamper/expiry detection +
require_web_admin auth-failure modes. Full OIDC redirect flow lives in
tests/integration/web/test_oidc_flow.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from fastapi import Request

from app.web.auth import (
    AdminShiftNeeded,
    WebAdminAuthRequired,
    WebAdminUser,
    build_session_cookie_payload,
    decode_session_cookie,
    encode_session_cookie,
    require_web_admin,
    reset_web_auth_cache,
)

_SUB = UUID("11111111-1111-1111-1111-111111111111")


def _make_request(cookies: dict[str, str] | None = None) -> Request:
    """Build a minimal Starlette Request with optional cookies."""
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_header.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/",
        "headers": headers,
    }
    return Request(scope)


# ---------- encode / decode round-trip --------------------------------------


def test_encode_decode_session_cookie_round_trips(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(sub=_SUB, email="alice@example.com", roles=("dcinv-admin",))
    cookie = encode_session_cookie(user)
    decoded = decode_session_cookie(cookie)
    assert decoded is not None
    assert decoded.sub == _SUB
    assert decoded.email == "alice@example.com"
    assert decoded.roles == ("dcinv-admin",)
    # exp round-trips at second precision (timestamps are ints inside).
    assert abs((decoded.exp - user.exp).total_seconds()) < 1


def test_decode_session_cookie_returns_none_on_tampered_payload(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(sub=_SUB, email="alice@example.com", roles=("dcinv-admin",))
    cookie = encode_session_cookie(user)
    # Flip a character mid-cookie to break the Fernet HMAC.
    tampered = cookie[:-5] + ("A" if cookie[-5] != "A" else "B") + cookie[-4:]
    assert decode_session_cookie(tampered) is None


def test_decode_session_cookie_returns_none_when_encrypted_under_different_key(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production rotation: changing SESSION_COOKIE_KEY invalidates outstanding
    cookies. Each one decode-fails individually so users just re-login."""
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(sub=_SUB, email="alice@example.com", roles=("dcinv-admin",))
    # Encrypt under an unrelated key.
    other_key = Fernet.generate_key().decode()
    other_cookie = (
        Fernet(other_key.encode())
        .encrypt(
            json.dumps(
                {
                    "sub": str(user.sub),
                    "email": user.email,
                    "roles": list(user.roles),
                    "exp": int(user.exp.timestamp()),
                }
            ).encode()
        )
        .decode()
    )
    assert decode_session_cookie(other_cookie) is None


def test_decode_session_cookie_returns_none_for_expired_payload(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_session_env(monkeypatch)
    expired_user = WebAdminUser(
        sub=_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) - timedelta(seconds=1),
        csrf_token="irrelevant",
    )
    cookie = encode_session_cookie(expired_user)
    assert decode_session_cookie(cookie) is None


def test_decode_session_cookie_returns_none_for_malformed_payload(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cookie decrypts but the inner JSON is missing required fields."""
    _set_session_env(monkeypatch)
    from app.web.auth import _fernet

    bad_payload = json.dumps({"only": "garbage"}).encode()
    cookie = _fernet().encrypt(bad_payload).decode()
    assert decode_session_cookie(cookie) is None


def test_build_session_cookie_payload_sets_exp_in_future(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(sub=_SUB, email="alice@example.com", roles=("dcinv-admin",))
    delta = user.exp - datetime.now(UTC)
    # Default lifetime is 8 hours per SESSION_COOKIE_MAX_AGE_SECONDS.
    assert 7 * 3600 < delta.total_seconds() <= 8 * 3600 + 1


def test_reset_web_auth_cache_picks_up_new_key(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests need the cached Fernet to drop between runs so a new
    SESSION_COOKIE_KEY (e.g. from a different env) takes effect."""
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(sub=_SUB, email="alice@example.com", roles=("dcinv-admin",))
    cookie_under_old_key = encode_session_cookie(user)
    assert decode_session_cookie(cookie_under_old_key) is not None

    # Rotate the key and clear the cache.
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("SESSION_COOKIE_KEY", new_key)
    from app.config import get_settings

    get_settings.cache_clear()
    reset_web_auth_cache()

    # Old cookie no longer decodes (encrypted under previous key).
    assert decode_session_cookie(cookie_under_old_key) is None


# ---------- require_web_admin dep -------------------------------------------


async def test_require_web_admin_raises_auth_required_without_cookie(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_session_env(monkeypatch)
    with pytest.raises(WebAdminAuthRequired):
        await require_web_admin(_make_request())


async def test_require_web_admin_raises_auth_required_for_invalid_cookie(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_session_env(monkeypatch)
    with pytest.raises(WebAdminAuthRequired):
        await require_web_admin(_make_request({"dcinv_admin_session": "not-a-valid-fernet-token"}))


async def test_require_web_admin_raises_auth_required_when_admin_role_missing(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-admins get the same response as no-cookie — they can't
    differentiate 'wrong cookie' from 'wrong role' (no information leak)."""
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(
        sub=_SUB, email="bob@example.com", roles=("dcinv-mobile-user",)
    )
    cookie = encode_session_cookie(user)
    with pytest.raises(WebAdminAuthRequired):
        await require_web_admin(_make_request({"dcinv_admin_session": cookie}))


async def test_require_web_admin_raises_shift_needed_when_no_active_shift(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _patch_shift_lookup: None,
) -> None:
    """Authenticated + admin + no active shift → raise AdminShiftNeeded
    carrying the user (intermediate page greets them by email)."""
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(sub=_SUB, email="alice@example.com", roles=("dcinv-admin",))
    cookie = encode_session_cookie(user)
    with pytest.raises(AdminShiftNeeded) as exc:
        await require_web_admin(_make_request({"dcinv_admin_session": cookie}))
    assert exc.value.user.email == "alice@example.com"


async def test_require_web_admin_returns_user_with_active_shift(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    _patch_shift_lookup_active: None,
) -> None:
    _set_session_env(monkeypatch)
    user = build_session_cookie_payload(sub=_SUB, email="alice@example.com", roles=("dcinv-admin",))
    cookie = encode_session_cookie(user)
    returned = await require_web_admin(_make_request({"dcinv_admin_session": cookie}))
    assert returned.sub == _SUB
    assert returned.email == "alice@example.com"
    assert "dcinv-admin" in returned.roles


# ---------- helpers / fixtures ----------------------------------------------


def _set_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set just enough env for app.config.Settings + the Fernet to construct."""
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test"
    )
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "test-web-client-secret")
    # A valid Fernet key (44-byte url-safe base64).
    monkeypatch.setenv("SESSION_COOKIE_KEY", "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo=")
    from app.config import get_settings

    get_settings.cache_clear()
    reset_web_auth_cache()


@pytest.fixture
def _patch_shift_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the shift lookup to return None (no active shift)."""

    class _NoShiftRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_active_for_user(self, _user_id: UUID) -> None:
            return None

    monkeypatch.setattr("app.web.auth.ShiftSessionRepository", _NoShiftRepo)
    _patch_sessionmaker(monkeypatch)


@pytest.fixture
def _patch_shift_lookup_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the shift lookup to return a sentinel active shift."""

    class _ActiveShiftRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_active_for_user(self, _user_id: UUID) -> object:
            return object()  # sentinel; require_web_admin only checks not-None

    monkeypatch.setattr("app.web.auth.ShiftSessionRepository", _ActiveShiftRepo)
    _patch_sessionmaker(monkeypatch)


def _patch_sessionmaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the sessionmaker so the shift-lookup doesn't need a real DB."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_cm():
        yield object()

    def _factory():
        return _fake_cm()

    monkeypatch.setattr("app.web.auth.get_sessionmaker", lambda: _factory)
