"""Unit tests for app.domain.shift_session — construction invariants and the
end() transition.

The DB layer (Task 1 migration) enforces the same end_at/end_reason pairing via
a CHECK constraint and the "one active per user" rule via a partial unique
index. Mirroring the CHECK in ``ShiftSession.__post_init__`` catches violations
at construction time instead of surfacing them as opaque IntegrityErrors deep
in a transaction.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.shift_session import (
    IllegalShiftTransition,
    ShiftEndReason,
    ShiftSession,
)

_USER_ID = UUID("11111111-1111-1111-1111-111111111111")
_START_AT = datetime(2026, 5, 29, 9, 0, 0, tzinfo=UTC)
_END_AT = datetime(2026, 5, 29, 17, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 30, 9, 0, 0, tzinfo=UTC)


def _active(session_id: UUID | None = None) -> ShiftSession:
    return ShiftSession(
        id=session_id or UUID("22222222-2222-2222-2222-222222222222"),
        user_email="alice@example.com",
        user_keycloak_id=_USER_ID,
        shift_start_at=_START_AT,
        shift_end_at=None,
        tablet_id="tablet-01",
        end_reason=None,
    )


def _ended(reason: ShiftEndReason = ShiftEndReason.MANUAL) -> ShiftSession:
    return ShiftSession(
        id=UUID("33333333-3333-3333-3333-333333333333"),
        user_email="alice@example.com",
        user_keycloak_id=_USER_ID,
        shift_start_at=_START_AT,
        shift_end_at=_END_AT,
        tablet_id="tablet-01",
        end_reason=reason,
    )


# ShiftEndReason enum -----------------------------------------------------------


def test_shift_end_reason_values_are_lowercase_strings() -> None:
    # Lowercase matches the Postgres enum literals defined in the migration.
    assert ShiftEndReason.MANUAL.value == "manual"
    assert ShiftEndReason.INACTIVITY_TIMEOUT.value == "inactivity_timeout"
    assert ShiftEndReason.ADMIN_FORCE_CLOSE.value == "admin_force_close"


# ShiftSession.__post_init__ — legal construction -------------------------------


def test_shift_session_construct_active_with_null_end_fields_succeeds() -> None:
    session = _active()
    assert session.shift_end_at is None
    assert session.end_reason is None


def test_shift_session_construct_ended_with_paired_end_fields_succeeds() -> None:
    session = _ended()
    assert session.shift_end_at == _END_AT
    assert session.end_reason is ShiftEndReason.MANUAL


def test_shift_session_construct_ended_with_inactivity_timeout_succeeds() -> None:
    session = _ended(ShiftEndReason.INACTIVITY_TIMEOUT)
    assert session.end_reason is ShiftEndReason.INACTIVITY_TIMEOUT


def test_shift_session_construct_ended_with_admin_force_close_succeeds() -> None:
    session = _ended(ShiftEndReason.ADMIN_FORCE_CLOSE)
    assert session.end_reason is ShiftEndReason.ADMIN_FORCE_CLOSE


# ShiftSession.__post_init__ — illegal construction -----------------------------


def test_shift_session_construct_active_with_end_reason_raises_value_error() -> None:
    with pytest.raises(ValueError, match="active"):
        ShiftSession(
            id=uuid4(),
            user_email="alice@example.com",
            user_keycloak_id=_USER_ID,
            shift_start_at=_START_AT,
            shift_end_at=None,
            tablet_id="tablet-01",
            end_reason=ShiftEndReason.MANUAL,
        )


def test_shift_session_construct_ended_without_end_reason_raises_value_error() -> None:
    with pytest.raises(ValueError, match="ended"):
        ShiftSession(
            id=uuid4(),
            user_email="alice@example.com",
            user_keycloak_id=_USER_ID,
            shift_start_at=_START_AT,
            shift_end_at=_END_AT,
            tablet_id="tablet-01",
            end_reason=None,
        )


# is_active property ------------------------------------------------------------


def test_shift_session_is_active_true_when_end_at_is_none() -> None:
    assert _active().is_active is True


def test_shift_session_is_active_false_when_end_at_is_set() -> None:
    assert _ended().is_active is False


# Legal transitions -------------------------------------------------------------


def test_shift_session_end_from_active_with_manual_returns_new_ended_session() -> None:
    original = _active()

    ended = original.end(reason=ShiftEndReason.MANUAL, at=_END_AT)

    assert ended is not original
    assert ended.shift_end_at == _END_AT
    assert ended.end_reason is ShiftEndReason.MANUAL
    assert ended.is_active is False
    # Original is untouched (frozen dataclass).
    assert original.shift_end_at is None
    assert original.end_reason is None
    assert original.is_active is True


def test_shift_session_end_from_active_with_inactivity_timeout_succeeds() -> None:
    original = _active()
    ended = original.end(reason=ShiftEndReason.INACTIVITY_TIMEOUT, at=_END_AT)
    assert ended.end_reason is ShiftEndReason.INACTIVITY_TIMEOUT


def test_shift_session_end_from_active_with_admin_force_close_succeeds() -> None:
    original = _active()
    ended = original.end(reason=ShiftEndReason.ADMIN_FORCE_CLOSE, at=_END_AT)
    assert ended.end_reason is ShiftEndReason.ADMIN_FORCE_CLOSE


def test_shift_session_end_preserves_identity_user_and_tablet_fields() -> None:
    original = _active()
    ended = original.end(reason=ShiftEndReason.MANUAL, at=_END_AT)
    assert ended.id == original.id
    assert ended.user_email == original.user_email
    assert ended.user_keycloak_id == original.user_keycloak_id
    assert ended.shift_start_at == original.shift_start_at
    assert ended.tablet_id == original.tablet_id


# Illegal transitions -----------------------------------------------------------


def test_shift_session_end_from_already_ended_raises_illegal_shift_transition() -> None:
    session = _ended()

    with pytest.raises(IllegalShiftTransition) as exc:
        session.end(reason=ShiftEndReason.MANUAL, at=_LATER)

    assert "ended" in str(exc.value).lower() or "active" in str(exc.value).lower()


def test_illegal_shift_transition_message_is_informative() -> None:
    session = _ended()

    with pytest.raises(IllegalShiftTransition) as exc:
        session.end(reason=ShiftEndReason.MANUAL, at=_LATER)

    message = str(exc.value)
    assert len(message) > 0


# Frozen / equality semantics ---------------------------------------------------


def test_shift_session_is_frozen_and_rejects_attribute_assignment() -> None:
    session = _active()
    with pytest.raises(dataclasses.FrozenInstanceError):
        session.tablet_id = "tablet-99"  # type: ignore[misc]


def test_shift_session_equality_holds_for_identical_field_values() -> None:
    assert _active() == _active()
    assert _ended() == _ended()


def test_shift_session_inequality_when_fields_differ() -> None:
    assert _active(UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")) != _active(
        UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    )


def test_shift_session_is_hashable_and_usable_in_a_set() -> None:
    sessions = {_active(), _active(), _ended()}
    assert len(sessions) == 2
