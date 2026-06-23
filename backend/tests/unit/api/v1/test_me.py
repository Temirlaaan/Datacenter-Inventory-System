"""Unit tests for GET /api/v1/me — direct-await of the handler."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import Request

from app.auth.dependencies import AuthUser

_SUB = "11111111-1111-1111-1111-111111111111"
_FERNET_KEY = "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo="


def _req(*, cookies: dict[str, str] | None = None) -> Request:
    raw_headers: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_str.encode()))
    return Request(
        {"type": "http", "method": "GET", "path": "/api/v1/me",
         "query_string": b"", "headers": raw_headers}
    )


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_a: object) -> None:
        return None


class _FakeSession:
    def begin(self) -> _FakeTx:
        return _FakeTx()


def _shift() -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        user_email="field@x",
        user_keycloak_id=UUID(_SUB),
        shift_start_at=datetime(2026, 6, 23, 9, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="web",
        end_reason=None,
    )


def _patch_repo(monkeypatch: pytest.MonkeyPatch, *, active: object) -> None:
    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_active_for_user(self, _sub: UUID) -> object:
            return active

    monkeypatch.setattr("app.api.v1.me.ShiftSessionRepository", _FakeRepo)


async def test_me_returns_identity_and_active_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1.me import get_me

    _patch_repo(monkeypatch, active=_shift())
    user = AuthUser(sub=_SUB, email="field@x", roles=("dcinv-mobile-user",), session_id=None)

    result = await get_me(request=_req(), user=user, session=_FakeSession())  # type: ignore[arg-type]

    assert result.sub == _SUB
    assert result.roles == ["dcinv-mobile-user"]
    assert result.active_shift is not None
    assert result.active_shift.tablet_id == "web"
    # Bearer caller (no cookie) → no CSRF token.
    assert result.csrf_token is None


async def test_me_returns_none_active_shift_when_no_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1.me import get_me

    _patch_repo(monkeypatch, active=None)
    user = AuthUser(sub=_SUB, email="field@x", roles=("dcinv-mobile-user",), session_id=None)

    result = await get_me(request=_req(), user=user, session=_FakeSession())  # type: ignore[arg-type]

    assert result.active_shift is None


async def test_me_returns_csrf_token_from_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cookie-authenticated caller gets the CSRF token (read out of the
    httpOnly cookie) so the SPA can echo it on writes."""
    from app.api.v1.me import get_me

    monkeypatch.setenv("SESSION_COOKIE_KEY", _FERNET_KEY)
    from app.config import get_settings
    from app.web.auth import (
        SESSION_COOKIE_NAME,
        build_session_cookie_payload,
        encode_session_cookie,
        reset_web_auth_cache,
    )

    get_settings.cache_clear()
    reset_web_auth_cache()
    web_user = build_session_cookie_payload(
        sub=UUID(_SUB), email="field@x", roles=("dcinv-mobile-user",)
    )
    cookie = encode_session_cookie(web_user)

    _patch_repo(monkeypatch, active=None)
    user = AuthUser(sub=_SUB, email="field@x", roles=("dcinv-mobile-user",), session_id=None)

    result = await get_me(
        request=_req(cookies={SESSION_COOKIE_NAME: cookie}),
        user=user,
        session=_FakeSession(),  # type: ignore[arg-type]
    )

    assert result.csrf_token == web_user.csrf_token
