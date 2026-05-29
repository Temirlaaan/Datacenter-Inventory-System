"""PostgreSQL-backed idempotency for POST endpoints.

Usage::

    async with with_idempotency(session, user.sub, idem_key, payload) as result:
        if result.is_replay:
            return cached_to_http(result.cached_status, result.cached_response)
        # ... do the actual work ...
        await result.record(response_status=201, response_body=response_dict)
        return new_response_to_http(...)

Concurrency
-----------
Two requests with the same ``(user_keycloak_id, key)`` race to INSERT the same
placeholder row. The DB UNIQUE constraint serializes them: only one INSERT
commits, the other raises ``IntegrityError`` (Postgres holds the second INSERT
until the first transaction finishes). The losing side rolls back, re-SELECTs,
and returns the cached response from the winner.

Transactional scope
-------------------
The placeholder INSERT runs inside the *caller's* transaction. If the caller's
work raises and the surrounding session rolls back, the placeholder vanishes
with it, so the next attempt is fresh. This is why the idempotency block must
be the *outermost* DB work in the request handler.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.idempotency import IdempotencyKeyModel


class IdempotencyKeyConflict(Exception):
    """Same idempotency key was previously used with a different payload."""


def _canonical_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 over a canonical JSON form of ``payload``."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyResult:
    """Yielded by ``with_idempotency``. Tells the caller whether to do the work."""

    def __init__(
        self,
        *,
        is_replay: bool,
        cached_status: int | None,
        cached_response: dict[str, Any] | None,
        session: AsyncSession | None,
        user_keycloak_id: UUID,
        key: str,
    ) -> None:
        self.is_replay = is_replay
        self.cached_status = cached_status
        self.cached_response = cached_response
        self._session = session
        self._user_keycloak_id = user_keycloak_id
        self._key = key
        self._recorded = False

    async def record(self, response_status: int, response_body: dict[str, Any]) -> None:
        """Persist the response onto the placeholder row. Must be called exactly
        once on a non-replay result before the caller commits.
        """
        if self.is_replay:
            raise RuntimeError("record() called on a replay result")
        if self._recorded:
            raise RuntimeError("record() called twice")
        assert self._session is not None  # narrowed for type-checker
        await self._session.execute(
            update(IdempotencyKeyModel)
            .where(
                IdempotencyKeyModel.user_keycloak_id == self._user_keycloak_id,
                IdempotencyKeyModel.key == self._key,
            )
            .values(response_status=response_status, response_body=response_body)
        )
        self._recorded = True


@asynccontextmanager
async def with_idempotency(
    session: AsyncSession,
    user_keycloak_id: UUID,
    key: str,
    request_payload: dict[str, Any],
) -> AsyncIterator[IdempotencyResult]:
    """Wrap a POST handler so duplicate ``(user, key)`` returns the cached response."""
    request_hash = _canonical_hash(request_payload)

    # Step 1: check for an already-committed row.
    existing = await session.scalar(
        select(IdempotencyKeyModel).where(
            IdempotencyKeyModel.user_keycloak_id == user_keycloak_id,
            IdempotencyKeyModel.key == key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyKeyConflict()
        yield IdempotencyResult(
            is_replay=True,
            cached_status=existing.response_status,
            cached_response=existing.response_body,
            session=None,
            user_keycloak_id=user_keycloak_id,
            key=key,
        )
        return

    # Step 2: try to claim the slot with a placeholder INSERT.
    try:
        await session.execute(
            insert(IdempotencyKeyModel).values(
                user_keycloak_id=user_keycloak_id,
                key=key,
                request_hash=request_hash,
                response_status=None,
                response_body=None,
            )
        )
    except IntegrityError:
        # Race lost. Postgres only unblocks us once the winner's tx commits, so
        # the winning row is now visible from a fresh transaction.
        await session.rollback()
        winner = await session.scalar(
            select(IdempotencyKeyModel).where(
                IdempotencyKeyModel.user_keycloak_id == user_keycloak_id,
                IdempotencyKeyModel.key == key,
            )
        )
        assert winner is not None  # UNIQUE conflict implies the row is committed
        if winner.request_hash != request_hash:
            raise IdempotencyKeyConflict() from None
        yield IdempotencyResult(
            is_replay=True,
            cached_status=winner.response_status,
            cached_response=winner.response_body,
            session=None,
            user_keycloak_id=user_keycloak_id,
            key=key,
        )
        return

    # We claimed the slot; hand off to the caller.
    yield IdempotencyResult(
        is_replay=False,
        cached_status=None,
        cached_response=None,
        session=session,
        user_keycloak_id=user_keycloak_id,
        key=key,
    )
