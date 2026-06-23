"""Unit tests for app.auth.dependencies — token validation, AuthUser shape, role checks."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import respx
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from tests.unit.auth.conftest import JWKS_URL, RSAKeyPair


def _jwks_payload(*keys: RSAKeyPair) -> dict[str, list[dict[str, str]]]:
    return {"keys": [k.public_jwk for k in keys]}


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _req(*, method: str = "GET", cookies: dict[str, str] | None = None,
         headers: dict[str, str] | None = None) -> Request:
    """Minimal Starlette Request for the cookie-or-bearer ``get_current_user``.

    The bearer path ignores it; the cookie path reads ``cookies`` + the
    ``X-CSRF-Token`` header. No cookie → cookie path returns None → 401."""
    raw_headers: list[tuple[bytes, bytes]] = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_str.encode()))
    return Request(
        {"type": "http", "method": method, "path": "/", "query_string": b"",
         "headers": raw_headers}
    )


@pytest.fixture
def jwks_route(test_key: RSAKeyPair):
    """Standard JWKS endpoint serving the default test_key. Use in most tests."""
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).respond(json=_jwks_payload(test_key))
        yield router


async def test_get_current_user_returns_authuser_for_valid_token(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import AuthUser, get_current_user

    token = token_factory(test_key, sub="user-1", email="alice@x", roles=["admin"], session_id="s1")
    user = await get_current_user(_req(), _bearer(token))

    assert isinstance(user, AuthUser)
    assert user.sub == "user-1"
    assert user.email == "alice@x"
    assert user.roles == ("admin",)
    assert user.session_id == "s1"


async def test_get_current_user_extracts_email_as_none_when_absent(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user

    token = token_factory(test_key, email=None)
    user = await get_current_user(_req(), _bearer(token))
    assert user.email is None


async def test_get_current_user_extracts_roles_as_empty_when_realm_access_absent(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user

    token = token_factory(test_key, roles=None)
    user = await get_current_user(_req(), _bearer(token))
    assert user.roles == ()


async def test_get_current_user_extracts_session_id_as_none_when_absent(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user

    token = token_factory(test_key, session_id=None)
    user = await get_current_user(_req(), _bearer(token))
    assert user.session_id is None


async def test_get_current_user_raises_401_when_no_credentials(
    clean_env: None, auth_env: None
) -> None:
    from app.auth.dependencies import get_current_user

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(), None)
    assert exc.value.status_code == 401


async def test_get_current_user_raises_401_when_token_malformed(
    clean_env: None, auth_env: None
) -> None:
    from app.auth.dependencies import get_current_user

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(), _bearer("not-a-jwt"))
    assert exc.value.status_code == 401


async def test_get_current_user_raises_401_when_token_missing_kid(
    clean_env: None, auth_env: None, test_key: RSAKeyPair
) -> None:
    """A JWT without a `kid` header can't be matched to a JWKS entry — reject it."""
    import time

    from jose import jwt

    from app.auth.dependencies import get_current_user

    now = int(time.time())
    token = jwt.encode(
        {"iss": "https://sso.example.com/realms/prod-v1", "sub": "u", "exp": now + 60},
        test_key.private_pem,
        algorithm="RS256",
        # no headers={"kid": ...}
    )
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(), _bearer(token))
    assert exc.value.status_code == 401


async def test_get_current_user_raises_401_when_kid_unknown_after_refresh(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    foreign_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    """Foreign signing key with a kid not in JWKS — refresh fails to find it → 401."""
    from app.auth.dependencies import get_current_user

    token = token_factory(foreign_key)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(), _bearer(token))
    assert exc.value.status_code == 401


async def test_get_current_user_raises_401_when_signature_invalid(
    clean_env: None,
    auth_env: None,
    test_key: RSAKeyPair,
    foreign_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    """Token signed by foreign_key but stamped with test_key's kid → JWKS lookup
    finds test_key's PUBLIC material, which doesn't match foreign_key's signature.
    """
    from jose import jwt

    from app.auth.dependencies import get_current_user

    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).respond(json={"keys": [test_key.public_jwk]})
        token = token_factory(foreign_key)
        # Re-encode with test_key's kid so JWKS lookup succeeds but signature verification fails.
        token = jwt.encode(
            jwt.get_unverified_claims(token),
            foreign_key.private_pem,
            algorithm="RS256",
            headers={"kid": test_key.kid},
        )
        with pytest.raises(HTTPException) as exc:
            await get_current_user(_req(), _bearer(token))
    assert exc.value.status_code == 401


async def test_get_current_user_raises_401_when_token_expired(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user

    token = token_factory(test_key, expires_in=-60)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(), _bearer(token))
    assert exc.value.status_code == 401


async def test_get_current_user_raises_401_when_issuer_wrong(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user

    token = token_factory(test_key, issuer="https://attacker.example.com/realms/foo")
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(), _bearer(token))
    assert exc.value.status_code == 401


async def test_get_current_user_raises_401_when_sub_missing(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user

    token = token_factory(test_key, omit_claims=("sub",))
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(), _bearer(token))
    assert exc.value.status_code == 401


# ---------- cookie path (browser SPA / web) ----------------------------------

_FERNET_KEY = "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo="


def _cookie_for(
    monkeypatch: pytest.MonkeyPatch, *, roles: tuple[str, ...] = ("dcinv-mobile-user",)
) -> tuple[str, str, str]:
    """Build a valid encrypted session cookie. Returns (cookie_value, csrf, name)."""
    from uuid import UUID

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
        sub=UUID("11111111-1111-1111-1111-111111111111"),
        email="field@x",
        roles=roles,
    )
    return encode_session_cookie(web_user), web_user.csrf_token, SESSION_COOKIE_NAME


async def test_get_current_user_resolves_identity_from_cookie_on_read(
    clean_env: None, auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth.dependencies import AuthUser, get_current_user

    cookie, _csrf, name = _cookie_for(monkeypatch)
    user = await get_current_user(_req(cookies={name: cookie}), None)

    assert isinstance(user, AuthUser)
    assert user.email == "field@x"
    assert user.roles == ("dcinv-mobile-user",)
    assert user.session_id is None  # cookie path carries no JWT sid


async def test_get_current_user_cookie_write_requires_matching_csrf(
    clean_env: None, auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth.dependencies import get_current_user

    cookie, csrf, name = _cookie_for(monkeypatch)

    # Missing CSRF header on a write → 403.
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(method="POST", cookies={name: cookie}), None)
    assert exc.value.status_code == 403

    # Wrong CSRF header → 403.
    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            _req(method="POST", cookies={name: cookie}, headers={"X-CSRF-Token": "nope"}),
            None,
        )
    assert exc.value.status_code == 403

    # Correct CSRF header → resolves.
    user = await get_current_user(
        _req(method="POST", cookies={name: cookie}, headers={"X-CSRF-Token": csrf}),
        None,
    )
    assert user.email == "field@x"


async def test_get_current_user_bearer_takes_precedence_over_cookie(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request with BOTH a bearer token and a cookie uses the bearer identity."""
    from app.auth.dependencies import get_current_user

    cookie, _csrf, name = _cookie_for(monkeypatch)
    token = token_factory(test_key, sub="bearer-user", email="bearer@x")
    user = await get_current_user(_req(cookies={name: cookie}), _bearer(token))
    assert user.sub == "bearer-user"
    assert user.email == "bearer@x"


async def test_get_current_user_raises_401_on_invalid_cookie(
    clean_env: None, auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth.dependencies import get_current_user

    _cookie_for(monkeypatch)  # configures the Fernet key
    from app.web.auth import SESSION_COOKIE_NAME

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_req(cookies={SESSION_COOKIE_NAME: "tampered"}), None)
    assert exc.value.status_code == 401


async def test_require_role_returns_user_when_role_present(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user, require_role

    token = token_factory(test_key, roles=["operator", "viewer"])
    user = await get_current_user(_req(), _bearer(token))
    dep = require_role("operator")
    result = await dep(user)
    assert result is user


async def test_require_role_raises_403_when_role_missing(
    clean_env: None,
    auth_env: None,
    jwks_route: respx.MockRouter,
    test_key: RSAKeyPair,
    token_factory: Callable[..., str],
) -> None:
    from app.auth.dependencies import get_current_user, require_role

    token = token_factory(test_key, roles=["viewer"])
    user = await get_current_user(_req(), _bearer(token))
    dep = require_role("operator")
    with pytest.raises(HTTPException) as exc:
        await dep(user)
    assert exc.value.status_code == 403


# ---------- require_role_with_active_shift (Sprint 6 Task 4 step a) ----------


def _user(*, sub: str, roles: tuple[str, ...] = ("dcinv-mobile-user",)) -> object:
    """Build a minimal AuthUser. Imported lazily to avoid pulling app deps at module load."""
    from app.auth.dependencies import AuthUser

    return AuthUser(sub=sub, email="alice@example.com", roles=roles, session_id=None)


class _FakeTx:
    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        return False


class _FakeSession:
    """Minimal stand-in for AsyncSession exposing only ``begin()`` and ``in_transaction()``."""

    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _FakeTx:
        return _FakeTx()


class _FakeShiftSessionRepo:
    """Stand-in for ShiftSessionRepository — returns a canned active or None."""

    def __init__(self, active: object) -> None:
        self._active = active

    async def get_active_for_user(self, _user_keycloak_id: object) -> object:
        return self._active


def _build_active_shift(user_sub: str, shift_id: str = "33333333-3333-3333-3333-333333333333"):
    from datetime import UTC, datetime
    from uuid import UUID

    from app.domain.shift_session import ShiftSession

    return ShiftSession(
        id=UUID(shift_id),
        user_email="alice@example.com",
        user_keycloak_id=UUID(user_sub),
        shift_start_at=datetime(2026, 5, 29, 9, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="tablet-01",
        end_reason=None,
    )


async def test_require_role_with_active_shift_returns_user_with_populated_shift_session_id(
    clean_env: None, auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: role present + active shift exists → AuthUser carries the shift's UUID."""
    from uuid import UUID

    from app.auth import dependencies as deps

    user_sub = "11111111-1111-1111-1111-111111111111"
    shift = _build_active_shift(user_sub)
    monkeypatch.setattr(
        deps, "ShiftSessionRepository", lambda _session: _FakeShiftSessionRepo(shift)
    )

    dep = deps.require_role_with_active_shift("dcinv-mobile-user")
    result = await dep(user=_user(sub=user_sub), session=_FakeSession())

    assert result.shift_session_id == shift.id
    assert isinstance(result.shift_session_id, UUID)
    # Other fields preserved.
    assert result.sub == user_sub
    assert result.roles == ("dcinv-mobile-user",)


async def test_require_role_with_active_shift_raises_403_when_role_missing(
    clean_env: None, auth_env: None
) -> None:
    """Role check fires before the DB lookup — no shift repo call."""
    from app.auth.dependencies import require_role_with_active_shift

    dep = require_role_with_active_shift("dcinv-mobile-user")
    with pytest.raises(HTTPException) as exc:
        await dep(user=_user(sub="x", roles=("dcinv-admin",)), session=_FakeSession())
    assert exc.value.status_code == 403


async def test_require_role_with_active_shift_raises_no_active_shift_when_no_shift(
    clean_env: None, auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision G: role present but no active shift → NoActiveShiftError (→ 409 NO_ACTIVE_SHIFT)."""
    from app.auth import dependencies as deps

    monkeypatch.setattr(
        deps, "ShiftSessionRepository", lambda _session: _FakeShiftSessionRepo(None)
    )

    dep = deps.require_role_with_active_shift("dcinv-mobile-user")
    with pytest.raises(deps.NoActiveShiftError):
        await dep(
            user=_user(sub="11111111-1111-1111-1111-111111111111"),
            session=_FakeSession(),
        )
