"""``shift_sessions`` repository — domain-typed CRUD for the shift-session lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.shift_session import ShiftSessionModel
from app.domain.shift_session import ShiftSession


@dataclass(frozen=True, slots=True)
class ShiftSessionQueryFilters:
    """Filter spec for ``ShiftSessionRepository.query`` (Sprint 7 Task 3).

    All fields default to ``None`` / ``False`` so the admin endpoint can build
    the dataclass from optional query params without per-field branching.
    ``from_`` is named with a trailing underscore because ``from`` is a Python
    keyword; the wire spelling stays ``?from=...`` via ``Query(alias="from")``.
    """

    user_keycloak_id: UUID | None = None
    from_: datetime | None = None
    to: datetime | None = None
    active_only: bool = False


def _to_domain(model: ShiftSessionModel) -> ShiftSession:
    return ShiftSession(
        id=model.id,
        user_email=model.user_email,
        user_keycloak_id=model.user_keycloak_id,
        shift_start_at=model.shift_start_at,
        shift_end_at=model.shift_end_at,
        tablet_id=model.tablet_id,
        end_reason=model.end_reason,
    )


class ShiftSessionRepository:
    """Repository for ``shift_sessions``.

    Reads return domain ``ShiftSession`` instances. Writes don't commit — the
    caller (Task 2 ``ShiftSessionService``) owns the transaction.

    ``insert`` deliberately does NOT wrap ``IntegrityError`` in ``RepositoryError``
    (unlike ``AuditLogRepository.insert``). The Task 2 service catches the
    ``shift_sessions_one_active_per_user`` partial-unique-index violation
    specifically to raise the 409 ``SESSION_ALREADY_ACTIVE`` path — same call
    as ``QRCodeRepository.update`` against ``qr_one_per_device``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, session_id: UUID) -> ShiftSession | None:
        model = await self.session.get(ShiftSessionModel, session_id)
        return _to_domain(model) if model is not None else None

    async def get_active_for_user(self, user_keycloak_id: UUID) -> ShiftSession | None:
        """Return the user's currently-active shift, or ``None`` if none.

        The ``shift_sessions_one_active_per_user`` partial unique index
        guarantees at most one active row per user, so this returns a single
        ``ShiftSession`` or ``None``.
        """
        stmt = select(ShiftSessionModel).where(
            ShiftSessionModel.user_keycloak_id == user_keycloak_id,
            ShiftSessionModel.shift_end_at.is_(None),
        )
        model = (await self.session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def insert(self, shift: ShiftSession) -> None:
        model = ShiftSessionModel(
            id=shift.id,
            user_email=shift.user_email,
            user_keycloak_id=shift.user_keycloak_id,
            shift_start_at=shift.shift_start_at,
            shift_end_at=shift.shift_end_at,
            tablet_id=shift.tablet_id,
            end_reason=shift.end_reason,
        )
        self.session.add(model)
        await self.session.flush()

    async def update(self, shift: ShiftSession) -> None:
        """Persist changes to an existing shift row (used for the end transition)."""
        stmt = (
            update(ShiftSessionModel)
            .where(ShiftSessionModel.id == shift.id)
            .values(
                shift_end_at=shift.shift_end_at,
                end_reason=shift.end_reason,
            )
        )
        await self.session.execute(stmt)

    async def query(
        self,
        *,
        filters: ShiftSessionQueryFilters,
        page: int,
        page_size: int,
    ) -> tuple[list[ShiftSession], bool]:
        """Return ``(rows, has_more)`` for the given filters + page (Sprint 7 Task 3).

        Ordered by ``shift_start_at DESC, id DESC`` so the most recent shifts
        surface first with a stable tiebreaker for identical-timestamp rows.
        ``has_more`` computed via ``LIMIT page_size + 1`` — same pattern as
        :py:meth:`app.db.repositories.audit_log.AuditLogRepository.query`,
        no separate ``COUNT(*)`` round-trip.
        """
        stmt = select(ShiftSessionModel)
        if filters.user_keycloak_id is not None:
            stmt = stmt.where(ShiftSessionModel.user_keycloak_id == filters.user_keycloak_id)
        if filters.from_ is not None:
            stmt = stmt.where(ShiftSessionModel.shift_start_at >= filters.from_)
        if filters.to is not None:
            stmt = stmt.where(ShiftSessionModel.shift_start_at <= filters.to)
        if filters.active_only:
            stmt = stmt.where(ShiftSessionModel.shift_end_at.is_(None))

        offset = (page - 1) * page_size
        stmt = (
            stmt.order_by(ShiftSessionModel.shift_start_at.desc(), ShiftSessionModel.id.desc())
            .offset(offset)
            .limit(page_size + 1)
        )
        models = (await self.session.execute(stmt)).scalars().all()
        has_more = len(models) > page_size
        return [_to_domain(m) for m in models[:page_size]], has_more

    async def find_stale_active(self, *, older_than: datetime) -> list[ShiftSession]:
        """Return active shifts whose ``shift_start_at`` predates ``older_than``.

        Caller (Sprint 7 Task 1 auto-end job) passes ``older_than`` so the
        boundary is deterministic in tests; production passes
        ``datetime.now(UTC) - timedelta(hours=SHIFT_AUTO_END_THRESHOLD_HOURS)``.

        Ordered by ``shift_start_at`` ASC so the oldest stale shift is ended
        first — deterministic and operationally sensible (longest-orphaned
        gets cleaned up first if iteration is partial).

        No new index needed: the partial unique index
        ``shift_sessions_one_active_per_user`` already restricts the scanned
        set to active rows (≤ 1 per user). See Sprint 7 Task 1 decision 6.
        """
        stmt = (
            select(ShiftSessionModel)
            .where(
                ShiftSessionModel.shift_end_at.is_(None),
                ShiftSessionModel.shift_start_at < older_than,
            )
            .order_by(ShiftSessionModel.shift_start_at.asc())
        )
        models = (await self.session.execute(stmt)).scalars().all()
        return [_to_domain(m) for m in models]
