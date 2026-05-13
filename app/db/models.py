"""SQLAlchemy declarative Base. Real models land in Sprint 2 (QR registry, audit log)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Common base for all SQLAlchemy models. Holds the metadata Alembic diffs against."""
