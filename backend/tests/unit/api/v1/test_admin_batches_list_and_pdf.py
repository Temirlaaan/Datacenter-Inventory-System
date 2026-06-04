"""Unit + integration tests for the Sprint 8b Task 2 endpoints:

- ``GET /api/v1/admin/batches/`` — paginated list of batches.
- ``GET /api/v1/admin/batches/{id}/labels.pdf`` — A4 PDF download.

Both gated ``dcinv-admin`` + active shift. Same direct-await + AsyncClient
split as the rest of the admin endpoint tests so coverage traces every line.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.batches import (
    BatchListResponse,
    get_batch_labels_pdf,
    list_batches,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_USER_KEYCLOAK_ID = UUID("11111111-1111-1111-1111-111111111111")
_SHIFT_SESSION_ID = UUID("33333333-3333-3333-3333-333333333333")


def _admin_user(*, shift_session_id: UUID | None = _SHIFT_SESSION_ID) -> AuthUser:
    return dataclasses.replace(make_user("dcinv-admin"), shift_session_id=shift_session_id)


def _seed_batch(*, count: int = 0, comment: str | None = None) -> QRBatch:
    return QRBatch(
        id=uuid4(),
        created_at=datetime.now(UTC),
        created_by_email="alice@example.com",
        created_by_keycloak_id=_USER_KEYCLOAK_ID,
        count=count,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment=comment,
    )


def _free_qr(qr_id: str, batch_id: UUID) -> QR:
    return QR(
        id=qr_id,
        batch_id=batch_id,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


# ---------- list_batches handler (direct await) ------------------------------


async def test_list_batches_returns_envelope_with_pagination_defaults(
    session: AsyncSession,
) -> None:
    async with get_sessionmaker()() as db:
        await QRBatchRepository(db).insert(_seed_batch(count=3, comment="alpha"))
        await db.commit()

    result = await list_batches(page=1, page_size=20, user=_admin_user(), session=session)
    assert isinstance(result, BatchListResponse)
    assert result.page == 1
    assert result.page_size == 20
    assert result.has_more is False
    assert len(result.results) == 1
    only = result.results[0]
    assert only.count == 3
    assert only.comment == "alpha"


async def test_list_batches_returns_empty_results_on_empty_table(
    session: AsyncSession,
) -> None:
    result = await list_batches(page=1, page_size=20, user=_admin_user(), session=session)
    assert result.results == []
    assert result.has_more is False


# ---------- list_batches routing + role + active-shift gating ----------------


async def test_get_batches_returns_403_when_caller_lacks_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get("/api/v1/admin/batches/")
    assert resp.status_code == 403


async def test_get_batches_returns_409_no_active_shift_when_admin_has_no_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/batches/")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_get_batches_rejects_page_size_above_cap_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/batches/?page_size=101")
    assert resp.status_code == 422


async def test_get_batches_returns_200_envelope(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/batches/")
    assert resp.status_code == 200
    body = resp.json()
    assert {"results", "page", "page_size", "has_more"} <= set(body.keys())


# ---------- get_batch_labels_pdf handler (direct await) ----------------------


async def test_get_batch_labels_pdf_returns_pdf_response_with_attachment_header(
    session: AsyncSession,
) -> None:
    batch = _seed_batch(count=1)
    async with get_sessionmaker()() as db:
        await QRBatchRepository(db).insert(batch)
        await QRCodeRepository(db).bulk_insert([_free_qr("DCQR-PDF00001", batch.id)])
        await db.commit()

    resp = await get_batch_labels_pdf(batch_id=batch.id, user=_admin_user(), session=session)
    assert resp.media_type == "application/pdf"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert f"batch-{batch.id}.pdf" in disposition
    body = bytes(resp.body)
    assert body.startswith(b"%PDF-")


async def test_get_batch_labels_pdf_raises_404_for_unknown_batch_id(
    session: AsyncSession,
) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_batch_labels_pdf(batch_id=uuid4(), user=_admin_user(), session=session)
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


# ---------- get_batch_labels_pdf routing + role + active-shift gating --------


async def test_get_batch_labels_pdf_returns_403_when_caller_lacks_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get(f"/api/v1/admin/batches/{uuid4()}/labels.pdf")
    assert resp.status_code == 403


async def test_get_batch_labels_pdf_returns_409_no_active_shift_when_admin_has_no_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")
    resp = await client.get(f"/api/v1/admin/batches/{uuid4()}/labels.pdf")
    assert resp.status_code == 409


async def test_get_batch_labels_pdf_e2e_downloads_pdf_for_seeded_batch(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    batch = _seed_batch(count=2)
    async with get_sessionmaker()() as db:
        await QRBatchRepository(db).insert(batch)
        await QRCodeRepository(db).bulk_insert(
            [_free_qr(f"DCQR-E2E0000{i}", batch.id) for i in range(2)]
        )
        await db.commit()

    resp = await client.get(f"/api/v1/admin/batches/{batch.id}/labels.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content.startswith(b"%PDF-")
