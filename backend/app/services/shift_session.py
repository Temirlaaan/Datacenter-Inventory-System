"""ShiftSessionService — start/end/get_active orchestration over ``shift_sessions``.

Pure app-DB orchestration: no NetBox, no audit row, no compensation. Shifts
are session metadata, not "operations on entities", so they are not part of
Architecture §3.1's three-record write apparatus. The Sprint 6 Task 4 work
re-sources ``audit_log.session_id`` to ``shift_sessions.id`` for writes the
other services already perform — this service does not itself produce audit
rows.

Decisions (see ``docs/sprint-6.md`` §"Cross-cutting decisions"):

- **B** — at most one active session per user; concurrent ``start`` races are
  caught by the ``shift_sessions_one_active_per_user`` partial unique index and
  surfaced as ``SessionAlreadyActive`` carrying the winning row so the endpoint
  can populate the 409 body.
- **E** — service-layer ``end`` accepts ANY ``ShiftEndReason`` so the Sprint
  7 admin force-close endpoint can reuse it for ``forced`` and the Sprint 7
  auto-end background job can reuse it for ``auto_timeout``. The wire-format
  restriction to ``{manual, auto_timeout}`` on ``POST /sessions/end`` is
  enforced by that endpoint's Pydantic ``Literal``.
- **F.a** — the Task 4 dep layer resolves a user's active shift; this service
  exposes ``get_active`` to support that lookup.

The defensive ``in_transaction()`` guard mirrors ``QRLifecycleService`` (Sprint
4 Q2): SQLAlchemy 2.0 doesn't nest ``session.begin()`` cleanly, so calling
``start``/``end`` inside an active transaction would conflict with the inner
``async with self._session.begin()`` block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.shift_session import ShiftSessionRepository
from app.domain.shift_session import ShiftEndReason, ShiftSession


class SessionAlreadyActive(Exception):
    """A ``start()`` raced/duplicated against an existing active shift for this user.

    ``active`` carries the winning row so the endpoint can echo it in the
    structured 409 body — mobile shows "you already have a shift open since X".
    """

    def __init__(self, active: ShiftSession) -> None:
        super().__init__(f"shift already active for user {active.user_keycloak_id}")
        self.active = active


class NoActiveShift(Exception):
    """``end()`` called when the user has no active shift."""

    def __init__(self, user_keycloak_id: UUID) -> None:
        super().__init__(f"no active shift for user {user_keycloak_id}")
        self.user_keycloak_id = user_keycloak_id


class ShiftSessionService:
    """Coordinates ``shift_sessions`` lifecycle transitions."""

    def __init__(self, session: AsyncSession, repo: ShiftSessionRepository) -> None:
        self._session = session
        self._repo = repo

    async def get_active(self, user_keycloak_id: UUID) -> ShiftSession | None:
        return await self._repo.get_active_for_user(user_keycloak_id)

    async def start(
        self, *, user_email: str, user_keycloak_id: UUID, tablet_id: str
    ) -> ShiftSession:
        """Open a new active shift for ``user_keycloak_id``.

        Raises ``SessionAlreadyActive`` if the user already has one (either
        seen on the pre-check read or surfaced by the partial-unique-index
        race on insert).
        """
        if self._session.in_transaction():
            raise RuntimeError("ShiftSessionService.start called inside an active transaction")

        new = ShiftSession(
            id=uuid4(),
            user_email=user_email,
            user_keycloak_id=user_keycloak_id,
            shift_start_at=datetime.now(UTC),
            shift_end_at=None,
            tablet_id=tablet_id,
            end_reason=None,
        )
        try:
            async with self._session.begin():
                existing = await self._repo.get_active_for_user(user_keycloak_id)
                if existing is not None:
                    raise SessionAlreadyActive(existing)
                await self._repo.insert(new)
        except IntegrityError:
            # Partial-unique-index race: concurrent start won between our read
            # and our insert. Re-read in a fresh tx to populate the 409 body.
            async with self._session.begin():
                winner = await self._repo.get_active_for_user(user_keycloak_id)
            if winner is None:
                # Triple race (winner ended before our re-read). Practically
                # impossible; let the IntegrityError surface as a 500 rather
                # than fabricate a SessionAlreadyActive without a payload.
                raise
            raise SessionAlreadyActive(winner) from None
        return new

    async def end(self, *, user_keycloak_id: UUID, reason: ShiftEndReason) -> ShiftSession:
        """End the user's active shift with ``reason``.

        Raises ``NoActiveShift`` if there isn't one. Last-write-wins on
        concurrent ``end`` for the same user — acceptable per the Task 1 plan;
        no row lock is taken.
        """
        if self._session.in_transaction():
            raise RuntimeError("ShiftSessionService.end called inside an active transaction")

        async with self._session.begin():
            active = await self._repo.get_active_for_user(user_keycloak_id)
            if active is None:
                raise NoActiveShift(user_keycloak_id)
            ended = active.end(reason=reason, at=datetime.now(UTC))
            await self._repo.update(ended)
        return ended
