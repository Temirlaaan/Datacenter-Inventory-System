"""Audit-log domain types — pure Python, no SQLAlchemy or Pydantic.

``AuditResult`` lives here (not in ``app/db/models/audit.py``) so the model layer
imports from domain, mirroring the QR pattern. The ``audit_result`` Postgres
enum literals match the lowercase string values defined below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AuditResult(StrEnum):
    """Outcome recorded against each audit_log row."""

    SUCCESS = "success"
    FAILURE = "failure"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class AuditLogEntry:
    """A single audit-log row. Fields per ToR §7.2.3.

    Insert path: pass ``id=None`` (the default); BIGSERIAL is populated DB-side.
    Read path: ``id`` carries the persisted primary key — added in Sprint 7
    Task 2 when the audit-log query endpoint started reading rows back.

    The ``before_json`` / ``after_json`` dicts are stored as JSONB. The frozen
    dataclass holds a reference to whatever dict the caller passes; mutating it
    after construction is the caller's mistake.
    """

    request_id: UUID
    timestamp: datetime
    user_email: str
    user_keycloak_id: UUID
    session_id: UUID | None
    operation: str
    entity_type: str
    entity_id: str
    before_json: dict[str, Any]
    after_json: dict[str, Any]
    result: AuditResult
    id: int | None = None
