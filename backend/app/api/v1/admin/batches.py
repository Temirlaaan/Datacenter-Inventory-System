"""QR batch endpoints. ToR §8.3.

- ``POST /api/v1/admin/batches/`` — generate a batch of QR codes.
- ``GET  /api/v1/admin/batches/{batch_id}`` — batch metadata + its codes.

Both require the ``dcinv-admin`` role. The POST honours an optional
``Idempotency-Key`` header: the batch generation, the idempotency placeholder,
and the recorded response all commit in one transaction, so a retried request
returns the original response without generating a second batch.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role_with_active_shift
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_session
from app.domain.qr import QR, QRBatch, QRStatus
from app.services.idempotency import IdempotencyKeyConflict, with_idempotency
from app.services.pdf_labels import render_batch_labels_pdf
from app.services.qr.generation import GenerateBatchRequest, QRGenerationService

router = APIRouter()


class QRCodeShort(BaseModel):
    """A QR code id, returned in the batch-creation response."""

    id: str


class QRCodeDetail(BaseModel):
    """Full QR code state, returned in the batch-detail response."""

    id: str
    status: QRStatus
    bound_to_device_id: int | None = None
    bound_at: datetime | None = None
    retired_at: datetime | None = None
    retired_reason: str | None = None


class BatchCreatedResponse(BaseModel):
    batch_id: UUID
    count: int
    codes: list[QRCodeShort]


class BatchDetailsResponse(BaseModel):
    batch_id: UUID
    created_at: datetime
    created_by_email: str
    count: int
    intended_site_id: int | None
    intended_location_id: int | None
    intended_rack_id: int | None
    comment: str | None
    codes: list[QRCodeDetail]


class BatchSummary(BaseModel):
    """A single row of ``GET /api/v1/admin/batches/`` — metadata only,
    without the per-batch QR list. The detail endpoint returns the full
    list when the admin clicks through."""

    batch_id: UUID
    created_at: datetime
    created_by_email: str
    count: int
    comment: str | None = None


class BatchListResponse(BaseModel):
    """Envelope returned by ``GET /api/v1/admin/batches/``."""

    results: list[BatchSummary]
    page: int
    page_size: int
    has_more: bool


def _summary(batch: QRBatch) -> BatchSummary:
    return BatchSummary(
        batch_id=batch.id,
        created_at=batch.created_at,
        created_by_email=batch.created_by_email,
        count=batch.count,
        comment=batch.comment,
    )


def _batch_created_body(batch: QRBatch, codes: list[QR]) -> dict[str, object]:
    return BatchCreatedResponse(
        batch_id=batch.id,
        count=batch.count,
        codes=[QRCodeShort(id=code.id) for code in codes],
    ).model_dump(mode="json")


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=BatchCreatedResponse)
async def create_batch(
    payload: GenerateBatchRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    """Generate a batch of QR codes. Returns 201 with the new batch id and codes."""
    code_repo = QRCodeRepository(session)
    service = QRGenerationService(
        session, QRBatchRepository(session), code_repo, AuditLogRepository(session)
    )

    if idempotency_key is None:
        batch = await service.generate_batch(payload, user)
        body = _batch_created_body(batch, await code_repo.find_by_batch_id(batch.id))
        await session.commit()
        return JSONResponse(body, status_code=status.HTTP_201_CREATED)

    try:
        async with with_idempotency(
            session, UUID(user.sub), idempotency_key, payload.model_dump()
        ) as result:
            if result.is_replay:
                return JSONResponse(
                    result.cached_response,
                    status_code=result.cached_status or status.HTTP_201_CREATED,
                )
            batch = await service.generate_batch(payload, user)
            body = _batch_created_body(batch, await code_repo.find_by_batch_id(batch.id))
            await result.record(response_status=status.HTTP_201_CREATED, response_body=body)
            await session.commit()
            return JSONResponse(body, status_code=status.HTTP_201_CREATED)
    except IdempotencyKeyConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key reused with a different request payload",
        ) from exc


@router.get("/", response_model=BatchListResponse)
async def list_batches(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
) -> BatchListResponse:
    """Page through ``qr_batches`` newest-first. No audit row (operational
    read, parallels ``/admin/sessions`` — Sprint 7 decision 8)."""
    _ = user  # gate side-effect only
    rows, has_more = await QRBatchRepository(session).query(page=page, page_size=page_size)
    return BatchListResponse(
        results=[_summary(b) for b in rows],
        page=page,
        page_size=page_size,
        has_more=has_more,
    )


@router.get(
    "/{batch_id}/labels.pdf",
    responses={200: {"content": {"application/pdf": {}}, "description": "PDF batch labels"}},
)
async def get_batch_labels_pdf(
    batch_id: UUID,
    include: str = Query(default="free", pattern="^(free|all)$"),
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """A4-landscape PDF with the batch's QR codes laid out 32 per page.

    ``?include=free`` (default, 2026-06-10): render only QR codes still
    in FREE state. Use case: admin re-downloads the PDF after some
    stickers were stuck on devices (now BOUND) or damaged (now RETIRED) —
    no reason to reprint labels for codes already used or discarded.
    ``?include=all`` returns every code regardless of state — archival
    view of the batch as originally printed.

    No audit row: the contents are the same as the JSON detail endpoint
    (admin already saw the codes), and ToR §5.4.6 covers sensitive reads
    only — batch contents aren't in that class.

    reportlab is synchronous; the render runs in a worker thread via
    ``asyncio.to_thread`` so the event loop stays responsive on large
    batches (decision 11)."""
    _ = user
    batch = await QRBatchRepository(session).get_by_id(batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    codes = await QRCodeRepository(session).find_by_batch_id(batch_id)
    if include == "free":
        codes = [c for c in codes if c.status is QRStatus.FREE]
    pdf_bytes = await asyncio.to_thread(render_batch_labels_pdf, batch=batch, codes=codes)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="batch-{batch_id}.pdf"'},
    )


@router.get("/{batch_id}", response_model=BatchDetailsResponse)
async def get_batch(
    batch_id: UUID,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
) -> BatchDetailsResponse:
    """Return a batch's metadata and all its QR codes. 404 if the batch is unknown."""
    batch = await QRBatchRepository(session).get_by_id(batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")
    codes = await QRCodeRepository(session).find_by_batch_id(batch_id)
    return BatchDetailsResponse(
        batch_id=batch.id,
        created_at=batch.created_at,
        created_by_email=batch.created_by_email,
        count=batch.count,
        intended_site_id=batch.intended_site_id,
        intended_location_id=batch.intended_location_id,
        intended_rack_id=batch.intended_rack_id,
        comment=batch.comment,
        codes=[
            QRCodeDetail(
                id=code.id,
                status=code.status,
                bound_to_device_id=code.bound_to_device_id,
                bound_at=code.bound_at,
                retired_at=code.retired_at,
                retired_reason=code.retired_reason,
            )
            for code in codes
        ],
    )
