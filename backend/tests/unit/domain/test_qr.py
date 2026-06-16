"""Unit tests for app.domain.qr — QR/QRBatch construction invariants and state transitions.

The DB layer (Task 2) enforces the same state-consistency invariant via a CHECK
constraint plus a partial unique index on `bound_to_device_id`. Mirroring the CHECK
in `QR.__post_init__` catches violations at construction time instead of surfacing
them as opaque IntegrityErrors deep in a transaction.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.qr import (
    QR,
    IllegalQRTransition,
    QRBatch,
    QRStatus,
)

# Test fixtures / helpers --------------------------------------------------------

_BATCH_ID = UUID("00000000-0000-0000-0000-000000000001")
_BOUND_AT = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_RETIRED_AT = datetime(2026, 5, 16, 9, 30, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC)


def _free(qr_id: str = "DCQR-ABCDEFGH") -> QR:
    return QR(
        id=qr_id,
        batch_id=_BATCH_ID,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _bound(qr_id: str = "DCQR-ABCDEFGH") -> QR:
    return QR(
        id=qr_id,
        batch_id=_BATCH_ID,
        status=QRStatus.BOUND,
        bound_to_device_id=42,
        bound_at=_BOUND_AT,
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )


def _retired(qr_id: str = "DCQR-ABCDEFGH") -> QR:
    return QR(
        id=qr_id,
        batch_id=_BATCH_ID,
        status=QRStatus.RETIRED,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=_RETIRED_AT,
        retired_reason="damaged",
    )


# QRStatus enum ------------------------------------------------------------------


def test_qr_status_values_are_lowercase_strings() -> None:
    # Lowercase matches the SQL CHECK literals in Task 2 ('free'/'bound'/'retired').
    assert QRStatus.FREE.value == "free"
    assert QRStatus.BOUND.value == "bound"
    assert QRStatus.RETIRED.value == "retired"


# QR.__post_init__ — legal construction -----------------------------------------


def test_qr_construct_free_with_all_optional_fields_none_succeeds() -> None:
    qr = _free()
    assert qr.status is QRStatus.FREE
    assert qr.bound_to_device_id is None
    assert qr.retired_at is None


def test_qr_construct_bound_with_required_binding_fields_succeeds() -> None:
    qr = _bound()
    assert qr.status is QRStatus.BOUND
    assert qr.bound_to_device_id == 42
    assert qr.bound_at == _BOUND_AT
    assert qr.bound_by_email == "alice@example.com"


def test_qr_construct_retired_with_retired_at_succeeds() -> None:
    qr = _retired()
    assert qr.status is QRStatus.RETIRED
    assert qr.retired_at == _RETIRED_AT
    assert qr.retired_reason == "damaged"


def test_qr_construct_retired_allows_null_reason() -> None:
    # ToR §7.2.2 does not mark retired_reason NOT NULL.
    qr = dataclasses.replace(_retired(), retired_reason=None)
    assert qr.retired_reason is None


# QR.__post_init__ — illegal construction ---------------------------------------


def test_qr_construct_free_with_bound_to_device_id_raises_value_error() -> None:
    with pytest.raises(ValueError, match="free"):
        QR(
            id="DCQR-ABCDEFGH",
            batch_id=_BATCH_ID,
            status=QRStatus.FREE,
            bound_to_device_id=42,
            bound_at=None,
            bound_by_email=None,
            retired_at=None,
            retired_reason=None,
        )


def test_qr_construct_free_with_retired_at_raises_value_error() -> None:
    with pytest.raises(ValueError, match="free"):
        QR(
            id="DCQR-ABCDEFGH",
            batch_id=_BATCH_ID,
            status=QRStatus.FREE,
            bound_to_device_id=None,
            bound_at=None,
            bound_by_email=None,
            retired_at=_RETIRED_AT,
            retired_reason=None,
        )


def test_qr_construct_bound_without_bound_to_device_id_raises_value_error() -> None:
    with pytest.raises(ValueError, match="bound"):
        QR(
            id="DCQR-ABCDEFGH",
            batch_id=_BATCH_ID,
            status=QRStatus.BOUND,
            bound_to_device_id=None,
            bound_at=_BOUND_AT,
            bound_by_email="alice@example.com",
            retired_at=None,
            retired_reason=None,
        )


def test_qr_construct_bound_with_retired_at_raises_value_error() -> None:
    with pytest.raises(ValueError, match="bound"):
        QR(
            id="DCQR-ABCDEFGH",
            batch_id=_BATCH_ID,
            status=QRStatus.BOUND,
            bound_to_device_id=42,
            bound_at=_BOUND_AT,
            bound_by_email="alice@example.com",
            retired_at=_RETIRED_AT,
            retired_reason=None,
        )


def test_qr_construct_retired_without_retired_at_raises_value_error() -> None:
    with pytest.raises(ValueError, match="retired"):
        QR(
            id="DCQR-ABCDEFGH",
            batch_id=_BATCH_ID,
            status=QRStatus.RETIRED,
            bound_to_device_id=None,
            bound_at=None,
            bound_by_email=None,
            retired_at=None,
            retired_reason="damaged",
        )


# Legal transitions --------------------------------------------------------------


def test_qr_bind_from_free_returns_new_bound_qr_with_supplied_fields() -> None:
    original = _free()

    bound = original.bind(device_id=99, by_email="bob@example.com", at=_BOUND_AT)

    assert bound is not original
    assert bound.status is QRStatus.BOUND
    assert bound.bound_to_device_id == 99
    assert bound.bound_at == _BOUND_AT
    assert bound.bound_by_email == "bob@example.com"
    assert bound.retired_at is None
    # Original is untouched (frozen dataclass).
    assert original.status is QRStatus.FREE
    assert original.bound_to_device_id is None


def test_qr_retire_from_free_returns_new_retired_qr() -> None:
    original = _free()

    retired = original.retire(reason="unused", at=_RETIRED_AT)

    assert retired is not original
    assert retired.status is QRStatus.RETIRED
    assert retired.retired_at == _RETIRED_AT
    assert retired.retired_reason == "unused"
    assert original.status is QRStatus.FREE


def test_qr_retire_from_bound_preserves_historical_binding_fields() -> None:
    # Preserving bound_* on retire is intentional: forensics/audit need to see who
    # held this QR before it was retired.
    original = _bound()

    retired = original.retire(reason="device decommissioned", at=_RETIRED_AT)

    assert retired.status is QRStatus.RETIRED
    assert retired.retired_at == _RETIRED_AT
    assert retired.retired_reason == "device decommissioned"
    # Historical binding fields preserved.
    assert retired.bound_to_device_id == 42
    assert retired.bound_at == _BOUND_AT
    assert retired.bound_by_email == "alice@example.com"


def test_qr_retire_accepts_null_reason() -> None:
    retired = _free().retire(reason=None, at=_RETIRED_AT)
    assert retired.retired_reason is None


# Illegal transitions ------------------------------------------------------------


def test_qr_bind_from_bound_raises_illegal_qr_transition() -> None:
    qr = _bound()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.bind(device_id=100, by_email="eve@example.com", at=_LATER)

    assert "bound" in str(exc.value)


def test_qr_bind_from_retired_raises_illegal_qr_transition() -> None:
    qr = _retired()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.bind(device_id=100, by_email="eve@example.com", at=_LATER)

    assert "retired" in str(exc.value)
    assert "bound" in str(exc.value)


def test_qr_retire_from_retired_raises_illegal_qr_transition() -> None:
    qr = _retired()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.retire(reason="again", at=_LATER)

    assert "retired" in str(exc.value)


# Restore (RETIRED → FREE) -------------------------------------------------------


def test_qr_restore_from_retired_returns_clean_free_qr() -> None:
    """RETIRED → FREE clears retired_at, retired_reason. State invariants are
    satisfied (free => null bound + null retired_at)."""
    retired = _retired()

    restored = retired.restore()

    assert restored.status is QRStatus.FREE
    assert restored.bound_to_device_id is None
    assert restored.bound_at is None
    assert restored.bound_by_email is None
    assert restored.retired_at is None
    assert restored.retired_reason is None
    # Same id / batch_id so the row maps to itself.
    assert restored.id == retired.id
    assert restored.batch_id == retired.batch_id


def test_qr_restore_does_not_resurrect_prior_binding() -> None:
    """If the QR was BOUND → RETIRED, restore() returns FREE — historical
    bound_to_device_id is NOT auto-rebound. Safer default: don't recreate
    a binding that might collide with another QR captured by that device
    in the meantime."""
    # Manually construct a retired-with-historical-binding case. The domain
    # invariant allows RETIRED to carry residual bound_to_device_id from a
    # BOUND → RETIRED transition (see QR.__post_init__).
    retired_was_bound = QR(
        id="DCQR-WASBOUND",
        batch_id=_BATCH_ID,
        status=QRStatus.RETIRED,
        bound_to_device_id=42,  # residual from prior binding
        bound_at=_BOUND_AT,
        bound_by_email="engineer@example.com",
        retired_at=_RETIRED_AT,
        retired_reason="decommissioned",
    )

    restored = retired_was_bound.restore()

    assert restored.status is QRStatus.FREE
    # The historical binding is NOT carried over.
    assert restored.bound_to_device_id is None
    assert restored.bound_at is None
    assert restored.bound_by_email is None


def test_qr_restore_from_free_raises_illegal_qr_transition() -> None:
    """restore() is RETIRED-only; FREE/BOUND → FREE is meaningless."""
    qr = _free()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.restore()

    assert "free" in str(exc.value)


def test_qr_restore_from_bound_raises_illegal_qr_transition() -> None:
    qr = _bound()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.restore()

    assert "bound" in str(exc.value)


# Rebind (BOUND → BOUND, change device) -----------------------------------------


def test_qr_rebind_moves_binding_to_new_device_and_refreshes_attribution() -> None:
    """BOUND → BOUND with a different device_id; bound_at / bound_by_email are
    refreshed to the rebind moment (the binding is now owned by whoever
    moved it)."""
    bound = _bound()  # bound to device 100 by default helper

    rebound = bound.rebind(device_id=777, by_email="mover@example.com", at=_LATER)

    assert rebound.status is QRStatus.BOUND
    assert rebound.bound_to_device_id == 777
    assert rebound.bound_at == _LATER
    assert rebound.bound_by_email == "mover@example.com"
    assert rebound.retired_at is None
    # Original untouched (frozen dataclass).
    assert bound.bound_to_device_id != 777


def test_qr_rebind_from_free_raises_illegal_qr_transition() -> None:
    qr = _free()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.rebind(device_id=5, by_email="x@example.com", at=_LATER)

    assert "free" in str(exc.value)


def test_qr_rebind_from_retired_raises_illegal_qr_transition() -> None:
    qr = _retired()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.rebind(device_id=5, by_email="x@example.com", at=_LATER)

    assert "retired" in str(exc.value)


# Unbind (BOUND → FREE) ----------------------------------------------------------


def test_qr_unbind_returns_clean_free_qr() -> None:
    """BOUND → FREE clears bound_to_device_id / bound_at / bound_by_email."""
    bound = _bound()

    freed = bound.unbind()

    assert freed.status is QRStatus.FREE
    assert freed.bound_to_device_id is None
    assert freed.bound_at is None
    assert freed.bound_by_email is None
    assert freed.retired_at is None
    assert freed.id == bound.id
    # Original untouched (frozen).
    assert bound.bound_to_device_id is not None


def test_qr_unbind_from_free_raises_illegal_qr_transition() -> None:
    qr = _free()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.unbind()

    assert "free" in str(exc.value)


def test_qr_unbind_from_retired_raises_illegal_qr_transition() -> None:
    qr = _retired()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.unbind()

    assert "retired" in str(exc.value)


def test_illegal_qr_transition_message_contains_from_and_to_states() -> None:
    qr = _retired()

    with pytest.raises(IllegalQRTransition) as exc:
        qr.bind(device_id=100, by_email="eve@example.com", at=_LATER)

    message = str(exc.value)
    assert "retired" in message
    assert "bound" in message


# Frozen / equality semantics ----------------------------------------------------


def test_qr_is_frozen_and_rejects_attribute_assignment() -> None:
    qr = _free()
    with pytest.raises(dataclasses.FrozenInstanceError):
        qr.status = QRStatus.BOUND  # type: ignore[misc]


def test_qr_equality_holds_for_identical_field_values() -> None:
    assert _free() == _free()
    assert _bound() == _bound()


def test_qr_inequality_when_fields_differ() -> None:
    assert _free("DCQR-AAAAAAAA") != _free("DCQR-BBBBBBBB")


def test_qr_is_hashable_and_usable_in_a_set() -> None:
    qrs = {_free(), _free(), _bound()}
    assert len(qrs) == 2


# QRBatch ------------------------------------------------------------------------


def test_qr_batch_constructs_with_all_tor_fields_and_default_pdf_path() -> None:
    batch_id = uuid4()
    created_at = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)

    batch = QRBatch(
        id=batch_id,
        created_at=created_at,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=50,
        intended_site_id=1,
        intended_location_id=2,
        intended_rack_id=3,
        comment="rack 14 spares",
    )

    assert batch.id == batch_id
    assert batch.created_at == created_at
    assert batch.count == 50
    assert batch.intended_site_id == 1
    assert batch.intended_location_id == 2
    assert batch.intended_rack_id == 3
    assert batch.comment == "rack 14 spares"
    # pdf_path defaults to None — populated when PDF generation lands in a later sprint.
    assert batch.pdf_path is None


def test_qr_batch_allows_all_optional_fields_to_be_none() -> None:
    batch = QRBatch(
        id=uuid4(),
        created_at=datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC),
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=10,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    assert batch.intended_site_id is None
    assert batch.comment is None
    assert batch.pdf_path is None


def test_qr_batch_is_frozen() -> None:
    batch = QRBatch(
        id=uuid4(),
        created_at=datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC),
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=10,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        batch.count = 20  # type: ignore[misc]
