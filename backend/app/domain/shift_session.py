"""Shift-session domain types — pure Python, no SQLAlchemy or Pydantic.

The end-state invariant enforced in ``ShiftSession.__post_init__`` mirrors the
``shift_end_consistency`` CHECK constraint defined by the Sprint 6 Task 1
migration:

    active  => shift_end_at IS NULL     AND end_reason IS NULL
    ended   => shift_end_at IS NOT NULL AND end_reason IS NOT NULL

Mirroring the CHECK in code catches violations at the call site instead of
surfacing them as opaque IntegrityErrors deep in a transaction. Same pattern
as ``app.domain.qr.QR``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ShiftEndReason(StrEnum):
    """How a shift was ended. Values match the ``shift_end_reason`` Postgres enum.

    ``admin_force_close`` is reserved for the Sprint 7+ admin endpoint; Sprint 6's
    ``POST /sessions/end`` accepts only ``manual`` and ``inactivity_timeout``.
    """

    MANUAL = "manual"
    INACTIVITY_TIMEOUT = "inactivity_timeout"
    ADMIN_FORCE_CLOSE = "admin_force_close"


class IllegalShiftTransition(Exception):
    """Raised when ``end()`` is called on a session that is already ended."""


@dataclass(frozen=True, slots=True)
class ShiftSession:
    """A single shift session. Fields per ToR §7.2.4 + decision H of Sprint 6 plan."""

    id: UUID
    user_email: str
    user_keycloak_id: UUID
    shift_start_at: datetime
    shift_end_at: datetime | None
    tablet_id: str
    end_reason: ShiftEndReason | None

    def __post_init__(self) -> None:
        if self.shift_end_at is None:
            if self.end_reason is not None:
                raise ValueError("active shift must have null end_reason")
        else:
            if self.end_reason is None:
                raise ValueError("ended shift must have non-null end_reason")

    @property
    def is_active(self) -> bool:
        return self.shift_end_at is None

    def end(self, *, reason: ShiftEndReason, at: datetime) -> ShiftSession:
        """Transition active -> ended. Returns a new ShiftSession; original is unchanged."""
        if not self.is_active:
            raise IllegalShiftTransition("cannot end a shift that is already ended")
        return dataclasses.replace(self, shift_end_at=at, end_reason=reason)
