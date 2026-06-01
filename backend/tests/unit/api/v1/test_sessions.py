"""Endpoint tests for /api/v1/sessions/{start,end,active}.

Mirrors ``test_qr_retire.py``: handler logic via direct ``await``, full ASGI
stack via ``AsyncClient`` for routing + role gating + body validation. The
``ShiftSessionService`` is stubbed; its own units live in
``tests/unit/services/test_shift_session.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.sessions import (
    SessionEndRequest,
    SessionResponse,
    SessionStartRequest,
    end_session,
    get_active_session,
    get_shift_session_service,
    start_session,
)
from app.auth.dependencies import AuthUser
from app.domain.shift_session import ShiftEndReason, ShiftSession
from app.main import app
from app.services.shift_session import (
    NoActiveShift,
    SessionAlreadyActive,
    ShiftSessionService,
)
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_USER_A = UUID("11111111-1111-1111-1111-111111111111")
_NOW = datetime(2026, 5, 29, 9, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 29, 17, 0, 0, tzinfo=UTC)


def _active(
    *,
    session_id: UUID | None = None,
    user_keycloak_id: UUID = _USER_A,
    user_email: str = "alice@example.com",
    tablet_id: str = "tablet-01",
) -> ShiftSession:
    return ShiftSession(
        id=session_id or uuid4(),
        user_email=user_email,
        user_keycloak_id=user_keycloak_id,
        shift_start_at=_NOW,
        shift_end_at=None,
        tablet_id=tablet_id,
        end_reason=None,
    )


def _ended(
    *,
    session_id: UUID | None = None,
    reason: ShiftEndReason = ShiftEndReason.MANUAL,
) -> ShiftSession:
    return ShiftSession(
        id=session_id or uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_A,
        shift_start_at=_NOW,
        shift_end_at=_LATER,
        tablet_id="tablet-01",
        end_reason=reason,
    )


class _StubShiftSessionService:
    """Stand-in for ``ShiftSessionService`` — records calls, returns canned values."""

    def __init__(
        self,
        *,
        start_return: ShiftSession | None = None,
        start_error: Exception | None = None,
        end_return: ShiftSession | None = None,
        end_error: Exception | None = None,
        active_return: ShiftSession | None = None,
    ) -> None:
        self._start_return = start_return
        self._start_error = start_error
        self._end_return = end_return
        self._end_error = end_error
        self._active_return = active_return
        self.start_calls: list[dict[str, object]] = []
        self.end_calls: list[dict[str, object]] = []
        self.get_active_calls: list[UUID] = []

    async def start(
        self,
        *,
        user_email: str,
        user_keycloak_id: UUID,
        tablet_id: str,
    ) -> ShiftSession:
        self.start_calls.append(
            {
                "user_email": user_email,
                "user_keycloak_id": user_keycloak_id,
                "tablet_id": tablet_id,
            }
        )
        if self._start_error is not None:
            raise self._start_error
        assert self._start_return is not None
        return self._start_return

    async def end(
        self,
        *,
        user_keycloak_id: UUID,
        reason: ShiftEndReason,
    ) -> ShiftSession:
        self.end_calls.append({"user_keycloak_id": user_keycloak_id, "reason": reason})
        if self._end_error is not None:
            raise self._end_error
        assert self._end_return is not None
        return self._end_return

    async def get_active(self, user_keycloak_id: UUID) -> ShiftSession | None:
        self.get_active_calls.append(user_keycloak_id)
        return self._active_return


# ---------- handler logic (direct await) ----------


async def test_start_session_handler_returns_session_on_happy_path(
    session: AsyncSession,
) -> None:
    started = _active(tablet_id="tablet-42")
    stub = _StubShiftSessionService(start_return=started)

    result = await start_session(
        request=SessionStartRequest(tablet_id="tablet-42"),
        user=make_user("dcinv-mobile-user"),
        service=cast(ShiftSessionService, stub),
    )

    assert isinstance(result, SessionResponse)
    assert result.session is not None
    assert result.session.id == started.id
    assert result.session.tablet_id == "tablet-42"
    assert result.session.shift_end_at is None
    # Service receives the user's identity + tablet.
    assert stub.start_calls == [
        {
            "user_email": "alice@example.com",
            "user_keycloak_id": _USER_A,
            "tablet_id": "tablet-42",
        }
    ]


async def test_start_session_handler_returns_409_when_session_already_active(
    session: AsyncSession,
) -> None:
    existing = _active(session_id=UUID("99999999-9999-9999-9999-999999999999"))
    stub = _StubShiftSessionService(start_error=SessionAlreadyActive(existing))

    result = await start_session(
        request=SessionStartRequest(tablet_id="tablet-01"),
        user=make_user("dcinv-mobile-user"),
        service=cast(ShiftSessionService, stub),
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "SESSION_ALREADY_ACTIVE"
    assert body["error"]["active"]["id"] == str(existing.id)
    assert body["error"]["active"]["tablet_id"] == existing.tablet_id


async def test_end_session_handler_returns_session_on_happy_path_manual(
    session: AsyncSession,
) -> None:
    ended = _ended(reason=ShiftEndReason.MANUAL)
    stub = _StubShiftSessionService(end_return=ended)

    result = await end_session(
        request=SessionEndRequest(end_reason="manual"),
        user=make_user("dcinv-mobile-user"),
        service=cast(ShiftSessionService, stub),
    )

    assert isinstance(result, SessionResponse)
    assert result.session is not None
    assert result.session.id == ended.id
    assert result.session.end_reason is ShiftEndReason.MANUAL
    assert result.session.shift_end_at == _LATER
    assert stub.end_calls == [{"user_keycloak_id": _USER_A, "reason": ShiftEndReason.MANUAL}]


async def test_end_session_handler_returns_session_on_happy_path_inactivity_timeout(
    session: AsyncSession,
) -> None:
    ended = _ended(reason=ShiftEndReason.INACTIVITY_TIMEOUT)
    stub = _StubShiftSessionService(end_return=ended)

    result = await end_session(
        request=SessionEndRequest(end_reason="inactivity_timeout"),
        user=make_user("dcinv-mobile-user"),
        service=cast(ShiftSessionService, stub),
    )

    assert isinstance(result, SessionResponse)
    assert result.session is not None
    assert result.session.end_reason is ShiftEndReason.INACTIVITY_TIMEOUT
    assert stub.end_calls == [
        {"user_keycloak_id": _USER_A, "reason": ShiftEndReason.INACTIVITY_TIMEOUT}
    ]


async def test_end_session_handler_returns_409_when_no_active_shift(
    session: AsyncSession,
) -> None:
    stub = _StubShiftSessionService(end_error=NoActiveShift(_USER_A))

    result = await end_session(
        request=SessionEndRequest(end_reason="manual"),
        user=make_user("dcinv-mobile-user"),
        service=cast(ShiftSessionService, stub),
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_get_active_session_handler_returns_active_when_present(
    session: AsyncSession,
) -> None:
    active = _active()
    stub = _StubShiftSessionService(active_return=active)

    result = await get_active_session(
        user=make_user("dcinv-mobile-user"),
        service=cast(ShiftSessionService, stub),
    )

    assert isinstance(result, SessionResponse)
    assert result.session is not None
    assert result.session.id == active.id
    assert stub.get_active_calls == [_USER_A]


async def test_get_active_session_handler_returns_null_session_when_absent(
    session: AsyncSession,
) -> None:
    # No-shift case bypasses the response_model via a raw JSONResponse so the
    # wire stays ``{"session": null}`` instead of being collapsed to ``{}`` by
    # response_model_exclude_none.
    stub = _StubShiftSessionService(active_return=None)

    result = await get_active_session(
        user=make_user("dcinv-mobile-user"),
        service=cast(ShiftSessionService, stub),
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 200
    assert json.loads(bytes(result.body)) == {"session": None}


# ---------- routing / role / validation (AsyncClient) ----------


async def test_post_start_returns_200_on_happy_path(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        start_return=_active(tablet_id="tablet-77")
    )

    resp = await client.post("/api/v1/sessions/start", json={"tablet_id": "tablet-77"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["tablet_id"] == "tablet-77"
    # response_model_exclude_none drops the closed-shift fields for an active row.
    assert "shift_end_at" not in body["session"]
    assert "end_reason" not in body["session"]


async def test_post_start_returns_403_without_mobile_user_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        start_return=_active()
    )

    resp = await client.post("/api/v1/sessions/start", json={"tablet_id": "tablet-01"})

    assert resp.status_code == 403


async def test_post_start_returns_422_for_missing_tablet_id(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        start_return=_active()
    )

    resp = await client.post("/api/v1/sessions/start", json={})

    assert resp.status_code == 422


async def test_post_start_returns_422_for_empty_tablet_id(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        start_return=_active()
    )

    resp = await client.post("/api/v1/sessions/start", json={"tablet_id": ""})

    assert resp.status_code == 422


async def test_post_start_returns_422_for_extra_body_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        start_return=_active()
    )

    resp = await client.post(
        "/api/v1/sessions/start", json={"tablet_id": "tablet-01", "rogue": True}
    )

    assert resp.status_code == 422


async def test_post_start_returns_409_with_active_session_body(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    existing = _active(session_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        start_error=SessionAlreadyActive(existing)
    )

    resp = await client.post("/api/v1/sessions/start", json={"tablet_id": "tablet-01"})

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "SESSION_ALREADY_ACTIVE"
    assert body["error"]["active"]["id"] == str(existing.id)


async def test_post_end_returns_200_on_happy_path(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        end_return=_ended()
    )

    resp = await client.post("/api/v1/sessions/end", json={"end_reason": "manual"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["end_reason"] == "manual"


async def test_post_end_returns_422_for_admin_force_close_reason(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    # Decision E: the wire format restricts end_reason to {manual,
    # inactivity_timeout}. admin_force_close is reserved for the Sprint 7+
    # admin endpoint and MUST be rejected at the schema layer.
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        end_return=_ended()
    )

    resp = await client.post("/api/v1/sessions/end", json={"end_reason": "admin_force_close"})

    assert resp.status_code == 422


async def test_post_end_returns_422_for_unknown_reason(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        end_return=_ended()
    )

    resp = await client.post("/api/v1/sessions/end", json={"end_reason": "bogus"})

    assert resp.status_code == 422


async def test_post_end_returns_422_for_missing_end_reason(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        end_return=_ended()
    )

    resp = await client.post("/api/v1/sessions/end", json={})

    assert resp.status_code == 422


async def test_post_end_returns_422_for_extra_body_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        end_return=_ended()
    )

    resp = await client.post("/api/v1/sessions/end", json={"end_reason": "manual", "rogue": True})

    assert resp.status_code == 422


async def test_post_end_returns_403_without_mobile_user_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        end_return=_ended()
    )

    resp = await client.post("/api/v1/sessions/end", json={"end_reason": "manual"})

    assert resp.status_code == 403


async def test_post_end_returns_409_when_no_active_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        end_error=NoActiveShift(_USER_A)
    )

    resp = await client.post("/api/v1/sessions/end", json={"end_reason": "manual"})

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_get_active_returns_200_with_session_when_present(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    active = _active(session_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        active_return=active
    )

    resp = await client.get("/api/v1/sessions/active")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["id"] == str(active.id)
    assert "shift_end_at" not in body["session"]


async def test_get_active_returns_200_with_null_session_when_absent(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        active_return=None
    )

    resp = await client.get("/api/v1/sessions/active")

    assert resp.status_code == 200
    # `session: null` is the explicit shape — confirms mobile can do a single
    # null-check rather than a try/except 404 path.
    assert resp.json() == {"session": None}


async def test_get_active_returns_403_without_mobile_user_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    app.dependency_overrides[get_shift_session_service] = lambda: _StubShiftSessionService(
        active_return=None
    )

    resp = await client.get("/api/v1/sessions/active")

    assert resp.status_code == 403


# ---------- DI factory ----------


async def test_get_shift_session_service_builds_a_shift_session_service(
    session: AsyncSession,
) -> None:
    assert isinstance(get_shift_session_service(session=session), ShiftSessionService)
