"""``qr_codes`` repository — domain-typed reads and bulk insert."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.qr import QRCodeModel
from app.db.repositories.errors import RepositoryError
from app.domain.qr import QR, QRStatus


def _to_domain(model: QRCodeModel) -> QR:
    return QR(
        id=model.id,
        batch_id=model.batch_id,
        status=model.status,
        bound_to_device_id=model.bound_to_device_id,
        bound_at=model.bound_at,
        bound_by_email=model.bound_by_email,
        retired_at=model.retired_at,
        retired_reason=model.retired_reason,
    )


def _from_domain(qr: QR) -> dict[str, object]:
    return {
        "id": qr.id,
        "batch_id": qr.batch_id,
        "status": qr.status,
        "bound_to_device_id": qr.bound_to_device_id,
        "bound_at": qr.bound_at,
        "bound_by_email": qr.bound_by_email,
        "retired_at": qr.retired_at,
        "retired_reason": qr.retired_reason,
    }


class QRCodeRepository:
    """Repository for ``qr_codes``.

    Reads return domain QR instances. Writes don't commit — the caller owns the
    transaction. ``get_by_id_for_update`` and ``update`` support the Sprint 4
    bind/retire orchestration's lock-then-transition pattern.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, qr_id: str) -> QR | None:
        model = await self.session.get(QRCodeModel, qr_id)
        return _to_domain(model) if model is not None else None

    async def get_by_id_for_update(self, qr_id: str) -> QR | None:
        """Like ``get_by_id`` but issues ``SELECT ... FOR UPDATE`` for a row lock.

        Must be called inside an active transaction so the lock is held until
        commit/rollback. Caller responsibility — not enforced here.
        """
        stmt = select(QRCodeModel).where(QRCodeModel.id == qr_id).with_for_update()
        model = (await self.session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def find_by_batch_id(self, batch_id: UUID) -> list[QR]:
        stmt = select(QRCodeModel).where(QRCodeModel.batch_id == batch_id).order_by(QRCodeModel.id)
        result = await self.session.execute(stmt)
        return [_to_domain(model) for model in result.scalars()]

    async def find_by_bound_device_id(self, device_id: int) -> QR | None:
        """Return the BOUND QR for ``device_id``, or ``None`` if none.

        The ``qr_one_per_device`` partial unique index guarantees at most one
        BOUND row per device, so this returns a single ``QR`` or ``None``. Used
        by Sprint 5 Task 4 decommission to find the QR (if any) to retire
        before changing the device's status.
        """
        stmt = select(QRCodeModel).where(
            QRCodeModel.bound_to_device_id == device_id,
            QRCodeModel.status == QRStatus.BOUND,
        )
        model = (await self.session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def count_by_status_for_batch(self, batch_id: UUID) -> dict[QRStatus, int]:
        """Return a ``{QRStatus -> count}`` dict for one batch via a single
        GROUP BY query.

        Missing statuses are filled with zero so the caller can render all
        three buckets uniformly without per-status branching. Unknown
        ``batch_id`` returns all-zeros (no exception, no 404 — caller
        decides whether the batch even exists).
        """
        stmt = (
            select(QRCodeModel.status, func.count())
            .where(QRCodeModel.batch_id == batch_id)
            .group_by(QRCodeModel.status)
        )
        result = await self.session.execute(stmt)
        # Explicit annotation: dict.fromkeys' inferred value type is Optional[int]
        # under strict-mode tightening, but every value is a non-None int here.
        counts: dict[QRStatus, int] = dict.fromkeys(QRStatus, 0)
        for status_value, count in result.all():
            counts[status_value] = count
        return counts

    async def exists(self, qr_id: str) -> bool:
        stmt = select(QRCodeModel.id).where(QRCodeModel.id == qr_id).limit(1)
        return (await self.session.scalar(stmt)) is not None

    async def bulk_insert(self, codes: list[QR]) -> None:
        # Empty input must be a no-op; sqlalchemy raises on insert().values([]).
        if not codes:
            return
        stmt = insert(QRCodeModel).values([_from_domain(qr) for qr in codes])
        try:
            await self.session.execute(stmt)
        except IntegrityError as exc:
            raise RepositoryError(str(exc)) from exc

    async def update(self, qr: QR) -> None:
        """Persist changes to an existing QR row.

        IntegrityError is **not** wrapped in RepositoryError (unlike
        ``bulk_insert``): the Sprint 4 bind/retire orchestration relies on
        catching the ``qr_one_per_device`` partial-unique-index violation
        specifically (race against another concurrent bind to the same device).
        """
        stmt = (
            update(QRCodeModel)
            .where(QRCodeModel.id == qr.id)
            .values(
                status=qr.status,
                bound_to_device_id=qr.bound_to_device_id,
                bound_at=qr.bound_at,
                bound_by_email=qr.bound_by_email,
                retired_at=qr.retired_at,
                retired_reason=qr.retired_reason,
            )
        )
        await self.session.execute(stmt)
