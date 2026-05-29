"""Domain types — pure Python, no SQLAlchemy or Pydantic.

Domain classes describe the business state. Persistence (``app/db``) and API
boundary (``app/api``) translate to and from these types; they never leak
SQLAlchemy models or Pydantic schemas in either direction.
"""

from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.qr import QR, IllegalQRTransition, QRBatch, QRStatus

__all__ = [
    "QR",
    "AuditLogEntry",
    "AuditResult",
    "IllegalQRTransition",
    "QRBatch",
    "QRStatus",
]
