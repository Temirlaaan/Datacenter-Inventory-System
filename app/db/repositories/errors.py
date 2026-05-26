"""Typed exceptions for repository failures. Callers never import SQLAlchemy."""

from __future__ import annotations


class RepositoryError(Exception):
    """A write to the application DB failed an integrity check.

    Wraps SQLAlchemy ``IntegrityError`` so service-layer callers and the API
    boundary don't need to ``import sqlalchemy``. The original exception is
    preserved on ``__cause__`` via ``raise ... from exc``.
    """
