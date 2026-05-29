"""``audit_log`` repository — append-only insert path."""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLogModel
from app.db.repositories.errors import RepositoryError
from app.domain.audit import AuditLogEntry


class AuditLogRepository:
    """Repository for ``audit_log``.

    Only an insert path is exposed this sprint. The repo never commits — the
    surrounding service transaction (Task 6) is responsible for atomicity with
    the qr_batches + qr_codes writes.
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
