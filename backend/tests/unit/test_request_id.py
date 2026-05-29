"""Unit tests for app.observability.request_id."""

from __future__ import annotations

import re
from uuid import UUID

import pytest
import structlog

from app.observability.request_id import current_request_id


@pytest.fixture(autouse=True)
def _clear_contextvars() -> None:
    """Ensure each test starts with a clean structlog contextvars dict."""
    structlog.contextvars.clear_contextvars()


def test_current_request_id_returns_bound_value_when_it_is_a_valid_uuid() -> None:
    bound = "8400e7f2-1111-2222-3333-1234567890ab"
    structlog.contextvars.bind_contextvars(request_id=bound)
    assert current_request_id() == bound


def test_current_request_id_mints_uuid_when_bound_value_is_not_a_uuid() -> None:
    # The middleware binds whatever X-Request-ID the client sent; a junk value
    # must not propagate into UUID() downstream.
    structlog.contextvars.bind_contextvars(request_id="not-a-uuid")
    rid = current_request_id()
    assert rid != "not-a-uuid"
    UUID(rid)  # raises if not a valid UUID


def test_current_request_id_mints_uuid_when_unbound() -> None:
    rid = current_request_id()
    # Must parse as a UUID — proves we minted a real one rather than a sentinel.
    UUID(rid)


def test_current_request_id_mints_uuid_when_contextvar_is_not_a_string() -> None:
    # Defensive: if something binds a non-string (a bug), we fall back to a UUID
    # rather than propagating the bad value into outgoing headers / audit rows.
    structlog.contextvars.bind_contextvars(request_id=12345)
    rid = current_request_id()
    UUID(rid)


def test_current_request_id_mints_uuid_when_contextvar_is_empty_string() -> None:
    structlog.contextvars.bind_contextvars(request_id="")
    rid = current_request_id()
    assert re.fullmatch(r"[0-9a-f-]{36}", rid)
