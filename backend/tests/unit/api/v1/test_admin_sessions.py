"""Unit tests for /api/v1/admin/sessions list + force-close (Sprint 7 Task 3).

Mirrors test_admin_audit.py's split: handler-direct-await for happy/no-op/404
paths + audit-row inspection, AsyncClient for role + active-shift gating +
body validation (Pydantic Field constraints).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.sessions import (
    AdminSessionStartRequest,
    ForceCloseRequest,
    ShiftSessionListResponse,
    ShiftSessionResponse,
    force_close_session,
    list_sessions,
    start_admin_session,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_sessionmaker
from app.domain.shift_session import ShiftEndReason, ShiftSession
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_REQUEST_ID = "33333333-3333-3333-3333-333333333333"
_SHIFT_SESSION_ID = UUID("33333333-3333-3333-3333-333333333333")
_TARGET_USER = UUID("44444444-4444-4444-4444-444444444444")


def _admin_user(*, shift_session_id: UUID | None = _SHIFT_SESSION_ID) -> AuthUser:
    """Admin AuthUser with shift_session_id populated as the dep layer would."""
    return dataclasses.replace(make_user("dcinv-admin"), shift_session_id=shift_session_id)


def _bind_request_id() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=_REQUEST_ID)


def _active_target() -> ShiftSession:
    return ShiftSession(
        id=uuid4(),
        user_email="bob@example.com",
        user_keycloak_id=_TARGET_USER,
        shift_start_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="tablet-bob",
        end_reason=None,
    )


# ---------- list_sessions (direct await) -------------------------------------


async def test_list_sessions_returns_envelope_with_pagination_defaults(
    session: AsyncSession,
) -> None:
    # Seed two ad-hoc target shifts (the conftest already seeded one for the
    # admin's own active shift via _truncate's seed_default_active_shift).
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(_active_target())
        await repo.insert(
            dataclasses.replace(
                _active_target(),
                id=uuid4(),
                user_keycloak_id=UUID("55555555-5555-5555-5555-555555555555"),
                tablet_id="tablet-c",
            )
        )
        await db.commit()

    result = await list_sessions(
        user_keycloak_id=None,
        from_=None,
        to=None,
        active_only=False,
        page=1,
        page_size=20,
        user=_admin_user(),
        session=session,
    )

    assert isinstance(result, ShiftSessionListResponse)
    assert result.page == 1
    assert result.page_size == 20
    assert result.has_more is False
    # Three rows: two seeded here + the admin's own canonical shift from the
    # _truncate fixture (decision 8: list endpoint sees its own active shift).
    assert len(result.results) == 3


async def test_list_sessions_filters_by_active_only(session: AsyncSession) -> None:
    target_active = _active_target()
    target_ended = dataclasses.replace(
        _active_target(),
        id=uuid4(),
        user_keycloak_id=UUID("55555555-5555-5555-5555-555555555555"),
        tablet_id="tablet-c",
    ).end(reason=ShiftEndReason.MANUAL, at=datetime(2026, 6, 1, 17, 0, 0, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(target_active)
        await repo.insert(target_ended)
        await db.commit()

    result = await list_sessions(
        user_keycloak_id=None,
        from_=None,
        to=None,
        active_only=True,
        page=1,
        page_size=20,
        user=_admin_user(),
        session=session,
    )

    returned_ids = {r.id for r in result.results}
    assert target_active.id in returned_ids
    assert target_ended.id not in returned_ids


async def test_list_sessions_does_not_write_audit_row(session: AsyncSession) -> None:
    """Decision 8: shift-listing is operational, not a §5.4.6 sensitive read."""
    _bind_request_id()
    await list_sessions(
        user_keycloak_id=None,
        from_=None,
        to=None,
        active_only=False,
        page=1,
        page_size=20,
        user=_admin_user(),
        session=session,
    )

    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE operation = 'shift_session.list'")
        )
        assert rows.scalar_one() == 0


# ---------- force_close_session (direct await) -------------------------------


async def test_force_close_session_ends_active_shift_and_writes_success_audit_row(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    target = _active_target()
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(target)
        await db.commit()

    result = await force_close_session(
        session_id=target.id,
        body=ForceCloseRequest(reason="Engineer left without ending shift"),
        user=_admin_user(),
        session=session,
    )

    assert isinstance(result, ShiftSessionResponse)
    assert result.id == target.id
    assert result.end_reason is ShiftEndReason.FORCED
    assert result.shift_end_at is not None

    # Target row is persisted as ended.
    async with get_sessionmaker()() as db:
        persisted = await ShiftSessionRepository(db).get_by_id(target.id)
    assert persisted is not None and not persisted.is_active
    assert persisted.end_reason is ShiftEndReason.FORCED

    # Audit row carries the reason in after_json + result=SUCCESS.
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text(
                "SELECT result, after_json, before_json, entity_id, operation, session_id"
                " FROM audit_log WHERE operation = 'shift_session.force_close'"
            )
        )
        records = rows.fetchall()
    assert len(records) == 1
    res, after, before, eid, op, sess = records[0]
    assert res == "success"
    assert op == "shift_session.force_close"
    assert eid == str(target.id)
    assert after["reason"] == "Engineer left without ending shift"
    assert after["end_reason"] == "forced"
    assert "no_op" not in after  # only present on the idempotent path
    assert before["active"] is True
    # Admin's own shift, not the target's, attributed in session_id.
    assert sess == _SHIFT_SESSION_ID


async def test_force_close_session_idempotent_no_op_on_already_ended(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    already_ended = _active_target().end(
        reason=ShiftEndReason.MANUAL, at=datetime(2026, 6, 1, 17, 0, 0, tzinfo=UTC)
    )
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(already_ended)
        await db.commit()

    result = await force_close_session(
        session_id=already_ended.id,
        body=ForceCloseRequest(reason="Late attempt after manual end"),
        user=_admin_user(),
        session=session,
    )

    # Returns the existing ended state — end_reason stays MANUAL, NOT FORCED.
    assert result.end_reason is ShiftEndReason.MANUAL

    # Target row was NOT updated.
    async with get_sessionmaker()() as db:
        persisted = await ShiftSessionRepository(db).get_by_id(already_ended.id)
    assert persisted is not None
    assert persisted.end_reason is ShiftEndReason.MANUAL

    # Audit row carries result=CONFLICT + after_json.no_op=True.
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text(
                "SELECT result, after_json, before_json"
                " FROM audit_log WHERE operation = 'shift_session.force_close'"
            )
        )
        records = rows.fetchall()
    assert len(records) == 1
    res, after, before = records[0]
    assert res == "conflict"
    assert after["no_op"] is True
    assert after["reason"] == "Late attempt after manual end"
    assert before["active"] is False


async def test_force_close_session_returns_404_for_unknown_id_with_no_audit_row(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    unknown = uuid4()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await force_close_session(
            session_id=unknown,
            body=ForceCloseRequest(reason="anything"),
            user=_admin_user(),
            session=session,
        )
    assert exc.value.status_code == 404

    # No audit row written — admin typo is not a state-change conflict.
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text("SELECT COUNT(*) FROM audit_log" " WHERE operation = 'shift_session.force_close'")
        )
        assert rows.scalar_one() == 0


# ---------- routing + role + active-shift gating + body validation -----------


async def test_get_admin_sessions_returns_403_without_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get("/api/v1/admin/sessions")
    assert resp.status_code == 403


async def test_get_admin_sessions_returns_409_no_active_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/sessions")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_get_admin_sessions_returns_200_with_envelope(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert {"results", "page", "page_size", "has_more"} <= set(body.keys())


async def test_get_admin_sessions_rejects_page_size_above_cap(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/sessions?page_size=101")
    assert resp.status_code == 422


async def test_post_force_close_returns_403_without_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.post(f"/api/v1/admin/sessions/{uuid4()}/force-close", json={"reason": "x"})
    assert resp.status_code == 403


async def test_post_force_close_returns_409_no_active_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")
    resp = await client.post(f"/api/v1/admin/sessions/{uuid4()}/force-close", json={"reason": "x"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_post_force_close_rejects_empty_reason_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post(f"/api/v1/admin/sessions/{uuid4()}/force-close", json={"reason": ""})
    assert resp.status_code == 422


async def test_post_force_close_rejects_reason_over_500_chars_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post(
        f"/api/v1/admin/sessions/{uuid4()}/force-close", json={"reason": "x" * 501}
    )
    assert resp.status_code == 422


async def test_post_force_close_rejects_missing_reason_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post(f"/api/v1/admin/sessions/{uuid4()}/force-close", json={})
    assert resp.status_code == 422


# ---------- start_admin_session (Sprint 8a Task 0) ---------------------------


async def test_start_admin_session_returns_started_shift_on_happy_path(
    session: AsyncSession,
) -> None:
    """Direct-await: starts a shift, returns the row, persists to DB."""
    # Wipe the conftest's pre-seeded shift so the start can actually take.
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()

    from app.api.v1.admin.sessions import get_shift_session_service

    service = get_shift_session_service(session)
    result = await start_admin_session(
        request=AdminSessionStartRequest(workstation_id="admin-ws-01"),
        user=make_user("dcinv-admin"),
        service=service,
    )

    assert isinstance(result, ShiftSessionResponse)
    assert result.tablet_id == "admin-ws-01"  # column stays 'tablet_id' under the hood
    assert result.shift_end_at is None
    assert result.end_reason is None


async def test_start_admin_session_returns_409_when_admin_already_has_active_shift(
    session: AsyncSession,
) -> None:
    """Direct-await: the conftest pre-seeded a shift for the default user;
    starting again returns 409 with the existing-shift payload."""
    import json

    from fastapi.responses import JSONResponse

    from app.api.v1.admin.sessions import get_shift_session_service

    service = get_shift_session_service(session)
    result = await start_admin_session(
        request=AdminSessionStartRequest(workstation_id="admin-ws-01"),
        user=make_user("dcinv-admin"),
        service=service,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(bytes(result.body))
    assert body["error"]["code"] == "SESSION_ALREADY_ACTIVE"
    assert "active" in body["error"]
    assert body["error"]["active"]["id"]  # the existing shift's id is echoed


async def test_post_admin_sessions_start_endpoint_returns_403_without_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Role gate kicks in before any DB lookup."""
    as_user("dcinv-mobile-user")
    resp = await client.post("/api/v1/admin/sessions/start", json={"workstation_id": "ws-01"})
    assert resp.status_code == 403


async def test_post_admin_sessions_start_endpoint_does_not_require_active_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Sprint 8a Task 0 chicken-and-egg: this is the ONE /admin/* route NOT
    gated by require_role_with_active_shift. Wipe the conftest's seeded
    shift, then prove the call still succeeds (no 409 NO_ACTIVE_SHIFT)."""
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")

    resp = await client.post("/api/v1/admin/sessions/start", json={"workstation_id": "admin-ws-01"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tablet_id"] == "admin-ws-01"
    # response_model_exclude_none drops the null shift_end_at + end_reason
    # fields — the absence-from-wire means the shift is active.
    assert "shift_end_at" not in body
    assert "end_reason" not in body


async def test_post_admin_sessions_start_rejects_missing_workstation_id_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post("/api/v1/admin/sessions/start", json={})
    assert resp.status_code == 422


async def test_post_admin_sessions_start_rejects_empty_workstation_id_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post("/api/v1/admin/sessions/start", json={"workstation_id": ""})
    assert resp.status_code == 422


async def test_post_admin_sessions_start_rejects_over_length_workstation_id_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post("/api/v1/admin/sessions/start", json={"workstation_id": "x" * 256})
    assert resp.status_code == 422


async def test_post_admin_sessions_start_rejects_extra_body_field_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """extra='forbid' on AdminSessionStartRequest catches typos."""
    as_user("dcinv-admin")
    resp = await client.post(
        "/api/v1/admin/sessions/start",
        json={"workstation_id": "ws-01", "tablet_id": "wrong-field-name"},
    )
    assert resp.status_code == 422


# Suppress unused-import warning for timedelta — referenced via fixture defaults.
_ = timedelta
