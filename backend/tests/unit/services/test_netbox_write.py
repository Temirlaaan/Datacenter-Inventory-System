"""Unit tests for app.services.netbox_write.NetBoxWriteService.

NetBox is faked with respx; the audit-log repository and session are faked in
process. Audit rows actually landing in Postgres is covered by
tests/integration/test_netbox_write.py.
"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

import pytest
import respx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogRepository
from app.domain.audit import AuditLogEntry, AuditResult
from app.netbox.client import NetBoxClient
from app.netbox.errors import NetBoxClientError, NetBoxServerError
from app.services.netbox_write import (
    NetBoxWriteService,
    WriteConflictError,
    _format_create_journal_comment,
    _format_diff,
    _format_journal_comment,
)

NETBOX_URL = "https://netbox.example.com"
_DEVICE_PATH = "/api/dcim/devices/5/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_VERSION = "2026-05-18T10:00:00.000000Z"
_NEW_VERSION = "2026-05-18T11:30:00.000000Z"
_USER_SUB = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def netbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip retry sleeps so tests aren't gated on real wall time."""
    from app.netbox import client as client_module

    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


# ---------- fakes ----------


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for AsyncSession — the service only ever calls begin()."""

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()


class _RecordingAuditRepo:
    """Captures every AuditLogEntry the service tries to insert."""

    def __init__(self) -> None:
        self.entries: list[AuditLogEntry] = []

    async def insert(self, entry: AuditLogEntry) -> None:
        self.entries.append(entry)


class _FailingAuditRepo:
    """Simulates the app DB being unreachable when the audit row is written."""

    def __init__(self) -> None:
        self.calls = 0

    async def insert(self, entry: AuditLogEntry) -> None:
        self.calls += 1
        raise RuntimeError("audit DB unavailable")


# ---------- helpers ----------


def _user(
    *,
    email: str | None = "alice@example.com",
    shift_session_id: UUID | None = None,
) -> AuthUser:
    # Sprint 6 Task 4: audit row session_id is sourced from shift_session_id
    # (populated by require_role_with_active_shift), not the JWT sid claim.
    return AuthUser(
        sub=_USER_SUB,
        email=email,
        roles=("dcinv-admin",),
        session_id=None,
        shift_session_id=shift_session_id,
    )


def _device(version: str = _VERSION, **overrides: Any) -> dict[str, Any]:
    device = {
        "id": 5,
        "name": "sw-01",
        "status": {"value": "active"},
        "last_updated": version,
    }
    device.update(overrides)
    return device


def _service(client: NetBoxClient, repo: object) -> NetBoxWriteService:
    return NetBoxWriteService(
        client,
        cast(AsyncSession, _FakeSession()),
        cast(AuditLogRepository, repo),
    )


async def _call(
    service: NetBoxWriteService,
    *,
    expected_version: str = _VERSION,
    changes: dict[str, Any] | None = None,
    user: AuthUser | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return await service.patch_with_attribution(
        netbox_path=_DEVICE_PATH,
        netbox_object_type="dcim.device",
        netbox_object_id=5,
        entity_type="device",
        operation="device.update",
        expected_version=expected_version,
        changes={"name": "sw-01-new"} if changes is None else changes,
        user=user or _user(),
        reason=reason,
    )


# ---------- success path ----------


async def test_patch_with_attribution_returns_updated_object_on_success(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    updated = _device(_NEW_VERSION, name="sw-01-new")
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=updated)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            result = await _call(_service(client, repo))

    assert result == updated


async def test_patch_with_attribution_success_writes_one_success_audit_row(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo))

    assert len(repo.entries) == 1
    entry = repo.entries[0]
    assert entry.result is AuditResult.SUCCESS
    assert entry.operation == "device.update"
    assert entry.entity_type == "device"
    assert entry.entity_id == "5"


async def test_patch_with_attribution_success_audit_records_both_versions(
    clean_env: None, netbox_env: None
) -> None:
    """Decision A: the audit row carries the client-expected AND backend-observed versions."""
    repo = _RecordingAuditRepo()
    updated = _device(_NEW_VERSION)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=updated)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo))

    entry = repo.entries[0]
    assert entry.before_json == {"object": _device(), "expected_version": _VERSION}
    assert entry.after_json == {"object": updated, "observed_version": _VERSION}


async def test_patch_with_attribution_sends_changes_as_patch_body(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            patch_route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device(_NEW_VERSION)
            )
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo), changes={"name": "x", "serial": "ABC"})

    assert json.loads(patch_route.calls.last.request.content) == {"name": "x", "serial": "ABC"}


async def test_patch_with_attribution_posts_journal_entry_with_attribution(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            await _call(_service(client, repo))

    body = json.loads(journal_route.calls.last.request.content)
    assert body["assigned_object_type"] == "dcim.device"
    assert body["assigned_object_id"] == 5
    assert body["kind"] == "info"
    assert "alice@example.com" in body["comments"]


async def test_patch_with_attribution_journal_comment_includes_reason_when_provided(
    clean_env: None, netbox_env: None
) -> None:
    """Sprint 7 Task 4: ``reason`` flows from patch_with_attribution through
    _post_journal_entry into the journal POST body's ``comments`` field."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            await _call(_service(client, repo), reason="Rack moved out of service")

    body = json.loads(journal_route.calls.last.request.content)
    assert "Reason: Rack moved out of service" in body["comments"]


# ---------- conflict path ----------


async def test_patch_with_attribution_raises_conflict_on_version_mismatch(
    clean_env: None, netbox_env: None
) -> None:
    """A stale expected_version must abort with no NetBox write (no PATCH/journal routes)."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            with pytest.raises(WriteConflictError) as exc_info:
                await _call(_service(client, repo), expected_version=_VERSION)

    assert exc_info.value.current_version == _NEW_VERSION
    assert exc_info.value.current_object == _device(_NEW_VERSION)


async def test_patch_with_attribution_conflict_writes_conflict_audit_row(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            with pytest.raises(WriteConflictError):
                await _call(_service(client, repo), expected_version=_VERSION)

    assert len(repo.entries) == 1
    entry = repo.entries[0]
    assert entry.result is AuditResult.CONFLICT
    assert entry.before_json == {"expected_version": _VERSION}
    assert entry.after_json == {"object": _device(_NEW_VERSION), "observed_version": _NEW_VERSION}


# ---------- failure path ----------


async def test_patch_with_attribution_writes_failure_audit_when_reread_fails(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await _call(_service(client, repo))

    assert len(repo.entries) == 1
    entry = repo.entries[0]
    assert entry.result is AuditResult.FAILURE
    # The re-read never returned, so there is no object to record.
    assert entry.before_json == {"expected_version": _VERSION}
    assert entry.after_json == {}


async def test_patch_with_attribution_writes_failure_audit_when_patch_fails(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                status_code=400, json={"detail": "bad"}
            )
            with pytest.raises(NetBoxClientError):
                await _call(_service(client, repo))

    entry = repo.entries[0]
    assert entry.result is AuditResult.FAILURE
    # The re-read succeeded before the PATCH failed — its object is recorded.
    assert entry.before_json == {"object": _device(), "expected_version": _VERSION}


async def test_patch_with_attribution_writes_failure_audit_when_reread_response_missing_last_updated(
    clean_env: None, netbox_env: None
) -> None:
    """A re-read that parses but lacks `last_updated` is still a failed write."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            # Valid JSON, but no `last_updated` — observed_version extraction raises.
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json={"id": 5, "name": "sw-01"})
            with pytest.raises(KeyError):
                await _call(_service(client, repo))

    assert len(repo.entries) == 1
    assert repo.entries[0].result is AuditResult.FAILURE
    assert repo.entries[0].before_json == {"expected_version": _VERSION}


# ---------- best-effort attribution (decision B) ----------


async def test_patch_with_attribution_succeeds_when_journal_post_fails(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """A failed journal POST must not roll back or fail the operation — the PATCH stands."""
    repo = _RecordingAuditRepo()
    updated = _device(_NEW_VERSION)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=updated)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=500)
            result = await _call(_service(client, repo))

    assert result == updated
    assert repo.entries[0].result is AuditResult.SUCCESS


async def test_patch_with_attribution_succeeds_when_audit_insert_fails(
    clean_env: None, netbox_env: None
) -> None:
    """A failed audit-row write is logged, not fatal — the operation still returns."""
    repo = _FailingAuditRepo()
    updated = _device(_NEW_VERSION)
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=updated)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            result = await _call(_service(client, repo))

    assert result == updated
    assert repo.calls == 1


# ---------- attribution sourcing ----------


async def test_patch_with_attribution_audit_request_id_matches_contextvar(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    bound_id = "8400e7f2-aaaa-bbbb-cccc-1234567890ab"
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=bound_id)
    try:
        async with NetBoxClient.from_settings() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
                router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
                router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
                await _call(_service(client, repo))
    finally:
        structlog.contextvars.clear_contextvars()

    assert str(repo.entries[0].request_id) == bound_id


async def test_patch_with_attribution_records_session_id_from_active_shift(
    clean_env: None, netbox_env: None
) -> None:
    """Sprint 6 decision D: session_id is sourced from the active shift_sessions
    row populated by ``require_role_with_active_shift``, not the JWT sid claim."""
    repo = _RecordingAuditRepo()
    shift_session_id = UUID("22222222-2222-2222-2222-222222222222")
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo), user=_user(shift_session_id=shift_session_id))

    assert repo.entries[0].session_id == shift_session_id


async def test_patch_with_attribution_records_none_session_id_when_no_shift_id_on_user(
    clean_env: None, netbox_env: None
) -> None:
    """Defensive: if ``shift_session_id`` is absent on the AuthUser (e.g. an
    unauthenticated test wiring) the audit row still inserts with NULL — the
    dep-layer gate is what enforces "no write without shift", not the service.
    """
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo), user=_user(shift_session_id=None))

    assert repo.entries[0].session_id is None


async def test_patch_with_attribution_with_no_email_writes_empty_string_to_audit(
    clean_env: None, netbox_env: None
) -> None:
    # AuthUser.email is str | None; audit_log.user_email is NOT NULL → falls back to "".
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo), user=_user(email=None))

    assert repo.entries[0].user_email == ""


# ---------- WriteConflictError ----------


def test_write_conflict_error_carries_current_state() -> None:
    current = _device(_NEW_VERSION)
    err = WriteConflictError(current_object=current, current_version=_NEW_VERSION)
    assert err.current_object == current
    assert err.current_version == _NEW_VERSION
    assert isinstance(err, Exception)


# ---------- diff / journal-comment formatting ----------


def test_format_diff_returns_empty_string_for_no_changes() -> None:
    assert _format_diff(_device(), {}) == ""


def test_format_diff_renders_one_change_as_old_to_new() -> None:
    assert _format_diff(_device(), {"name": "sw-01-new"}) == "  name: 'sw-01' → 'sw-01-new'"


def test_format_diff_renders_every_changed_field() -> None:
    diff = _format_diff(_device(), {"name": "sw-02", "id": 6})
    assert "  name: 'sw-01' → 'sw-02'" in diff
    assert "  id: 5 → 6" in diff


def test_format_diff_shows_none_for_field_absent_from_original() -> None:
    assert _format_diff(_device(), {"asset_tag": "A-9"}) == "  asset_tag: None → 'A-9'"


def test_format_journal_comment_includes_user_request_and_diff() -> None:
    request_id = UUID("8400e7f2-aaaa-bbbb-cccc-1234567890ab")
    comment = _format_journal_comment(
        user=_user(shift_session_id=UUID("22222222-2222-2222-2222-222222222222")),
        request_id=request_id,
        original=_device(),
        changes={"name": "sw-01-new"},
    )
    assert "Modified by alice@example.com" in comment
    assert f"Request ID: {request_id}" in comment
    assert "Session: 22222222-2222-2222-2222-222222222222" in comment
    assert "name: 'sw-01' → 'sw-01-new'" in comment


def test_format_journal_comment_falls_back_when_email_and_shift_session_absent() -> None:
    comment = _format_journal_comment(
        user=_user(email=None, shift_session_id=None),
        request_id=UUID("8400e7f2-aaaa-bbbb-cccc-1234567890ab"),
        original=_device(),
        changes={"name": "x"},
    )
    assert "Modified by unknown" in comment
    assert "Session: unknown" in comment


def test_format_journal_comment_includes_reason_when_provided() -> None:
    """Sprint 7 Task 4: ``reason`` is rendered between the Session line and
    the Changes block so auditors see the WHY alongside the WHAT."""
    comment = _format_journal_comment(
        user=_user(shift_session_id=UUID("22222222-2222-2222-2222-222222222222")),
        request_id=UUID("8400e7f2-aaaa-bbbb-cccc-1234567890ab"),
        original=_device(),
        changes={"status": "decommissioning"},
        reason="Rack moved out of service",
    )
    assert "Reason: Rack moved out of service\n" in comment
    # Ordering: Reason appears between Session and Changes.
    session_idx = comment.index("Session:")
    reason_idx = comment.index("Reason:")
    changes_idx = comment.index("Changes:")
    assert session_idx < reason_idx < changes_idx


def test_format_journal_comment_omits_reason_line_when_none() -> None:
    """Backward-compatible default: absent line (NOT ``Reason: None``) when
    the caller did not pass a reason. Existing callers stay unchanged."""
    comment = _format_journal_comment(
        user=_user(shift_session_id=UUID("22222222-2222-2222-2222-222222222222")),
        request_id=UUID("8400e7f2-aaaa-bbbb-cccc-1234567890ab"),
        original=_device(),
        changes={"name": "sw-01-new"},
    )
    assert "Reason:" not in comment
    assert "Reason: None" not in comment


# ============================================================================
# post_with_attribution (Sprint 5 Task 1)
# ============================================================================

_CREATE_PATH = "/api/dcim/devices/"


def _create_payload() -> dict[str, Any]:
    return {
        "name": "sw-99",
        "device_type": 11,
        "role": 31,
        "site": 1,
        "status": "active",
    }


def _created_device(device_id: int = 99) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": "sw-99",
        "status": {"value": "active", "label": "Active"},
        "last_updated": _NEW_VERSION,
    }


async def _post_call(
    service: NetBoxWriteService,
    *,
    netbox_path: str = _CREATE_PATH,
    netbox_object_id: int | None = None,
    entity_id: str | None = None,
    operation: str = "device.create",
    payload: dict[str, Any] | None = None,
    user: AuthUser | None = None,
    attach_journal: bool = True,
) -> dict[str, Any]:
    return await service.post_with_attribution(
        netbox_path=netbox_path,
        netbox_object_type="dcim.device",
        netbox_object_id=netbox_object_id,
        entity_type="device",
        entity_id=entity_id,
        operation=operation,
        payload=payload if payload is not None else _create_payload(),
        user=user or _user(),
        attach_journal=attach_journal,
    )


# ---------- POST happy path ----------


async def test_post_with_attribution_returns_created_object_on_success(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    created = _created_device()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(status_code=201, json=created)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            result = await _post_call(_service(client, repo))

    assert result == created


async def test_post_with_attribution_sends_payload_as_post_body(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    payload = {"name": "sw-99", "device_type": 11, "role": 31, "site": 1, "status": "active"}
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            post_route = router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device()
            )
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _post_call(_service(client, repo), payload=payload)

    assert json.loads(post_route.calls.last.request.content) == payload


async def test_post_with_attribution_success_writes_one_success_audit_row(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device()
            )
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _post_call(_service(client, repo))

    assert len(repo.entries) == 1
    entry = repo.entries[0]
    assert entry.result is AuditResult.SUCCESS
    assert entry.operation == "device.create"
    assert entry.entity_type == "device"


async def test_post_with_attribution_success_audit_records_created_object(
    clean_env: None, netbox_env: None
) -> None:
    """Decision: before_json={} (nothing pre-existed); after_json={"object": created}."""
    repo = _RecordingAuditRepo()
    created = _created_device()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(status_code=201, json=created)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _post_call(_service(client, repo))

    entry = repo.entries[0]
    assert entry.before_json == {}
    assert entry.after_json == {"object": created}


async def test_post_with_attribution_attaches_journal_with_created_attribution(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device(device_id=99)
            )
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            await _post_call(_service(client, repo))

    body = json.loads(journal_route.calls.last.request.content)
    assert body["assigned_object_type"] == "dcim.device"
    assert body["assigned_object_id"] == 99
    assert body["kind"] == "info"
    assert "Created by alice@example.com" in body["comments"]
    assert "Object ID: 99" in body["comments"]


# ---------- entity_id resolution ----------


async def test_post_with_attribution_derives_entity_id_from_response_when_none(
    clean_env: None, netbox_env: None
) -> None:
    """Task 2 case: device-create passes entity_id=None; service derives str(created["id"])."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device(device_id=99)
            )
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _post_call(_service(client, repo), entity_id=None)

    assert repo.entries[0].entity_id == "99"


async def test_post_with_attribution_uses_provided_entity_id_when_set(
    clean_env: None, netbox_env: None
) -> None:
    """Task 3 case: add-comment passes entity_id=str(device_id); service preserves it."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}/api/extras/journal-entries/").respond(
                status_code=201, json={"id": 42}
            )
            await _post_call(
                _service(client, repo),
                # add-comment posts straight to the journal endpoint; no separate
                # journal attach. netbox_object_id is the device the comment is for.
                netbox_path=_JOURNAL_PATH,
                netbox_object_id=5,
                entity_id="5",
                operation="device.add_comment",
                payload={
                    "assigned_object_type": "dcim.device",
                    "assigned_object_id": 5,
                    "kind": "info",
                    "comments": "test",
                },
                attach_journal=False,
            )

    assert repo.entries[0].entity_id == "5"


# ---------- attach_journal=False ----------


async def test_post_with_attribution_skips_journal_when_attach_journal_false(
    clean_env: None, netbox_env: None
) -> None:
    """add-comment case: the POST IS the journal entry; don't write a second one."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            # Only one POST route registered: the create itself. If
            # post_with_attribution attempts a journal POST, respx will
            # fail unmatched-request.
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device()
            )
            await _post_call(_service(client, repo), attach_journal=False)

    # Audit row still landed
    assert len(repo.entries) == 1
    assert repo.entries[0].result is AuditResult.SUCCESS


# ---------- journal-target routing ----------


async def test_post_with_attribution_journal_attaches_to_provided_netbox_object_id_when_set(
    clean_env: None, netbox_env: None
) -> None:
    """Hypothetical: POST creates a sub-resource; journal attaches to parent device."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device(device_id=99)
            )
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            # Pass netbox_object_id=5 explicitly — journal should attach there, not to 99.
            await _post_call(_service(client, repo), netbox_object_id=5)

    body = json.loads(journal_route.calls.last.request.content)
    assert body["assigned_object_id"] == 5


async def test_post_with_attribution_journal_attaches_to_created_id_when_netbox_object_id_none(
    clean_env: None, netbox_env: None
) -> None:
    """Task 2 case: device-create has no pre-existing object; journal targets created["id"]."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device(device_id=99)
            )
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            await _post_call(_service(client, repo), netbox_object_id=None)

    body = json.loads(journal_route.calls.last.request.content)
    assert body["assigned_object_id"] == 99


# ---------- failure / best-effort ----------


async def test_post_with_attribution_writes_failure_audit_when_post_fails(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=400, json={"detail": "bad"}
            )
            with pytest.raises(NetBoxClientError):
                await _post_call(_service(client, repo))

    assert len(repo.entries) == 1
    entry = repo.entries[0]
    assert entry.result is AuditResult.FAILURE
    assert entry.before_json == {}
    # `payload` echoed in after_json so the failed input is debuggable
    assert entry.after_json["payload"]["name"] == "sw-99"
    # Caller passed entity_id=None → audit uses "unknown" placeholder
    assert entry.entity_id == "unknown"


async def test_post_with_attribution_failure_audit_uses_provided_entity_id_when_set(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """add-comment-style call: caller-provided entity_id survives a POST failure."""
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}/api/extras/journal-entries/").respond(
                status_code=400, json={"detail": "bad"}
            )
            with pytest.raises(NetBoxClientError):
                await _post_call(
                    _service(client, repo),
                    netbox_path=_JOURNAL_PATH,
                    netbox_object_id=5,
                    entity_id="5",
                    operation="device.add_comment",
                    payload={
                        "assigned_object_type": "dcim.device",
                        "assigned_object_id": 5,
                        "kind": "info",
                        "comments": "test",
                    },
                    attach_journal=False,
                )

    assert repo.entries[0].entity_id == "5"


async def test_post_with_attribution_succeeds_when_journal_post_fails(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """A failed journal POST must not roll back or fail the operation."""
    repo = _RecordingAuditRepo()
    created = _created_device()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(status_code=201, json=created)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=500)
            result = await _post_call(_service(client, repo))

    assert result == created
    assert repo.entries[0].result is AuditResult.SUCCESS


async def test_post_with_attribution_succeeds_when_audit_insert_fails(
    clean_env: None, netbox_env: None
) -> None:
    """A failed audit-row write is logged, not fatal — the operation still returns."""
    repo = _FailingAuditRepo()
    created = _created_device()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(status_code=201, json=created)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            result = await _post_call(_service(client, repo))

    assert result == created
    assert repo.calls == 1


# ---------- attribution sourcing ----------


async def test_post_with_attribution_audit_request_id_matches_contextvar(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    bound_id = "8400e7f2-aaaa-bbbb-cccc-1234567890ab"
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=bound_id)
    try:
        async with NetBoxClient.from_settings() as client:
            with respx.mock(assert_all_called=True) as router:
                router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                    status_code=201, json=_created_device()
                )
                router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
                await _post_call(_service(client, repo))
    finally:
        structlog.contextvars.clear_contextvars()

    assert str(repo.entries[0].request_id) == bound_id


async def test_post_with_attribution_records_session_id_from_active_shift(
    clean_env: None, netbox_env: None
) -> None:
    """Sprint 6 decision D: session_id is sourced from the active shift, not JWT sid."""
    repo = _RecordingAuditRepo()
    shift_session_id = UUID("22222222-2222-2222-2222-222222222222")
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_CREATE_PATH}").respond(
                status_code=201, json=_created_device()
            )
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _post_call(_service(client, repo), user=_user(shift_session_id=shift_session_id))

    assert repo.entries[0].session_id == shift_session_id


# ---------- format helper (pure functions) ----------


def test_format_create_journal_comment_includes_user_request_and_object_id() -> None:
    request_id = UUID("8400e7f2-aaaa-bbbb-cccc-1234567890ab")
    comment = _format_create_journal_comment(
        user=_user(shift_session_id=UUID("22222222-2222-2222-2222-222222222222")),
        request_id=request_id,
        created={"id": 99, "name": "sw-99"},
    )
    assert "Created by alice@example.com" in comment
    assert f"Request ID: {request_id}" in comment
    assert "Session: 22222222-2222-2222-2222-222222222222" in comment
    assert "Object ID: 99" in comment


def test_format_create_journal_comment_falls_back_when_email_and_shift_session_absent() -> None:
    comment = _format_create_journal_comment(
        user=_user(email=None, shift_session_id=None),
        request_id=UUID("8400e7f2-aaaa-bbbb-cccc-1234567890ab"),
        created={"id": 99},
    )
    assert "Created by unknown" in comment
    assert "Session: unknown" in comment
