"""Unit tests for app.web.router (Sprint 8b Task 1 close-out).

Direct-await tests for the failure branches of the OIDC callback handler +
the ``/web/`` dashboard handler return. The ``test_oidc_flow.py`` integration
suite covers the happy path + state/nonce-mismatch guards, but the four
token-exchange failure branches (HTTPError, non-200 response, missing
id_token, claim-parse failure) and the dashboard handler's
``return templates.TemplateResponse(...)`` aren't traced through the
``httpx.AsyncClient`` ASGI transport — same coverage-tracing limitation as
the ``query_audit_log`` unit tests (see feedback memory:
``feedback_endpoint_test_direct_await.md``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import Request
from fastapi.responses import HTMLResponse
from jose import jwt

from app.web.auth import (
    SESSION_COOKIE_NAME,
    WebAdminUser,
    reset_web_auth_cache,
)
from app.web.router import (
    _OIDC_NONCE_COOKIE,
    _OIDC_STATE_COOKIE,
    _redirect_to_login,
    batches_detail,
    batches_list,
    dashboard,
    oidc_callback,
)

_USER_SUB = UUID("11111111-1111-1111-1111-111111111111")
_FERNET_KEY = "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo="


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test"
    )
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_ID", "dcinv-web")
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "test-web-client-secret")
    monkeypatch.setenv("SESSION_COOKIE_KEY", _FERNET_KEY)
    from app.config import get_settings

    get_settings.cache_clear()
    reset_web_auth_cache()


def _build_callback_request(*, state: str = "valid-state", nonce: str = "valid-nonce") -> Request:
    """Construct a Request carrying matching state + nonce cookies so the
    handler progresses past the state-check guard and reaches the token
    exchange path."""
    cookie_header = f"{_OIDC_STATE_COOKIE}={state}; {_OIDC_NONCE_COOKIE}={nonce}"
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/oidc/callback",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [(b"cookie", cookie_header.encode())],
    }
    return Request(scope)


# ---------- _redirect_to_login: query-string branch -------------------------


def test_redirect_to_login_preserves_query_string_in_next_param() -> None:
    """The query-string branch (`if request.url.query`) appends ?foo=bar to
    the `next` redirect so the page lands on the originally-requested URL."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/audit/",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"entity_type=qr&page=2",
        "headers": [],
    }
    request = Request(scope)
    response = _redirect_to_login(request)
    assert response.status_code == 302
    # URL-encoded; the path + query were folded into a single ?next=... value.
    assert "next=%2Fweb%2Faudit%2F%3Fentity_type%3Dqr%26page%3D2" in response.headers["location"]


# ---------- oidc_callback failure branches ----------------------------------


async def test_oidc_callback_returns_502_on_httpx_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx raises a transport-layer exception (e.g. Keycloak unreachable) →
    502 BAD GATEWAY HTMLResponse, not 500."""
    _set_env(monkeypatch)

    class _BoomClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("simulated DNS failure")

    with patch.object(httpx, "AsyncClient", _BoomClient):
        request = _build_callback_request(state="s", nonce="n")
        response = await oidc_callback(request, code="x", state="s")

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 502
    assert b"token exchange failed" in response.body


async def test_oidc_callback_returns_400_when_token_endpoint_returns_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keycloak rejected the code (e.g. expired) → 400, not 500."""
    _set_env(monkeypatch)

    class _NonOkClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _NonOkClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(status_code=400, text="invalid_grant")

    with patch.object(httpx, "AsyncClient", _NonOkClient):
        request = _build_callback_request(state="s", nonce="n")
        response = await oidc_callback(request, code="x", state="s")

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 400
    assert b"Keycloak rejected the code" in response.body


async def test_oidc_callback_returns_400_when_token_response_missing_id_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token endpoint responded 200 but the JSON body has no ``id_token``."""
    _set_env(monkeypatch)

    class _NoIdTokenClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _NoIdTokenClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(status_code=200, json={"access_token": "xx"})

    with patch.object(httpx, "AsyncClient", _NoIdTokenClient):
        request = _build_callback_request(state="s", nonce="n")
        response = await oidc_callback(request, code="x", state="s")

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 400
    assert b"no id_token" in response.body


async def test_oidc_callback_returns_400_when_id_token_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable id_token surfaces as 400, not a 500 stack trace."""
    _set_env(monkeypatch)

    class _BadTokenClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _BadTokenClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(status_code=200, json={"id_token": "not.a.valid.jwt"})

    with patch.object(httpx, "AsyncClient", _BadTokenClient):
        request = _build_callback_request(state="s", nonce="n")
        response = await oidc_callback(request, code="x", state="s")

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 400
    assert b"id_token parse failed" in response.body


# ---------- dashboard handler: direct-await return --------------------------


async def test_dashboard_handler_returns_html_response_with_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-await covers the post-await ``return templates.TemplateResponse``
    line that the ASGI stack hides from coverage tracing."""
    _set_env(monkeypatch)

    from app.domain.dashboard import DashboardSnapshot

    canned_snapshot = DashboardSnapshot(
        qr_free_count=11,
        qr_bound_count=22,
        qr_retired_count=33,
        batches_last_30_days=4,
        active_shifts_count=5,
        audit_rows_last_24h=66,
        generated_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
    )

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

        async def snapshot(self, *, now: datetime) -> DashboardSnapshot:
            _ = now
            return canned_snapshot

    monkeypatch.setattr("app.web.router.DashboardRepository", _FakeRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
    )

    # session arg isn't used by the fake repo; pass a sentinel.
    response = await dashboard(request=request, user=user, session=object())  # type: ignore[arg-type]
    assert response.status_code == 200
    body = bytes(response.body)
    # Numbers from the canned snapshot must surface in the rendered HTML.
    assert b">11<" in body
    assert b">22<" in body
    assert b">66<" in body
    assert b"alice@example.com" in body


# ---------- batches list + detail handlers: direct-await returns ------------


async def test_batches_list_handler_returns_html_response_with_seeded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-await covers the post-await ``return templates.TemplateResponse``
    line in ``batches_list`` (ASGI stack hides it from coverage tracing,
    same as Sprint 8b Task 1's dashboard handler)."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.qr import QRBatch

    canned_batch = QRBatch(
        id=uuid4(),
        created_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        created_by_email="alice@example.com",
        created_by_keycloak_id=_USER_SUB,
        count=42,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment="canned-batch",
    )

    class _FakeBatchRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(self, *, page: int, page_size: int) -> tuple[list[QRBatch], bool]:
            _ = page, page_size
            return [canned_batch], False

    monkeypatch.setattr("app.web.router.QRBatchRepository", _FakeBatchRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/batches/",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
    )

    response = await batches_list(
        request=request, page=1, user=user, session=object()  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    assert b"canned-batch" in body
    assert b"alice@example.com" in body


async def test_batches_detail_handler_returns_html_response_for_existing_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the happy-path return in ``batches_detail`` (post-await)."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.qr import QR, QRBatch, QRStatus

    batch_id = uuid4()
    canned_batch = QRBatch(
        id=batch_id,
        created_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        created_by_email="alice@example.com",
        created_by_keycloak_id=_USER_SUB,
        count=1,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment="detail-canned",
    )
    canned_code = QR(
        id="DCQR-CANNED01",
        batch_id=batch_id,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )

    class _FakeBatchRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_by_id(self, _id: object) -> QRBatch | None:
            return canned_batch

    class _FakeCodeRepo:
        def __init__(self, _session: object) -> None: ...

        async def find_by_batch_id(self, _id: object) -> list[QR]:
            return [canned_code]

        async def count_by_status_for_batch(self, _id: object) -> dict[QRStatus, int]:
            return {QRStatus.FREE: 1, QRStatus.BOUND: 0, QRStatus.RETIRED: 0}

    monkeypatch.setattr("app.web.router.QRBatchRepository", _FakeBatchRepo)
    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeCodeRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/web/batches/{batch_id}",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
    )

    response = await batches_detail(
        request=request, batch_id=batch_id, user=user, session=object()  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    assert b"detail-canned" in body
    assert b"DCQR-CANNED01" in body
    assert b"Free: 1" in body


# Suppress unused-import warnings for symbols only referenced inside scopes.
_ = (Fernet, AsyncIterator, jwt, SESSION_COOKIE_NAME, Any)
