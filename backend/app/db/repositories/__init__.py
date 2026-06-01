"""Repository layer: async, session-injected, returns domain types.

Repositories do not own transactions — they take a pre-existing ``AsyncSession``
and leave commit/rollback to the caller so multi-step service operations can
atomic-commit a batch + codes + audit row together (see Sprint 2 Task 6).
"""

from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.errors import RepositoryError
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.repositories.shift_session import ShiftSessionRepository

__all__ = [
    "AuditLogRepository",
    "QRBatchRepository",
    "QRCodeRepository",
    "RepositoryError",
    "ShiftSessionRepository",
]
