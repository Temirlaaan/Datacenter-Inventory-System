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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


# ---------- Sprint 9 Task 0: separate-session idempotency wrapper -------------
#
# The original ``with_idempotency`` above shares the caller's transaction —
# the placeholder INSERT and the work commit atomically. That works cleanly
# for ``POST /admin/batches/`` (Sprint 5) where the service was deliberately
# designed to join the caller's tx.
#
# Sprint 6+ services (ShiftSession, QRLifecycle, DeviceDecommission) open
# their OWN ``async with session.begin()`` blocks and commit themselves —
# refactoring them to caller-managed tx ripples through ~15+ existing tests
# per service. Instead, Sprint 9 Task 0 adds a separate-session idempotency
# wrapper that:
#
# - reads an existing idempotency row in its own session (concurrent-safe via
#   the (user, key) primary key)
# - runs the work via the caller's existing service-managed-tx style
# - records the response in a third session after work completes
#
# Trade-off: between "check" and "record" two concurrent retries can both run
# the work. For the mobile retry scenario (seconds-long backoff) the race
# window is in microseconds and effectively never trips. Downstream
# protections (qr_codes partial unique index, OCC version checks, NetBox
# 409s) catch the residual cases.


@dataclass(frozen=True, slots=True)
class IdempotentReplay:
    """A cached response found by :func:`check_idempotency_replay`."""

    status: int
    body: dict[str, Any]


async def check_idempotency_replay(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_keycloak_id: UUID,
    key: str,
    request_payload: dict[str, Any],
) -> IdempotentReplay | None:
    """Look up an existing idempotency row in a fresh session.

    Returns the cached response if found AND the payload matches, ``None``
    if this is a first call (or the previous attempt is mid-flight without
    a recorded response yet — caller treats that as a fresh attempt).

    Raises :class:`IdempotencyKeyConflict` when the key was previously used
    with a different payload.
    """
    request_hash = _canonical_hash(request_payload)
    async with sessionmaker() as sess:
        existing = await sess.scalar(
            select(IdempotencyKeyModel).where(
                IdempotencyKeyModel.user_keycloak_id == user_keycloak_id,
                IdempotencyKeyModel.key == key,
            )
        )
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyKeyConflict()
        if existing.response_status is None or existing.response_body is None:
            # Placeholder from a concurrent in-flight call (or the original
            # transaction crashed between insert and record). Treat as
            # "no replay" — caller re-runs the work; downstream protections
            # catch corruption-style duplicates.
            return None
        return IdempotentReplay(
            status=existing.response_status, body=existing.response_body
        )


async def store_idempotency_response(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_keycloak_id: UUID,
    key: str,
    request_payload: dict[str, Any],
    response_status: int,
    response_body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Persist the response after the work has completed.

    Returns the response the client should actually receive. Usually that's
    the one passed in (this call wrote the row); on a race with a concurrent
    first call that beat us to INSERT, returns the winner's cached response
    instead so both callers see a consistent answer.

    Raises :class:`IdempotencyKeyConflict` if the winning row happens to
    have a different payload hash (pathological: someone else used the
    same key with different data while we were running).
    """
    request_hash = _canonical_hash(request_payload)
    async with sessionmaker() as sess:
        try:
            await sess.execute(
                insert(IdempotencyKeyModel).values(
                    user_keycloak_id=user_keycloak_id,
                    key=key,
                    request_hash=request_hash,
                    response_status=response_status,
                    response_body=response_body,
                )
            )
            await sess.commit()
            return response_status, response_body
        except IntegrityError:
            await sess.rollback()
            winner = await sess.scalar(
                select(IdempotencyKeyModel).where(
                    IdempotencyKeyModel.user_keycloak_id == user_keycloak_id,
                    IdempotencyKeyModel.key == key,
                )
            )
            assert winner is not None  # UNIQUE conflict implies the row exists
            if winner.request_hash != request_hash:
                raise IdempotencyKeyConflict() from None
            if winner.response_status is None or winner.response_body is None:
                # Winner placed a placeholder but hasn't recorded yet — our
                # call has actually completed work, so surface our response
                # rather than waiting on theirs.
                return response_status, response_body
            return winner.response_status, winner.response_body


async def with_optional_idempotency_outer(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    user_keycloak_id: UUID,
    idempotency_key: str | None,
    request_payload: dict[str, Any],
    do_work: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
) -> tuple[int, dict[str, Any]]:
    """End-to-end optional idempotency wrapper for endpoints whose services
    manage their own transaction (Sprint 9 Task 0).

    Endpoint pattern::

        async def handler(..., idempotency_key=Header(None, alias="Idempotency-Key"),
                          sessionmaker=Depends(get_sessionmaker)):
            async def _do_work():
                try:
                    obj = await service.do_thing(...)
                    return 201, ResponseModel.from_obj(obj).model_dump(mode="json")
                except DomainConflict as exc:
                    return 409, {"error": {"code": "...", ...}}

            status_code, body = await with_optional_idempotency_outer(
                sessionmaker=sessionmaker,
                user_keycloak_id=UUID(user.sub),
                idempotency_key=idempotency_key,
                request_payload=request.model_dump(mode="json"),
                do_work=_do_work,
            )
            return JSONResponse(body, status_code=status_code)

    When ``idempotency_key`` is ``None`` the wrapper is a passthrough —
    current behaviour is unchanged for callers that don't opt in.

    Raises ``HTTPException(422)`` with detail ``"Idempotency-Key reused with
    a different request payload"`` on key-payload mismatch (mirrors the
    Sprint 5 ``create_batch`` semantics).
    """
    if idempotency_key is None:
        return await do_work()

    try:
        replay = await check_idempotency_replay(
            sessionmaker, user_keycloak_id, idempotency_key, request_payload
        )
    except IdempotencyKeyConflict as exc:
        from fastapi import HTTPException
        from fastapi import status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key reused with a different request payload",
        ) from exc
    if replay is not None:
        return replay.status, replay.body

    response_status, response_body = await do_work()

    try:
        return await store_idempotency_response(
            sessionmaker,
            user_keycloak_id,
            idempotency_key,
            request_payload,
            response_status,
            response_body,
        )
    except IdempotencyKeyConflict as exc:
        from fastapi import HTTPException
        from fastapi import status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key reused with a different request payload",
        ) from exc
