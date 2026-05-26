"""SQLAlchemy declarative Base and model registry.

Importing the model modules from here registers them with ``Base.metadata`` so
Alembic's autogenerate (and any future schema-reflection tests) sees the full
table set. Individual modules are kept slim — one concept per file.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Common base for all SQLAlchemy models. Holds the metadata Alembic diffs against."""


# Import after Base is defined so module-level mappers can resolve it. Order does
# not matter beyond that — no model cross-references another model class.
from app.db.models.audit import AuditLogModel  # noqa: E402
from app.db.models.idempotency import IdempotencyKeyModel  # noqa: E402
from app.db.models.qr import QRBatchModel, QRCodeModel  # noqa: E402

__all__ = [
    "AuditLogModel",
    "Base",
    "IdempotencyKeyModel",
    "QRBatchModel",
    "QRCodeModel",
]
