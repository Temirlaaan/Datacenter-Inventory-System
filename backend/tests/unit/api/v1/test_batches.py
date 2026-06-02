"""Tests for the /api/v1/admin/batches endpoints.

Handler-call tests ``await`` ``create_batch`` / ``get_batch`` directly to
exercise every line of the handlers. Integration tests drive the full ASGI
stack to confirm routing, role gating, and request-body validation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.batches import create_batch, get_batch
from app.auth.dependencies import AuthUser
from app.db.session import get_sessionmaker
from app.services.qr.generation import GenerateBatchRequest
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration


async def _count(table: str) -> int:
    async with get_sessionmaker()() as session:
        result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
        count: int = result.scalar_one()
        return count


# === create_batch handler =====================================================


async def test_create_batch_returns_201_with_batch_and_codes(
    session: AsyncSession,
) -> None:
    resp = await create_batch(
        GenerateBatchRequest(count=10), make_user("dcinv-admin"), session, None
    )

    assert resp.status_code == 201
    body = json.loads(resp.body)
    assert body["count"] == 10
    assert len(body["codes"]) == 10
    assert all(c["id"].startswith("DCQR-") for c in body["codes"])
    assert await _count("qr_batches") == 1
    assert await _count("qr_codes") == 10
    assert await _count("audit_log") == 1


async def test_create_batch_with_idempotency_key_records_and_returns_201(
    session: AsyncSession,
) -> None:
    resp = await create_batch(
        GenerateBatchRequest(count=4), make_user("dcinv-admin"), session, "key-1"
    )
    assert resp.status_code == 201
    assert await _count("qr_batches") == 1
    assert await _count("idempotency_keys") == 1


async def test_create_batch_replays_response_for_same_idempotency_key(
    session: AsyncSession,
) -> None:
    user = make_user("dcinv-admin")
    first = await create_batch(GenerateBatchRequest(count=4), user, session, "key-2")

    async with get_sessionmaker()() as session2:
        second = await create_batch(GenerateBatchRequest(count=4), user, session2, "key-2")

    assert first.status_code == 201
    assert second.status_code == 201
    assert json.loads(second.body) == json.loads(first.body)  # replayed
    assert await _count("qr_batches") == 1  # no second batch


async def test_create_batch_same_key_different_payload_raises_422(
    session: AsyncSession,
) -> None:
    user = make_user("dcinv-admin")
    await create_batch(GenerateBatchRequest(count=4), user, session, "key-3")

    async with get_sessionmaker()() as session2:
        with pytest.raises(HTTPException) as exc:
            await create_batch(GenerateBatchRequest(count=9), user, session2, "key-3")

    assert exc.value.status_code == 422
    assert await _count("qr_batches") == 1


# === get_batch handler ========================================================


async def test_get_batch_returns_metadata_and_codes(session: AsyncSession) -> None:
    created = await create_batch(
        GenerateBatchRequest(count=5, intended_site_id=7, comment="rack 9"),
        make_user("dcinv-admin"),
        session,
        None,
    )
    batch_id = json.loads(created.body)["batch_id"]

    async with get_sessionmaker()() as session2:
        result = await get_batch(batch_id, make_user("dcinv-admin"), session2)

    assert str(result.batch_id) == batch_id
    assert result.count == 5
    assert result.intended_site_id == 7
    assert result.comment == "rack 9"
    assert len(result.codes) == 5
    assert all(c.status == "free" for c in result.codes)


async def test_get_batch_unknown_id_raises_404(session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_batch(uuid4(), make_user("dcinv-admin"), session)
    assert exc.value.status_code == 404


# === full-stack integration ===================================================


async def test_create_batch_endpoint_rejects_bad_payload_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post("/api/v1/admin/batches/", json={"count": 0})
    assert resp.status_code == 422
    assert await _count("qr_batches") == 0


async def test_create_batch_endpoint_without_admin_role_returns_403(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")  # not an admin
    resp = await client.post("/api/v1/admin/batches/", json={"count": 5})
    assert resp.status_code == 403
    assert await _count("qr_batches") == 0


async def test_create_batch_endpoint_happy_path_returns_201(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.post("/api/v1/admin/batches/", json={"count": 3})
    assert resp.status_code == 201
    assert len(resp.json()["codes"]) == 3


async def test_create_batch_endpoint_persists_audit_row_with_admin_shift_session_id(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Sprint 8a Task 0: audit row's session_id is now sourced from the
    admin's shift_sessions.id (populated by require_role_with_active_shift),
    NOT hardcoded None. Locks the source swap in place."""
    from tests.integration.conftest import DEFAULT_SHIFT_SESSION_ID

    as_user("dcinv-admin")
    resp = await client.post("/api/v1/admin/batches/", json={"count": 2})
    assert resp.status_code == 201

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT session_id, operation FROM audit_log ORDER BY timestamp DESC LIMIT 1")
            )
        ).one()
    assert row.operation == "qr.generate_batch"
    assert row.session_id == DEFAULT_SHIFT_SESSION_ID


async def test_create_batch_endpoint_returns_409_no_active_shift_when_admin_has_no_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Sprint 8a Task 0: /admin/batches/ now requires an active shift
    (consistent with the rest of /admin/*). Wipe the conftest's seeded
    shift, then assert the structured 409."""
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE shift_sessions CASCADE"))
        await session.commit()
    as_user("dcinv-admin")

    resp = await client.post("/api/v1/admin/batches/", json={"count": 3})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"
    assert await _count("qr_batches") == 0


async def test_create_batch_endpoint_rejects_overlong_idempotency_key_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    # The idempotency_keys.key column is VARCHAR(255); an over-long header must
    # be rejected at the boundary, not blow up as a DB DataError -> 500.
    as_user("dcinv-admin")
    resp = await client.post(
        "/api/v1/admin/batches/",
        json={"count": 3},
        headers={"Idempotency-Key": "x" * 256},
    )
    assert resp.status_code == 422
    assert await _count("qr_batches") == 0


async def test_get_batch_endpoint_without_admin_role_returns_403(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get(f"/api/v1/admin/batches/{uuid4()}")
    assert resp.status_code == 403
