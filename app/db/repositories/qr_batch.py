"""``qr_batches`` repository — domain-typed CRUD against the live DB."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.qr import QRBatchModel
from app.db.repositories.errors import RepositoryError
from app.domain.qr import QRBatch


def _to_domain(model: QRBatchModel) -> QRBatch:
    return QRBatch(
        id=model.id,
        created_at=model.created_at,
        created_by_email=model.created_by_email,
        created_by_keycloak_id=model.created_by_keycloak_id,
        count=model.count,
        intended_site_id=model.intended_site_id,
        intended_location_id=model.intended_location_id,
        intended_rack_id=model.intended_rack_id,
        comment=model.comment,
        pdf_path=model.pdf_path,
    )


class QRBatchRepository:
    """Repository for ``qr_batches``.

    The repo does not own transactions — callers (services) wrap the work in a
    session-level transaction so multi-statement operations (Task 6's batch +
    codes + audit row) commit atomically.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, batch_id: UUID) -> QRBatch | None:
        model = await self.session.get(QRBatchModel, batch_id)
        return _to_domain(model) if model is not None else None

    async def insert(self, batch: QRBatch) -> None:
        model = QRBatchModel(
            id=batch.id,
            created_at=batch.created_at,
            created_by_email=batch.created_by_email,
            created_by_keycloak_id=batch.created_by_keycloak_id,
            count=batch.count,
            intended_site_id=batch.intended_site_id,
            intended_location_id=batch.intended_location_id,
            intended_rack_id=batch.intended_rack_id,
            comment=batch.comment,
            pdf_path=batch.pdf_path,
        )
        self.session.add(model)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise RepositoryError(str(exc)) from exc
