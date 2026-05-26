"""QR domain types — pure Python, no SQLAlchemy or Pydantic.

The state-consistency invariant enforced in ``QR.__post_init__`` mirrors the
``qr_state_consistency`` CHECK constraint defined by the Task 2 migration:

    free    => bound_to_device_id IS NULL    AND retired_at IS NULL
    bound   => bound_to_device_id IS NOT NULL AND retired_at IS NULL
    retired => retired_at IS NOT NULL

Mirroring the CHECK in code catches violations at the call site instead of
surfacing them as opaque IntegrityErrors deep in a transaction.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class QRStatus(StrEnum):
    """Lifecycle state of a QR code. Values match the PostgreSQL enum literals."""

    FREE = "free"
    BOUND = "bound"
    RETIRED = "retired"


class IllegalQRTransition(Exception):
    """Raised when a QR state transition violates the ToR §4.2.3 state machine."""

    def __init__(self, from_status: QRStatus, to_status: QRStatus) -> None:
        super().__init__(f"illegal QR transition: {from_status.value} -> {to_status.value}")
        self.from_status = from_status
        self.to_status = to_status


@dataclass(frozen=True, slots=True)
class QR:
    """A single QR code in the registry. Fields per ToR §7.2.2."""

    id: str
    batch_id: UUID
    status: QRStatus
    bound_to_device_id: int | None
    bound_at: datetime | None
    bound_by_email: str | None
    retired_at: datetime | None
    retired_reason: str | None

    def __post_init__(self) -> None:
        if self.status is QRStatus.FREE:
            if self.bound_to_device_id is not None or self.retired_at is not None:
                raise ValueError("free QR must have null bound_to_device_id and null retired_at")
        elif self.status is QRStatus.BOUND:
            if self.bound_to_device_id is None or self.retired_at is not None:
                raise ValueError(
                    "bound QR must have non-null bound_to_device_id and null retired_at"
                )
        else:  # QRStatus.RETIRED
            if self.retired_at is None:
                raise ValueError("retired QR must have non-null retired_at")

    def bind(self, *, device_id: int, by_email: str, at: datetime) -> QR:
        """Transition FREE -> BOUND. Returns a new QR; the original is unchanged."""
        if self.status is not QRStatus.FREE:
            raise IllegalQRTransition(self.status, QRStatus.BOUND)
        return dataclasses.replace(
            self,
            status=QRStatus.BOUND,
            bound_to_device_id=device_id,
            bound_at=at,
            bound_by_email=by_email,
        )

    def retire(self, *, reason: str | None, at: datetime) -> QR:
        """Transition FREE/BOUND -> RETIRED. Historical bound_* fields are preserved
        on a BOUND -> RETIRED transition so audit/forensics can trace prior ownership.
        """
        if self.status is QRStatus.RETIRED:
            raise IllegalQRTransition(self.status, QRStatus.RETIRED)
        return dataclasses.replace(
            self,
            status=QRStatus.RETIRED,
            retired_at=at,
            retired_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class QRBatch:
    """A batch of QR codes generated in one operation. Fields per ToR §7.2.1."""

    id: UUID
    created_at: datetime
    created_by_email: str
    created_by_keycloak_id: UUID
    count: int
    intended_site_id: int | None
    intended_location_id: int | None
    intended_rack_id: int | None
    comment: str | None
    pdf_path: str | None = None
