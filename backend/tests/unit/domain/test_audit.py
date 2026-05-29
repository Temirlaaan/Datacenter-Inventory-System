"""Unit tests for app.domain.audit — AuditResult enum values + AuditLogEntry shape."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.audit import AuditLogEntry, AuditResult

_REQUEST_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_KEYCLOAK_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SESSION_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _entry(**overrides: object) -> AuditLogEntry:
    base = dict(
        request_id=_REQUEST_ID,
        timestamp=_NOW,
        user_email="alice@example.com",
        user_keycloak_id=_KEYCLOAK_ID,
        session_id=_SESSION_ID,
        operation="qr.generate_batch",
        entity_type="batch",
        entity_id="some-batch-id",
        before_json={},
        after_json={"count": 10},
        result=AuditResult.SUCCESS,
    )
    base.update(overrides)
    return AuditLogEntry(**base)  # type: ignore[arg-type]


def test_audit_result_values_are_lowercase_strings() -> None:
    # Must match the SQL enum literals (`success`, `failure`, `conflict`) defined
    # in the Task 2 migration.
    assert AuditResult.SUCCESS.value == "success"
    assert AuditResult.FAILURE.value == "failure"
    assert AuditResult.CONFLICT.value == "conflict"


def test_audit_log_entry_constructs_with_all_required_fields() -> None:
    entry = _entry()
    assert entry.request_id == _REQUEST_ID
    assert entry.user_email == "alice@example.com"
    assert entry.operation == "qr.generate_batch"
    assert entry.result is AuditResult.SUCCESS
    assert entry.before_json == {}
    assert entry.after_json == {"count": 10}


def test_audit_log_entry_allows_null_session_id() -> None:
    # session_id is nullable until shift_sessions lands (ToR §7.2.3 / Sprint 2 plan).
    entry = _entry(session_id=None)
    assert entry.session_id is None


def test_audit_log_entry_is_frozen_and_rejects_attribute_assignment() -> None:
    entry = _entry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.result = AuditResult.FAILURE  # type: ignore[misc]
