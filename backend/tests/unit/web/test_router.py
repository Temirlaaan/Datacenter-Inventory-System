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
from typing import Any, cast
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
    _parse_optional_form_int,
    _redirect_to_login,
    _sessions_filter_query_string,
    audit_detail,
    audit_list,
    batches_detail,
    batches_list,
    batches_new_form,
    dashboard,
    devices_decommission_form,
    oidc_callback,
    sessions_list,
    web_admin_shift_start,
    web_audit_csv,
    web_batches_create,
    web_batches_labels_pdf,
    web_devices_add_comment,
    web_devices_decommission,
    web_devices_detail,
    web_devices_search,
    web_force_close_session,
    web_qr_retire,
    web_qr_search,
    web_users_detail,
    web_users_list,
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

    # Sprint 10 Task 1: dashboard also queries AuditLogRepository for the
    # activity feed. Stub it with an empty page so this test stays focused
    # on the snapshot rendering.
    class _FakeAuditRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(self, **_kwargs: object) -> tuple[list[Any], bool]:
            return [], False

    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeAuditRepo)

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
        csrf_token="test-csrf-token",
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
    # Empty activity feed → empty-state copy.
    assert b"No recent activity" in body


async def test_dashboard_handler_renders_activity_feed_with_audit_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint 10 Task 1 — populated feed: audit rows render in a table
    below the counter grid, each linking to its detail page."""
    _set_env(monkeypatch)
    from app.domain.audit import AuditLogEntry, AuditResult
    from app.domain.dashboard import DashboardSnapshot

    canned_snapshot = DashboardSnapshot(
        qr_free_count=0,
        qr_bound_count=0,
        qr_retired_count=0,
        batches_last_30_days=0,
        active_shifts_count=0,
        audit_rows_last_24h=2,
        generated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
    )

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

        async def snapshot(self, *, now: datetime) -> DashboardSnapshot:
            _ = now
            return canned_snapshot

    monkeypatch.setattr("app.web.router.DashboardRepository", _FakeRepo)

    activity = [
        AuditLogEntry(
            request_id=UUID("11111111-1111-1111-1111-111111111111"),
            timestamp=datetime(2026, 6, 8, 11, 30, 0, tzinfo=UTC),
            user_email="engineer@example.com",
            user_keycloak_id=_USER_SUB,
            session_id=None,
            operation="qr.bind",
            entity_type="qr",
            entity_id="QR-7F3A2B",
            before_json={},
            after_json={},
            result=AuditResult.SUCCESS,
            id=101,
        ),
        AuditLogEntry(
            request_id=UUID("22222222-2222-2222-2222-222222222222"),
            timestamp=datetime(2026, 6, 8, 11, 0, 0, tzinfo=UTC),
            user_email="alice@example.com",
            user_keycloak_id=_USER_SUB,
            session_id=None,
            operation="device.update",
            entity_type="device",
            entity_id="42",
            before_json={},
            after_json={},
            result=AuditResult.CONFLICT,
            id=102,
        ),
    ]

    class _FakeAuditRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(self, **_kwargs: object) -> tuple[list[Any], bool]:
            return activity, False

    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeAuditRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
        csrf_token="test-csrf-token",
    )
    response = await dashboard(
        request=Request(scope), user=user, session=object()  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    # Both rows must render with their operation + entity + link.
    assert b"qr.bind" in body
    assert b"QR-7F3A2B" in body
    assert b"device.update" in body
    assert b"engineer@example.com" in body
    # Each row links to its detail page.
    assert b'href="/web/audit/101"' in body
    assert b'href="/web/audit/102"' in body
    # Conflict result badge text.
    assert b"conflict" in body


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
        intended_site_id="",
        intended_location_id="",
        intended_rack_id="",
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
        csrf_token="test-csrf-token",
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
        intended_site_id="",
        intended_location_id="",
        intended_rack_id="",
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
        csrf_token="test-csrf-token",
    )

    response = await batches_detail(
        request=request, batch_id=batch_id, user=user, session=object()  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    assert b"detail-canned" in body
    assert b"DCQR-CANNED01" in body
    assert b"Free: 1" in body


# ---------- audit list + detail handlers: direct-await returns -------------


async def test_audit_list_handler_returns_html_response_with_seeded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-await covers the post-await ``return templates.TemplateResponse``
    line in ``audit_list`` (ASGI stack hides it from coverage tracing)."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.audit import AuditLogEntry, AuditResult

    canned_entry = AuditLogEntry(
        id=42,
        request_id=uuid4(),
        timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        session_id=None,
        operation="qr.bind",
        entity_type="qr",
        entity_id="DCQR-UNIT001",
        before_json={},
        after_json={},
        result=AuditResult.SUCCESS,
    )

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(
            self, *, filters: object, page: int, page_size: int
        ) -> tuple[list[AuditLogEntry], bool]:
            _ = filters, page, page_size
            return [canned_entry], False

    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/audit/",
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
        csrf_token="test-csrf-token",
    )

    response = await audit_list(
        request=request,
        page=1,
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type=None,
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        user=user,
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    assert b"DCQR-UNIT001" in body
    assert b"alice@example.com" in body


async def test_audit_detail_handler_returns_html_response_for_existing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the happy-path return in ``audit_detail`` (post-await)."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.audit import AuditLogEntry, AuditResult

    canned_entry = AuditLogEntry(
        id=99,
        request_id=uuid4(),
        timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        session_id=None,
        operation="qr.retire",
        entity_type="qr",
        entity_id="DCQR-DET999",
        before_json={"old": 1},
        after_json={"new": 2},
        result=AuditResult.SUCCESS,
    )

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_by_id(self, _id: int) -> AuditLogEntry | None:
            return canned_entry

    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/audit/99",
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
        csrf_token="test-csrf-token",
    )

    response = await audit_detail(
        request=request, audit_id=99, user=user, session=object()  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    assert b"DCQR-DET999" in body
    assert b"qr.retire" in body


# ---------- _sessions_filter_query_string -----------------------------------


def test_sessions_filter_query_string_includes_non_empty_from_and_to_values() -> None:
    """Cover the True branches for ``from_`` and ``to`` in the
    ``_sessions_filter_query_string`` helper. The integration tests pass
    empty hidden form values so the True branches don't trigger there."""
    qs = _sessions_filter_query_string(
        user_keycloak_id=None,
        from_="2026-06-01T00:00:00+00:00",
        to="2026-06-30T23:59:59+00:00",
        active_only=False,
    )
    assert "from=" in qs
    assert "to=" in qs


def test_sessions_filter_query_string_drops_all_empty_fields() -> None:
    assert (
        _sessions_filter_query_string(user_keycloak_id=None, from_=None, to=None, active_only=False)
        == ""
    )


# ---------- sessions_list + web_force_close handlers: direct-await ----------


async def test_sessions_list_handler_returns_html_response_with_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-await covers the post-await ``return templates.TemplateResponse``
    line in ``sessions_list`` (ASGI stack hides it from coverage tracing)."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.shift_session import ShiftSession

    canned_shift = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        shift_start_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="canned-tablet",
        end_reason=None,
    )

    class _FakeShiftRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(
            self, *, filters: object, page: int, page_size: int
        ) -> tuple[list[ShiftSession], bool]:
            _ = filters, page, page_size
            return [canned_shift], False

    monkeypatch.setattr("app.web.router.ShiftSessionRepository", _FakeShiftRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/sessions/",
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
        csrf_token="test-csrf-token",
    )

    response = await sessions_list(
        request=request,
        page=1,
        user_keycloak_id=None,
        from_=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
        to=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
        active_only=False,
        flash=None,
        flash_kind=None,
        user=user,
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    assert b"canned-tablet" in body
    assert b"alice@example.com" in body


async def test_web_force_close_session_returns_303_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-await covers the post-await success-return line in
    ``web_force_close_session`` (line 662 in router.py)."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.shift_session import ShiftEndReason, ShiftSession

    admin_shift = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        shift_start_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="admin-tablet",
        end_reason=None,
    )

    class _FakeShiftRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_active_for_user(self, _user_id: UUID) -> ShiftSession:
            return admin_shift

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_cm():
        yield object()

    monkeypatch.setattr("app.web.router.ShiftSessionRepository", _FakeShiftRepo)
    monkeypatch.setattr("app.web.router.get_sessionmaker", lambda: _fake_cm)

    async def _fake_force_close(**_kwargs: object) -> object:
        return None  # success path: JSON handler returned without raising

    monkeypatch.setattr("app.web.router.force_close_session", _fake_force_close)

    # ShiftEndReason imported so the underlying handler's type contracts stay
    # exercised in user-visible code; not directly used here.
    _ = ShiftEndReason

    target_id = uuid4()
    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
        csrf_token="test-csrf-token",
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/web/sessions/{target_id}/force-close",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    response = await web_force_close_session(
        request=request,
        session_id=target_id,
        reason="direct-await success path",
        user_keycloak_id=None,
        from_=None,
        to=None,
        active_only_value=None,
        csrf="test-csrf-token",
        user=user,
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash=Shift+force-closed" in response.headers["location"]


async def test_web_force_close_session_returns_303_with_error_flash_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the 404 branch + flash-error redirect (lines 656-660)."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.shift_session import ShiftSession

    admin_shift = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        shift_start_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="admin-tablet",
        end_reason=None,
    )

    class _FakeShiftRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_active_for_user(self, _user_id: UUID) -> ShiftSession:
            return admin_shift

    from contextlib import asynccontextmanager

    from fastapi import HTTPException, status

    @asynccontextmanager
    async def _fake_cm():
        yield object()

    monkeypatch.setattr("app.web.router.ShiftSessionRepository", _FakeShiftRepo)
    monkeypatch.setattr("app.web.router.get_sessionmaker", lambda: _fake_cm)

    async def _fake_force_close(**_kwargs: object) -> object:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    monkeypatch.setattr("app.web.router.force_close_session", _fake_force_close)

    target_id = uuid4()
    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
        csrf_token="test-csrf-token",
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/web/sessions/{target_id}/force-close",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    response = await web_force_close_session(
        request=request,
        session_id=target_id,
        reason="targeting unknown shift",
        user_keycloak_id=None,
        from_=None,
        to=None,
        active_only_value=None,
        csrf="test-csrf-token",
        user=user,
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash=Shift+not+found" in response.headers["location"]
    assert "flash_kind=error" in response.headers["location"]


async def test_web_force_close_session_reraises_non_404_http_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the ``raise`` branch when the JSON handler returns any
    non-404 HTTPException (e.g. 500). The web layer must surface it
    instead of swallowing into a generic flash."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.shift_session import ShiftSession

    admin_shift = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        shift_start_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="admin-tablet",
        end_reason=None,
    )

    class _FakeShiftRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_active_for_user(self, _user_id: UUID) -> ShiftSession:
            return admin_shift

    from contextlib import asynccontextmanager

    from fastapi import HTTPException

    @asynccontextmanager
    async def _fake_cm():
        yield object()

    monkeypatch.setattr("app.web.router.ShiftSessionRepository", _FakeShiftRepo)
    monkeypatch.setattr("app.web.router.get_sessionmaker", lambda: _fake_cm)

    async def _fake_force_close(**_kwargs: object) -> object:
        raise HTTPException(status_code=500, detail="boom")

    monkeypatch.setattr("app.web.router.force_close_session", _fake_force_close)

    target_id = uuid4()
    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
        csrf_token="test-csrf-token",
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/web/sessions/{target_id}/force-close",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    with pytest.raises(HTTPException) as exc:
        await web_force_close_session(
            request=request,
            session_id=target_id,
            reason="will surface as 500",
            user_keycloak_id=None,
            from_=None,
            to=None,
            active_only_value=None,
            csrf="test-csrf-token",
            user=user,
            session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 500


# ---------- web_admin_shift_start: "Open admin shift" form handler ----------


def _build_shift_start_request(
    *, cookie_value: str | None = None
) -> Request:
    """Construct a POST /web/admin/shift/start Request, optionally carrying
    a session cookie. The handler reads the cookie inline (skipping
    ``require_web_admin``'s shift check), so unit tests build the Request
    directly rather than threading a dep-overridden TestClient."""
    headers: list[tuple[bytes, bytes]] = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE_NAME}={cookie_value}".encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/web/admin/shift/start",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": headers,
    }
    return Request(scope)


def _admin_cookie_value(
    monkeypatch: pytest.MonkeyPatch, *, csrf_token: str = "test-csrf-token"
) -> str:
    """Fernet-encrypt an admin WebAdminUser payload using the test key.

    Builds the WebAdminUser directly (not via ``build_session_cookie_payload``)
    so the CSRF token is deterministic — tests need to know it to pass a
    matching value into the POST handler.
    """
    _set_env(monkeypatch)
    from app.web.auth import encode_session_cookie

    user = WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
        csrf_token=csrf_token,
    )
    return encode_session_cookie(user)


def _non_admin_cookie_value(monkeypatch: pytest.MonkeyPatch) -> str:
    _set_env(monkeypatch)
    from app.web.auth import build_session_cookie_payload, encode_session_cookie

    user = build_session_cookie_payload(
        sub=_USER_SUB, email="bob@example.com", roles=("dcinv-mobile-user",)
    )
    return encode_session_cookie(user)


async def test_web_admin_shift_start_returns_303_to_dashboard_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: valid admin cookie + workstation_id → service.start
    succeeds → 303 to /web/."""
    cookie = _admin_cookie_value(monkeypatch)

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeService:
        def __init__(self, *, session: object, repo: object) -> None: ...

        async def start(self, *, user_email: str, user_keycloak_id: UUID, tablet_id: str) -> None:
            assert user_email == "alice@example.com"
            assert user_keycloak_id == _USER_SUB
            assert tablet_id == "admin-laptop-01"

    monkeypatch.setattr("app.web.router.ShiftSessionRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.ShiftSessionService", _FakeService)

    response = await web_admin_shift_start(
        request=_build_shift_start_request(cookie_value=cookie),
        workstation_id="admin-laptop-01",
        csrf="test-csrf-token",
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/web/"


async def test_web_admin_shift_start_returns_303_to_dashboard_when_already_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent UX: ``SessionAlreadyActive`` (concurrent open in another
    tab) still lands the user on /web/ — they're already in the state the
    page wanted."""
    from uuid import uuid4

    from app.domain.shift_session import ShiftSession
    from app.services.shift_session import SessionAlreadyActive

    cookie = _admin_cookie_value(monkeypatch)
    winner = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        shift_start_at=datetime(2026, 6, 5, 10, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="admin-laptop-01",
        end_reason=None,
    )

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeService:
        def __init__(self, *, session: object, repo: object) -> None: ...

        async def start(self, **_kwargs: object) -> None:
            raise SessionAlreadyActive(winner)

    monkeypatch.setattr("app.web.router.ShiftSessionRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.ShiftSessionService", _FakeService)

    response = await web_admin_shift_start(
        request=_build_shift_start_request(cookie_value=cookie),
        workstation_id="admin-laptop-01",
        csrf="test-csrf-token",
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/web/"


async def test_web_admin_shift_start_returns_303_to_login_without_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No session cookie → redirect to /web/login (no information leak)."""
    _set_env(monkeypatch)
    response = await web_admin_shift_start(
        request=_build_shift_start_request(cookie_value=None),
        workstation_id="admin-laptop-01",
        csrf="test-csrf-token",
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/web/login"


async def test_web_admin_shift_start_returns_303_to_login_when_role_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid cookie but no ``dcinv-admin`` role → same redirect as no
    cookie (same information-leak rule as require_web_admin)."""
    cookie = _non_admin_cookie_value(monkeypatch)
    response = await web_admin_shift_start(
        request=_build_shift_start_request(cookie_value=cookie),
        workstation_id="admin-laptop-01",
        csrf="test-csrf-token",
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/web/login"


# ---------- Admin-action handlers (post-Sprint 8b fixes 2026-06-05) ---------
#
# Five new handlers added 2026-06-05 to close the admin-loop gap (admin can now
# create batches, retire FREE QRs, and decommission devices from /web/* instead
# of curling JSON endpoints). The pattern follows ``web_force_close_session``:
# delegate to the underlying service directly so the three-record-write
# apparatus stays in one place. Tests use the direct-await convention; the
# heavy shift-lookup + service init lives behind monkeypatched fakes.


def _admin_action_request() -> Request:
    """Bare Request scope (cookie/role check happens in require_web_admin,
    which is supplied as the ``user=`` arg directly in these tests)."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/web/admin-action",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    return Request(scope)


def _patch_admin_shift_lookup(
    monkeypatch: pytest.MonkeyPatch, *, sub: UUID = _USER_SUB
) -> None:
    """Make ``_build_auth_user_for_admin_action`` return cleanly by stubbing
    the ShiftSessionRepository + get_sessionmaker pair it consults (same
    pattern as the force-close tests)."""
    from contextlib import asynccontextmanager
    from uuid import uuid4

    from app.domain.shift_session import ShiftSession

    admin_shift = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=sub,
        shift_start_at=datetime(2026, 6, 5, 9, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="admin-laptop-01",
        end_reason=None,
    )

    class _FakeShiftRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_active_for_user(self, _user_id: UUID) -> ShiftSession:
            return admin_shift

    @asynccontextmanager
    async def _fake_cm():
        yield object()

    monkeypatch.setattr("app.web.router.ShiftSessionRepository", _FakeShiftRepo)
    monkeypatch.setattr("app.web.router.get_sessionmaker", lambda: _fake_cm)


def _admin_user(csrf_token: str = "test-csrf-token") -> WebAdminUser:
    return WebAdminUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        exp=datetime.now(UTC) + timedelta(hours=1),
        csrf_token=csrf_token,
    )


# --- web_batches_create -----------------------------------------------------


async def test_web_batches_create_redirects_to_detail_with_flash_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: form data → service.generate_batch → session.commit → 303."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from uuid import uuid4

    from app.domain.qr import QRBatch

    new_batch_id = uuid4()
    captured: dict[str, Any] = {}

    class _FakeQRBatchRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeQRCodeRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeAuditRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeGenerationService:
        def __init__(self, _session: object, _b: object, _c: object, _a: object) -> None: ...

        async def generate_batch(self, payload: Any, user: Any) -> QRBatch:
            captured["count"] = payload.count
            captured["comment"] = payload.comment
            captured["user_sub"] = user.sub
            return QRBatch(
                id=new_batch_id,
                created_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
                created_by_email=user.email,
                created_by_keycloak_id=UUID(user.sub),
                count=payload.count,
                intended_site_id=payload.intended_site_id,
                intended_location_id=payload.intended_location_id,
                intended_rack_id=payload.intended_rack_id,
                comment=payload.comment,
            )

    monkeypatch.setattr("app.web.router.QRBatchRepository", _FakeQRBatchRepo)
    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeQRCodeRepo)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeAuditRepo)
    monkeypatch.setattr("app.web.router.QRGenerationService", _FakeGenerationService)

    class _FakeSession:
        async def commit(self) -> None: ...

    response = await web_batches_create(
        count=25,
        comment="tray-A initial",
        intended_site_id="",
        intended_location_id="",
        intended_rack_id="",
        csrf="test-csrf-token",
        user=_admin_user(),
        session=_FakeSession(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/web/batches/{new_batch_id}?")
    assert "flash=Batch+created+with+25+codes" in response.headers["location"]
    assert "flash_kind=info" in response.headers["location"]
    assert captured == {
        "count": 25,
        "comment": "tray-A initial",
        "user_sub": str(_USER_SUB),
    }


async def test_web_batches_create_strips_comment_to_none_when_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty form ``comment`` → service receives ``None`` (the ``comment``
    column is nullable; storing an empty string would lie about intent)."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from uuid import uuid4

    from app.domain.qr import QRBatch

    received: dict[str, Any] = {}

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeGenerationService:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def generate_batch(self, payload: Any, user: Any) -> QRBatch:
            received["comment"] = payload.comment
            return QRBatch(
                id=uuid4(),
                created_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
                created_by_email=user.email,
                created_by_keycloak_id=UUID(user.sub),
                count=payload.count,
                intended_site_id=None,
                intended_location_id=None,
                intended_rack_id=None,
                comment=payload.comment,
            )

    monkeypatch.setattr("app.web.router.QRBatchRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.QRGenerationService", _FakeGenerationService)

    class _FakeSession:
        async def commit(self) -> None: ...

    await web_batches_create(
        count=10,
        comment="   ",  # whitespace-only, stripped → empty → None
        intended_site_id="",
        intended_location_id="",
        intended_rack_id="",
        csrf="test-csrf-token",
        user=_admin_user(),
        session=_FakeSession(),  # type: ignore[arg-type]
    )
    assert received["comment"] is None


# --- web_qr_retire ---------------------------------------------------------


def _patch_lifecycle_service(monkeypatch: pytest.MonkeyPatch, retire_impl: Any) -> None:
    """Install a fake QRLifecycleService whose ``retire`` runs ``retire_impl``.
    Wires up the netbox_client + write_service deps as no-ops since the
    handler reaches them via the import path on each call."""

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeWriteService:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

    class _FakeLifecycle:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def retire(self, **kwargs: object) -> Any:
            return await retire_impl(**kwargs)

    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.NetBoxWriteService", _FakeWriteService)
    monkeypatch.setattr("app.web.router.QRLifecycleService", _FakeLifecycle)
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())


@pytest.mark.parametrize(
    "raise_factory, expected_flash_kind, expected_flash_fragment",
    [
        pytest.param(
            lambda: None,
            "info",
            "QR+QR-ABC+retired",
            id="success",
        ),
    ],
)
async def test_web_qr_retire_success_redirects_with_info_flash(
    monkeypatch: pytest.MonkeyPatch,
    raise_factory: Any,
    expected_flash_kind: str,
    expected_flash_fragment: str,
) -> None:
    """Happy path: service.retire returns cleanly → 303 with info flash."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)

    async def _retire(**_kwargs: object) -> Any:
        return raise_factory()

    _patch_lifecycle_service(monkeypatch, _retire)

    response = await web_qr_retire(
        qr_id="QR-ABC",
        batch_id=None,
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/batches/?")
    assert f"flash_kind={expected_flash_kind}" in response.headers["location"]
    assert expected_flash_fragment in response.headers["location"]


async def test_web_qr_retire_redirects_back_to_batch_detail_when_batch_id_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden ``batch_id`` form input from the detail template → 303 to
    ``/web/batches/{batch_id}`` instead of the bare list. Preserves the
    admin's inspection context when retiring multiple FREE codes in a row
    (HIGH-1 from the 2026-06-05 code review)."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from uuid import uuid4

    async def _retire(**_kwargs: object) -> Any:
        return None

    _patch_lifecycle_service(monkeypatch, _retire)

    batch_id = uuid4()
    response = await web_qr_retire(
        qr_id="QR-K",
        batch_id=batch_id,
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/web/batches/{batch_id}?")
    assert "flash_kind=info" in response.headers["location"]


async def test_web_qr_retire_unknown_qr_redirects_with_error_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QRNotFoundError → 303 with error flash."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from app.services.qr.lifecycle import QRNotFoundError

    async def _retire(**_kwargs: object) -> Any:
        raise QRNotFoundError("QR-DOESNT-EXIST")

    _patch_lifecycle_service(monkeypatch, _retire)

    response = await web_qr_retire(
        qr_id="QR-DOESNT-EXIST",
        batch_id=None,
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "not+registered" in response.headers["location"]


async def test_web_qr_retire_already_retired_redirects_with_info_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QRStateConflictError(current=RETIRED) → 303 with info flash ("already
    retired"). Treats a no-op as success so the UI is idempotent."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from app.domain.qr import QRStatus
    from app.services.qr.lifecycle import QRStateConflictError

    async def _retire(**_kwargs: object) -> Any:
        raise QRStateConflictError(current_status=QRStatus.RETIRED)

    _patch_lifecycle_service(monkeypatch, _retire)

    response = await web_qr_retire(
        qr_id="QR-X",
        batch_id=None,
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash_kind=info" in response.headers["location"]
    assert "already+retired" in response.headers["location"]


async def test_web_qr_retire_bound_qr_redirects_with_error_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A QR somehow caught in BOUND state (the template hides this button for
    BOUND codes, but a race or hand-rolled POST could still hit it) →
    QRStateConflictError(current=BOUND) → 303 with error flash that points
    at the decommission flow."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from app.domain.qr import QRStatus
    from app.services.qr.lifecycle import QRStateConflictError

    async def _retire(**_kwargs: object) -> Any:
        raise QRStateConflictError(current_status=QRStatus.BOUND)

    _patch_lifecycle_service(monkeypatch, _retire)

    response = await web_qr_retire(
        qr_id="QR-B",
        batch_id=None,
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "device+decommission" in response.headers["location"]


async def test_web_qr_retire_missing_version_redirects_with_error_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MissingVersionError (BOUND retire without a version) → 303 error,
    same pointer to decommission flow."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from app.services.qr.lifecycle import MissingVersionError

    async def _retire(**_kwargs: object) -> Any:
        raise MissingVersionError("QR-V")

    _patch_lifecycle_service(monkeypatch, _retire)

    response = await web_qr_retire(
        qr_id="QR-V",
        batch_id=None,
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "device+decommission" in response.headers["location"]


# --- web_devices_decommission ----------------------------------------------


def _patch_decommission_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    get_device_impl: Any,
    decommission_impl: Any,
) -> None:
    """Install fakes for DeviceService.get_device + DeviceDecommissionService.decommission."""

    class _FakeDeviceService:
        def __init__(self, _client: object) -> None: ...

        async def get_device(self, device_id: int) -> Any:
            return await get_device_impl(device_id)

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeWriteService:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

    class _FakeLifecycle:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

    class _FakeDecommissionService:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def decommission(self, **kwargs: object) -> Any:
            return await decommission_impl(**kwargs)

    monkeypatch.setattr("app.web.router.DeviceService", _FakeDeviceService)
    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.NetBoxWriteService", _FakeWriteService)
    monkeypatch.setattr("app.web.router.QRLifecycleService", _FakeLifecycle)
    monkeypatch.setattr(
        "app.web.router.DeviceDecommissionService", _FakeDecommissionService
    )
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())


async def test_web_devices_decommission_success_redirects_with_info_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: get_device + decommission both succeed → 303 info flash."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)

    captured: dict[str, Any] = {}

    class _DeviceStub:
        version = "2026-06-05T12:00:00Z"

    async def _get_device(_device_id: int) -> Any:
        return _DeviceStub()

    async def _decommission(**kwargs: object) -> Any:
        captured.update(kwargs)
        return object()

    _patch_decommission_pipeline(
        monkeypatch, get_device_impl=_get_device, decommission_impl=_decommission
    )

    response = await web_devices_decommission(
        device_id=42,
        reason="end of life",
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/devices/decommission?")
    assert "flash_kind=info" in response.headers["location"]
    assert "Device+42+decommissioned" in response.headers["location"]
    assert captured["device_id"] == 42
    assert captured["expected_version"] == "2026-06-05T12:00:00Z"
    assert captured["reason"] == "end of life"


async def test_web_devices_decommission_unknown_device_redirects_with_error_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_device raises NetBoxNotFound → 303 with error flash; decommission
    service never called."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from app.netbox.errors import NetBoxNotFound

    async def _get_device(_device_id: int) -> Any:
        raise NetBoxNotFound("device 999")

    decom_called = False

    async def _decommission(**_kwargs: object) -> Any:
        nonlocal decom_called
        decom_called = True
        return object()

    _patch_decommission_pipeline(
        monkeypatch, get_device_impl=_get_device, decommission_impl=_decommission
    )

    response = await web_devices_decommission(
        device_id=999,
        reason="bad lookup",
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "not+found" in response.headers["location"]
    assert not decom_called


async def test_web_devices_decommission_write_conflict_redirects_with_error_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WriteConflictError between our get_device and the decommission write
    (someone else modified the device concurrently) → 303 with error flash."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from app.services.netbox_write import WriteConflictError

    class _DeviceStub:
        version = "2026-06-05T12:00:00Z"

    async def _get_device(_device_id: int) -> Any:
        return _DeviceStub()

    async def _decommission(**_kwargs: object) -> Any:
        raise WriteConflictError(
            current_version="2026-06-05T12:00:05Z", current_object={"id": 7}
        )

    _patch_decommission_pipeline(
        monkeypatch, get_device_impl=_get_device, decommission_impl=_decommission
    )

    response = await web_devices_decommission(
        device_id=7,
        reason="concurrent",
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "modified+concurrently" in response.headers["location"]


# --- CSRF verification (one mismatch test per POST handler) -----------------


async def test_web_batches_create_rejects_csrf_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong ``csrf`` → 403 from ``verify_csrf_token`` before any DB work."""
    from fastapi import HTTPException

    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await web_batches_create(
            count=10,
            csrf="WRONG-TOKEN",
            comment="",
            intended_site_id=None,
            intended_location_id=None,
            intended_rack_id=None,
            user=_admin_user(),
            session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


async def test_web_qr_retire_rejects_csrf_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await web_qr_retire(
            qr_id="QR-X",
            csrf="WRONG-TOKEN",
            batch_id=None,
            user=_admin_user(),
            session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


async def test_web_devices_decommission_rejects_csrf_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await web_devices_decommission(
            device_id=42,
            reason="any",
            csrf="WRONG-TOKEN",
            user=_admin_user(),
            session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


async def test_web_force_close_session_rejects_csrf_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from fastapi import HTTPException

    _set_env(monkeypatch)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/web/sessions/x/force-close",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    with pytest.raises(HTTPException) as exc:
        await web_force_close_session(
            request=Request(scope),
            session_id=uuid4(),
            reason="any",
            csrf="WRONG-TOKEN",
            user_keycloak_id=None,
            from_=None,
            to=None,
            active_only_value=None,
            user=_admin_user(),
            session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


async def test_web_admin_shift_start_rejects_csrf_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shift-start handler resolves the cookie itself, so the CSRF
    token comes from the cookie payload — a wrong ``csrf`` form value
    fails ``verify_csrf_token`` after auth succeeds but before any DB
    work."""
    from fastapi import HTTPException

    cookie = _admin_cookie_value(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await web_admin_shift_start(
            request=_build_shift_start_request(cookie_value=cookie),
            workstation_id="admin-laptop-01",
            csrf="WRONG-TOKEN",
            session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


# --- GET form pages (template-render coverage) ------------------------------


async def test_batches_new_form_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /web/batches/new`` returns the form template."""
    _set_env(monkeypatch)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/batches/new",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await batches_new_form(request=Request(scope), user=_admin_user())
    assert response.status_code == 200
    assert b"New QR batch" in response.body


async def test_web_qr_search_renders_empty_form_when_no_qr_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /web/qr/search`` with no ``qr_id`` shows the search form and no
    result block (``lookup_attempted=False``)."""
    _set_env(monkeypatch)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/qr/search",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_qr_search(
        request=Request(scope),
        qr_id=None,
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    assert b"QR search" in response.body
    assert b"No QR with id" not in response.body  # no lookup attempted


async def test_web_qr_search_renders_not_found_for_unknown_qr_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookup returns ``None`` → empty-state message under the form."""
    _set_env(monkeypatch)

    class _FakeQRCodeRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_by_id(self, _qr_id: str) -> None:
            return None

    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeQRCodeRepo)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/qr/search",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"qr_id=QR-DOESNT-EXIST",
        "headers": [],
    }
    response = await web_qr_search(
        request=Request(scope),
        qr_id="QR-DOESNT-EXIST",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    assert b"No QR with id" in response.body
    assert b"QR-DOESNT-EXIST" in response.body


async def test_web_qr_search_renders_free_qr_without_device_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FREE QR → no NetBox call, audit history rendered, no device card."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.qr import QR, QRStatus

    free_qr = QR(
        id="QR-FREE-1",
        batch_id=uuid4(),
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )

    class _FakeQRCodeRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_by_id(self, _qr_id: str) -> QR:
            return free_qr

    netbox_called = False

    class _FakeDeviceService:
        def __init__(self, _client: object) -> None: ...

        async def get_device(self, _device_id: int) -> Any:
            nonlocal netbox_called
            netbox_called = True
            raise AssertionError("DeviceService.get_device must not be called for FREE QRs")

    class _FakeAuditRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(self, **_kwargs: object) -> tuple[list[Any], bool]:
            return [], False

    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeQRCodeRepo)
    monkeypatch.setattr("app.web.router.DeviceService", _FakeDeviceService)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeAuditRepo)
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/qr/search",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"qr_id=QR-FREE-1",
        "headers": [],
    }
    response = await web_qr_search(
        request=Request(scope),
        qr_id="QR-FREE-1",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    assert b"QR-FREE-1" in response.body
    assert b"free" in response.body
    assert not netbox_called


async def test_web_qr_search_renders_bound_qr_with_stale_device_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BOUND QR but bound device id missing in NetBox → device_error flash
    surfaced (stale-binding diagnostic) instead of swallowing the 404."""
    _set_env(monkeypatch)
    from datetime import datetime
    from uuid import uuid4

    from app.domain.qr import QR, QRStatus
    from app.netbox.errors import NetBoxNotFound

    bound_qr = QR(
        id="QR-BOUND-1",
        batch_id=uuid4(),
        status=QRStatus.BOUND,
        bound_to_device_id=999,
        bound_at=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
        bound_by_email="engineer@example.com",
        retired_at=None,
        retired_reason=None,
    )

    class _FakeQRCodeRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_by_id(self, _qr_id: str) -> QR:
            return bound_qr

    class _FakeDeviceService:
        def __init__(self, _client: object) -> None: ...

        async def get_device(self, _device_id: int) -> Any:
            raise NetBoxNotFound("device 999")

    class _FakeAuditRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(self, **_kwargs: object) -> tuple[list[Any], bool]:
            return [], False

    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeQRCodeRepo)
    monkeypatch.setattr("app.web.router.DeviceService", _FakeDeviceService)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeAuditRepo)
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/qr/search",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"qr_id=QR-BOUND-1",
        "headers": [],
    }
    response = await web_qr_search(
        request=Request(scope),
        qr_id="QR-BOUND-1",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    assert b"QR-BOUND-1" in response.body
    assert b"stale binding" in response.body


async def test_devices_decommission_form_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /web/devices/decommission`` returns the form, including any
    flash banner passed via query params."""
    _set_env(monkeypatch)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/devices/decommission",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await devices_decommission_form(
        request=Request(scope),
        user=_admin_user(),
        flash="Device 42 decommissioned",
        flash_kind="info",
    )
    assert response.status_code == 200
    assert b"Device 42 decommissioned" in response.body
    assert b"Decommission device" in response.body


# --- /web/users/ list + detail (over a stubbed KeycloakAdminClient) --------


def _patch_keycloak_admin_client(
    monkeypatch: pytest.MonkeyPatch, *, list_impl: Any = None, get_impl: Any = None
) -> None:
    class _FakeAdminClient:
        async def list_users(self, **kwargs: object) -> Any:
            if list_impl is None:
                raise AssertionError("list_users not stubbed for this test")
            return await list_impl(**kwargs)

        async def get_user(self, user_id: str) -> Any:
            if get_impl is None:
                raise AssertionError("get_user not stubbed for this test")
            return await get_impl(user_id)

    monkeypatch.setattr(
        "app.web.router.get_keycloak_admin_client", lambda: _FakeAdminClient()
    )


async def test_web_users_list_renders_users_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_users`` returns rows → template renders them in the table."""
    _set_env(monkeypatch)
    from app.auth.keycloak_admin import KeycloakUser

    async def _list(**_kwargs: object) -> tuple[list[KeycloakUser], bool]:
        return (
            [
                KeycloakUser(
                    id="u-1",
                    username="alice",
                    email="alice@example.com",
                    first_name="Alice",
                    last_name=None,
                    enabled=True,
                    created_at=None,
                )
            ],
            False,
        )

    _patch_keycloak_admin_client(monkeypatch, list_impl=_list)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/users/",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_users_list(
        request=Request(scope),
        page=1,
        search=None,
        user=_admin_user(),
    )
    assert response.status_code == 200
    assert b"alice" in response.body
    assert b"alice@example.com" in response.body


async def test_web_users_list_renders_not_configured_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KeycloakAdminNotConfigured`` → template surfaces the
    "set KEYCLOAK_ADMIN_CLIENT_*" notice instead of 500-ing."""
    _set_env(monkeypatch)
    from app.auth.keycloak_admin import KeycloakAdminNotConfigured

    async def _list(**_kwargs: object) -> Any:
        raise KeycloakAdminNotConfigured("secret missing")

    _patch_keycloak_admin_client(monkeypatch, list_impl=_list)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/users/",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_users_list(
        request=Request(scope),
        page=1,
        search=None,
        user=_admin_user(),
    )
    assert response.status_code == 200
    assert b"Keycloak admin client not configured" in response.body
    assert b"KEYCLOAK_ADMIN_CLIENT_SECRET" in response.body


async def test_web_users_detail_renders_custom_404_for_unknown_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_user`` returns ``None`` → custom 404 HTML page (mirrors
    the batches-detail not-found flow from Sprint 8b)."""
    _set_env(monkeypatch)

    async def _get(_user_id: str) -> None:
        return None

    _patch_keycloak_admin_client(monkeypatch, get_impl=_get)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/users/ghost",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_users_detail(
        request=Request(scope),
        user_id="ghost",
        user=_admin_user(),
    )
    assert response.status_code == 404
    assert b"Not found" in response.body


# --- _parse_optional_form_int + web_batches_labels_pdf (2026-06-07 fixes) ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", None),
        ("   ", None),
        ("42", 42),
        ("  7 ", 7),
    ],
)
def test_parse_optional_form_int_accepts_blank_and_positive(
    raw: str, expected: int | None
) -> None:
    """Empty / whitespace → None (the column is nullable). Non-empty must
    parse as positive int."""
    assert _parse_optional_form_int(raw, field="intended_site_id") == expected


@pytest.mark.parametrize("raw", ["abc", "1.5", "-3", "0"])
def test_parse_optional_form_int_rejects_invalid_with_422(raw: str) -> None:
    """Non-int / non-positive → ``HTTPException(422)`` with a clear
    "must be a positive integer or blank" message instead of a silent
    server error."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _parse_optional_form_int(raw, field="intended_site_id")
    assert exc.value.status_code == 422
    assert "intended_site_id" in exc.value.detail


async def test_web_batches_create_accepts_blank_optional_id_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production bug 2026-06-07: empty intended_*_id form fields 422'd
    because the handler declared them as ``int | None`` and the browser
    submits "" rather than omitting the key. They're ``str`` now, parsed
    via ``_parse_optional_form_int`` → all-blank submit creates cleanly."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from uuid import uuid4

    from app.domain.qr import QRBatch

    captured: dict[str, Any] = {}
    new_batch_id = uuid4()

    class _FakeRepo:
        def __init__(self, _session: object) -> None: ...

    class _FakeGenerationService:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def generate_batch(self, payload: Any, user: Any) -> QRBatch:
            captured["site"] = payload.intended_site_id
            captured["location"] = payload.intended_location_id
            captured["rack"] = payload.intended_rack_id
            return QRBatch(
                id=new_batch_id,
                created_at=datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC),
                created_by_email=user.email,
                created_by_keycloak_id=UUID(user.sub),
                count=payload.count,
                intended_site_id=None,
                intended_location_id=None,
                intended_rack_id=None,
                comment=None,
            )

    monkeypatch.setattr("app.web.router.QRBatchRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeRepo)
    monkeypatch.setattr("app.web.router.QRGenerationService", _FakeGenerationService)

    class _FakeSession:
        async def commit(self) -> None: ...

    response = await web_batches_create(
        count=10,
        csrf="test-csrf-token",
        comment="",
        intended_site_id="",
        intended_location_id="",
        intended_rack_id="",
        user=_admin_user(),
        session=_FakeSession(),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert captured == {"site": None, "location": None, "rack": None}


async def test_web_batches_labels_pdf_returns_pdf_bytes_for_known_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookie-authed PDF endpoint (2026-06-07 fix). Browser couldn't hit
    the bearer-only /api/v1/admin/batches/{id}/labels.pdf; this web shim
    cookie-auths and renders the same PDF."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from app.domain.qr import QRBatch

    batch_id = uuid4()
    batch = QRBatch(
        id=batch_id,
        created_at=datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC),
        created_by_email="alice@example.com",
        created_by_keycloak_id=_USER_SUB,
        count=2,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )

    class _FakeBatchRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_by_id(self, _bid: UUID) -> QRBatch:
            return batch

    class _FakeCodeRepo:
        def __init__(self, _session: object) -> None: ...

        async def find_by_batch_id(self, _bid: UUID) -> list[Any]:
            return []

    monkeypatch.setattr("app.web.router.QRBatchRepository", _FakeBatchRepo)
    monkeypatch.setattr("app.web.router.QRCodeRepository", _FakeCodeRepo)
    monkeypatch.setattr(
        "app.services.pdf_labels.render_batch_labels_pdf",
        lambda batch, codes: b"%PDF-FAKE-BYTES",
    )

    response = await web_batches_labels_pdf(
        batch_id=batch_id,
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert response.body == b"%PDF-FAKE-BYTES"
    assert f'filename="batch-{batch_id}.pdf"' in response.headers["content-disposition"]


async def test_web_batches_labels_pdf_returns_404_for_unknown_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown batch → 404 (raised, not a flash) so curl / external
    monitors see the correct status."""
    _set_env(monkeypatch)
    from uuid import uuid4

    from fastapi import HTTPException

    class _FakeBatchRepo:
        def __init__(self, _session: object) -> None: ...

        async def get_by_id(self, _bid: UUID) -> None:
            return None

    monkeypatch.setattr("app.web.router.QRBatchRepository", _FakeBatchRepo)

    with pytest.raises(HTTPException) as exc:
        await web_batches_labels_pdf(
            batch_id=uuid4(),
            user=_admin_user(),
            session=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404


# Use _admin_action_request so the helper isn't dead code (CI-side flake guard).
_ = _admin_action_request


async def test_web_audit_csv_delegates_to_json_handler_with_auth_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web shim must build an ``AuthUser`` from the cookie + active shift
    and pass it to the JSON ``query_audit_log_csv`` so the
    ``audit.export_csv`` audit-of-audits row writes with proper attribution
    (2026-06-07 fix — original /api/v1 link was bearer-only and 401'd in
    the browser, same class as the PDF download bug)."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from fastapi.responses import StreamingResponse

    captured: dict[str, Any] = {}

    async def _fake_csv(**kwargs: object) -> StreamingResponse:
        captured.update(kwargs)
        return StreamingResponse(iter([b"timestamp,user\n"]), media_type="text/csv")

    monkeypatch.setattr("app.web.router.query_audit_log_csv", _fake_csv)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/audit/csv",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_audit_csv(
        request=Request(scope),
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type="qr",
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page_size=500,
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.media_type == "text/csv"
    auth_user = captured["user"]
    assert auth_user.email == "alice@example.com"
    assert auth_user.sub == str(_USER_SUB)
    # Shift session id was set by _patch_admin_shift_lookup
    assert auth_user.shift_session_id is not None
    # Filters passed through verbatim
    assert captured["entity_type"] == "qr"
    assert captured["page_size"] == 500


def test_web_audit_csv_route_declared_before_audit_detail() -> None:
    """Regression guard mirroring ``test_batches_new_route_declared_...``:
    ``GET /audit/csv`` MUST come before ``GET /audit/{audit_id}``, else
    FastAPI tries to parse ``"csv"`` as an ``int`` and 422s."""
    from app.web.router import router

    paths_in_order = [
        getattr(r, "path", None) for r in router.routes if getattr(r, "path", None)
    ]
    csv_idx = paths_in_order.index("/audit/csv")
    detail_idx = paths_in_order.index("/audit/{audit_id}")
    assert csv_idx < detail_idx, (
        f"/audit/csv must be registered before /audit/{{audit_id}}; "
        f"got csv at {csv_idx}, detail at {detail_idx}"
    )


def test_batches_new_route_declared_before_batches_detail() -> None:
    """Regression guard: FastAPI dispatches routes in registration order, so
    ``GET /batches/new`` MUST come before ``GET /batches/{batch_id}``,
    otherwise FastAPI tries to parse ``"new"`` as a UUID and 422s.

    Hit in production 2026-06-05; the fix was to reorder the handlers in
    router.py. This test pins the order so a future refactor moving the
    new-batch handler back down silently re-breaks the form.
    """
    from app.web.router import router

    paths_in_order = [
        getattr(r, "path", None) for r in router.routes if getattr(r, "path", None)
    ]
    new_idx = paths_in_order.index("/batches/new")
    detail_idx = paths_in_order.index("/batches/{batch_id}")
    assert new_idx < detail_idx, (
        f"/batches/new must be registered before /batches/{{batch_id}}; "
        f"got new at {new_idx}, detail at {detail_idx}"
    )

# ---------- /web/devices/{id} detail + comments (Sprint 9 Task 2) -----------


async def test_web_devices_detail_renders_device_with_audit_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: device fetch + audit query both succeed → page renders."""
    _set_env(monkeypatch)

    from app.domain.audit import AuditLogEntry, AuditResult
    from app.services.device import DeviceData, DeviceResponse, ObjectRef, StatusRef

    device = DeviceResponse(
        data=DeviceData(
            id=42,
            name="core-sw-01",
            status=StatusRef(value="active", label="Active"),
            site=ObjectRef(id=1, name="DC-1"),
            rack=ObjectRef(id=7, name="R-14"),
            position=10,
            serial="ABC123",
            asset_tag="A-9",
            comments="",
            custom_fields={},
        ),
        version="2026-06-08T12:00:00Z",
    )

    class _FakeDeviceService:
        def __init__(self, _client: object) -> None: ...

        async def get_device(self, device_id: int) -> Any:
            assert device_id == 42
            return device

    audit_row = AuditLogEntry(
        request_id=UUID("11111111-1111-1111-1111-111111111111"),
        timestamp=datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
        user_email="alice@example.com",
        user_keycloak_id=_USER_SUB,
        session_id=None,
        operation="device.update",
        entity_type="device",
        entity_id="42",
        before_json={},
        after_json={},
        result=AuditResult.SUCCESS,
        id=99,
    )

    class _FakeAuditRepo:
        def __init__(self, _session: object) -> None: ...

        async def query(self, **_kwargs: object) -> tuple[list[Any], bool]:
            return [audit_row], False

    monkeypatch.setattr("app.web.router.DeviceService", _FakeDeviceService)
    monkeypatch.setattr("app.web.router.AuditLogRepository", _FakeAuditRepo)
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/devices/42",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_devices_detail(
        request=Request(scope),
        device_id=42,
        flash=None,
        flash_kind=None,
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    body = bytes(response.body)
    assert b"core-sw-01" in body
    assert b"device.update" in body
    assert b"Add a comment" in body


async def test_web_devices_detail_returns_404_for_unknown_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NetBoxNotFound → 404 HTML page (mirrors batches/detail unknown-id)."""
    _set_env(monkeypatch)
    from app.netbox.errors import NetBoxNotFound

    class _FakeDeviceService:
        def __init__(self, _client: object) -> None: ...

        async def get_device(self, _device_id: int) -> Any:
            raise NetBoxNotFound("device 999")

    monkeypatch.setattr("app.web.router.DeviceService", _FakeDeviceService)
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/devices/999",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_devices_detail(
        request=Request(scope),
        device_id=999,
        flash=None,
        flash_kind=None,
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 404
    assert b"Not found" in response.body


async def test_web_devices_add_comment_redirects_with_flash_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CSRF-protected form post → delegates to add_comment → 303 to detail
    page with success flash."""
    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from contextlib import asynccontextmanager

    captured: dict[str, Any] = {}

    async def _fake_add_comment(**kwargs: object) -> object:
        captured.update(kwargs)
        return None

    monkeypatch.setattr("app.web.router.add_comment", _fake_add_comment)
    monkeypatch.setattr("app.web.router.NetBoxWriteService", lambda *a, **kw: object())
    monkeypatch.setattr("app.web.router.AuditLogRepository", lambda _s: object())
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())
    monkeypatch.setattr(
        "app.web.router.get_comment_service", lambda **_kw: object()
    )

    @asynccontextmanager
    async def _fake_cm():
        yield object()

    monkeypatch.setattr("app.web.router.get_sessionmaker", lambda: _fake_cm)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/web/devices/42/comments",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_devices_add_comment(
        request=Request(scope),
        device_id=42,
        comment="PSU1 amber LED",
        csrf="test-csrf-token",
        user=_admin_user(),
        session=object(),  # type: ignore[arg-type]
        sessionmaker=cast(object, _fake_cm),  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/devices/42?")
    assert "flash=Comment+added" in response.headers["location"]
    assert captured["device_id"] == 42
    assert captured["request"].comment == "PSU1 amber LED"


async def test_web_devices_add_comment_rejects_csrf_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong csrf token → HTTPException(403) before any side effects."""
    from fastapi import HTTPException

    _set_env(monkeypatch)
    _patch_admin_shift_lookup(monkeypatch)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_cm():
        yield object()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/web/devices/42/comments",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    with pytest.raises(HTTPException) as exc:
        await web_devices_add_comment(
            request=Request(scope),
            device_id=42,
            comment="anything",
            csrf="WRONG-TOKEN",
            user=_admin_user(),
            session=object(),  # type: ignore[arg-type]
            sessionmaker=cast(object, _fake_cm),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


async def test_web_devices_search_renders_empty_form_when_no_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No filters submitted → form-only page, no NetBox call."""
    _set_env(monkeypatch)

    netbox_called = False

    class _FakeDeviceService:
        def __init__(self, _client: object) -> None: ...

        async def search(self, **_kwargs: object) -> Any:
            nonlocal netbox_called
            netbox_called = True
            raise AssertionError("search must not be called when no filters are set")

    monkeypatch.setattr("app.web.router.DeviceService", _FakeDeviceService)
    monkeypatch.setattr("app.web.router.get_netbox_client", lambda: object())

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/web/devices/search",
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
        "headers": [],
    }
    response = await web_devices_search(
        request=Request(scope),
        name=None,
        asset_tag=None,
        serial=None,
        site_id=None,
        rack_id=None,
        page=1,
        user=_admin_user(),
    )
    assert response.status_code == 200
    assert b"Device search" in response.body
    assert not netbox_called


def test_web_devices_search_route_declared_before_web_devices_detail() -> None:
    """Regression guard: ``/web/devices/search`` must come before the
    int-typed ``/web/devices/{device_id}``. Otherwise FastAPI tries to
    parse ``"search"`` as ``int`` and 422s.
    """
    from app.web.router import router

    paths = [getattr(r, "path", None) for r in router.routes if getattr(r, "path", None)]
    search_idx = paths.index("/devices/search")
    detail_idx = paths.index("/devices/{device_id}")
    assert search_idx < detail_idx, (
        f"/devices/search must register before /devices/{{device_id}}; "
        f"got search={search_idx}, detail={detail_idx}"
    )


# Suppress unused-import warnings for symbols only referenced inside scopes.
_ = (Fernet, AsyncIterator, jwt, SESSION_COOKIE_NAME, Any)
