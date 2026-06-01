"""``audit_log`` repository — append-only insert path + Sprint 7 Task 2 query."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLogModel
from app.db.repositories.errors import RepositoryError
from app.domain.audit import AuditLogEntry, AuditResult


@dataclass(frozen=True, slots=True)
class AuditLogQueryFilters:
    """Filter spec for ``AuditLogRepository.query`` (Sprint 7 Task 2 decision C).

    Every field defaults to ``None`` so the endpoint can build the dataclass
    from optional query params without per-field branching. ``from_`` is named
    with a trailing underscore because ``from`` is a Python keyword; the wire
    spelling stays ``?from=...`` via ``Query(alias="from")``.
    """

    user_keycloak_id: UUID | None = None
    from_: datetime | None = None
    to: datetime | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    operation: str | None = None
    session_id: UUID | None = None
    result: AuditResult | None = None


def _to_domain(model: AuditLogModel) -> AuditLogEntry:
    return AuditLogEntry(
        id=model.id,
        request_id=model.request_id,
        timestamp=model.timestamp,
        user_email=model.user_email,
        user_keycloak_id=model.user_keycloak_id,
        session_id=model.session_id,
        operation=model.operation,
        entity_type=model.entity_type,
        entity_id=model.entity_id,
        before_json=model.before_json,
        after_json=model.after_json,
        result=model.result,
    )


class AuditLogRepository:
    """Repository for ``audit_log``.

    Insert path is the original Sprint 2 contract. ``query`` (Sprint 7 Task 2)
    is the read path for the admin audit endpoint. Neither commits — the
    surrounding service / endpoint owns the transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert(self, entry: AuditLogEntry) -> None:
        stmt = insert(AuditLogModel).values(
            request_id=entry.request_id,
            timestamp=entry.timestamp,
            user_email=entry.user_email,
            user_keycloak_id=entry.user_keycloak_id,
            session_id=entry.session_id,
            operation=entry.operation,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            before_json=entry.before_json,
            after_json=entry.after_json,
            result=entry.result,
        )
        try:
            await self.session.execute(stmt)
        except IntegrityError as exc:
            raise RepositoryError(str(exc)) from exc

    async def query(
        self,
        *,
        filters: AuditLogQueryFilters,
        page: int,
        page_size: int,
    ) -> tuple[list[AuditLogEntry], bool]:
        """Return ``(rows, has_more)`` for the given filters + page.

        ``has_more`` is computed via ``LIMIT page_size + 1`` and slicing the
        extra row off — one query, no separate ``COUNT(*)`` (Sprint 7 Task 2
        decision C: 36k+ rows at 2-year retention means count is expensive
        and unneeded; web admin "next / prev" UX is sufficient with has_more).

        Ordered by ``timestamp DESC, id DESC`` so identical-timestamp rows
        have a stable tiebreaker across page boundaries.
        """
        stmt = select(AuditLogModel)
        if filters.user_keycloak_id is not None:
            stmt = stmt.where(AuditLogModel.user_keycloak_id == filters.user_keycloak_id)
        if filters.from_ is not None:
            stmt = stmt.where(AuditLogModel.timestamp >= filters.from_)
        if filters.to is not None:
            stmt = stmt.where(AuditLogModel.timestamp <= filters.to)
        if filters.entity_type is not None:
            stmt = stmt.where(AuditLogModel.entity_type == filters.entity_type)
        if filters.entity_id is not None:
            stmt = stmt.where(AuditLogModel.entity_id == filters.entity_id)
        if filters.operation is not None:
            stmt = stmt.where(AuditLogModel.operation == filters.operation)
        if filters.session_id is not None:
            stmt = stmt.where(AuditLogModel.session_id == filters.session_id)
        if filters.result is not None:
            stmt = stmt.where(AuditLogModel.result == filters.result)

        offset = (page - 1) * page_size
        stmt = (
            stmt.order_by(AuditLogModel.timestamp.desc(), AuditLogModel.id.desc())
            .offset(offset)
            .limit(page_size + 1)
        )
        models = (await self.session.execute(stmt)).scalars().all()
        has_more = len(models) > page_size
        return [_to_domain(m) for m in models[:page_size]], has_more
