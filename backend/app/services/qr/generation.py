"""QR batch generation. ToR §4.2.1, Architecture §4.

``QRGenerationService.generate_batch`` writes the batch, N FREE codes, and a
``qr.generate_batch`` audit row. The **caller owns the transaction**: the
service issues the writes but does not commit, so the endpoint can wrap the
batch generation, an idempotency placeholder, and the idempotency response
record into one atomic commit (Sprint 2 Task 7).

On failure the service rolls the caller's transaction back (no orphan
batch/codes, and the idempotency placeholder vanishes with it), then writes a
separate ``result='failure'`` audit row in a fresh transaction and re-raises so
the caller sees the original error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.qr import QR, QRBatch, QRStatus
from app.observability.request_id import current_request_id
from app.services.qr.token import generate_unique_token


class GenerateBatchRequest(BaseModel):
    """Request body for ``POST /api/v1/admin/batches/``.

    Bounds from ToR §5.1 + sprint-2 Task 6 anti-criteria: count capped at 500 so
    a single request can't exhaust connection memory.
    """

    count: int = Field(ge=1, le=500)
    intended_site_id: int | None = None
    intended_location_id: int | None = None
    intended_rack_id: int | None = None
    comment: str | None = Field(default=None, max_length=200)


class QRGenerationService:
    """Generates a batch of QR codes end-to-end. Owns its session's transaction."""

    def __init__(
        self,
        session: AsyncSession,
        qr_batch_repo: QRBatchRepository,
        qr_code_repo: QRCodeRepository,
        audit_log_repo: AuditLogRepository,
    ) -> None:
        self._session = session
        self._qr_batch_repo = qr_batch_repo
        self._qr_code_repo = qr_code_repo
        self._audit_log_repo = audit_log_repo

    async def generate_batch(self, request: GenerateBatchRequest, user: AuthUser) -> QRBatch:
        request_id = UUID(current_request_id())
        now = datetime.now(UTC)
        batch = QRBatch(
            id=uuid4(),
            created_at=now,
            created_by_email=user.email or "",
            created_by_keycloak_id=UUID(user.sub),
            count=request.count,
            intended_site_id=request.intended_site_id,
            intended_location_id=request.intended_location_id,
            intended_rack_id=request.intended_rack_id,
            comment=request.comment,
        )
        after_json: dict[str, Any] = {
            "count": request.count,
            "intended_site_id": request.intended_site_id,
            "intended_location_id": request.intended_location_id,
            "intended_rack_id": request.intended_rack_id,
        }

        try:
            # No `begin()` — the writes join the caller's transaction so the
            # endpoint can commit batch + codes + audit + idempotency atomically.
            await self._qr_batch_repo.insert(batch)
            tokens = [await generate_unique_token(self._qr_code_repo) for _ in range(request.count)]
            codes = [
                QR(
                    id=token,
                    batch_id=batch.id,
                    status=QRStatus.FREE,
                    bound_to_device_id=None,
                    bound_at=None,
                    bound_by_email=None,
                    retired_at=None,
                    retired_reason=None,
                )
                for token in tokens
            ]
            await self._qr_code_repo.bulk_insert(codes)
            await self._audit_log_repo.insert(
                self._audit_entry(
                    request_id=request_id,
                    timestamp=now,
                    user=user,
                    batch_id=batch.id,
                    after_json=after_json,
                    result=AuditResult.SUCCESS,
                )
            )
            return batch
        except Exception:
            # Discard the caller's transaction — batch, codes, and any idempotency
            # placeholder it opened. Then write the failure audit row in a fresh
            # transaction so a forensic query can correlate the request_id with
            # the intended batch_id even though the batch never persisted.
            await self._session.rollback()
            async with self._session.begin():
                await self._audit_log_repo.insert(
                    self._audit_entry(
                        request_id=request_id,
                        timestamp=now,
                        user=user,
                        batch_id=batch.id,
                        after_json=after_json,
                        result=AuditResult.FAILURE,
                    )
                )
            raise

    @staticmethod
    def _audit_entry(
        *,
        request_id: UUID,
        timestamp: datetime,
        user: AuthUser,
        batch_id: UUID,
        after_json: dict[str, Any],
        result: AuditResult,
    ) -> AuditLogEntry:
        return AuditLogEntry(
            request_id=request_id,
            timestamp=timestamp,
            user_email=user.email or "",
            user_keycloak_id=UUID(user.sub),
            # Sprint 8a Task 0: source swapped from hardcoded None to the
            # admin's shift_session_id (populated by
            # require_role_with_active_shift on the /admin/batches/ endpoint).
            # Pre-Sprint-8a batch rows retain session_id NULL — consistent
            # with the Sprint 6 decision D "no historical migration" stance.
            session_id=user.shift_session_id,
            operation="qr.generate_batch",
            entity_type="batch",
            entity_id=str(batch_id),
            before_json={},
            after_json=after_json,
            result=result,
        )
