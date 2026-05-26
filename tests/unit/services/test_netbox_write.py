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


def _user(*, email: str | None = "alice@example.com", session_id: str | None = None) -> AuthUser:
    return AuthUser(sub=_USER_SUB, email=email, roles=("dcinv-admin",), session_id=session_id)


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


async def test_patch_with_attribution_records_session_id_from_auth_user(
    clean_env: None, netbox_env: None
) -> None:
    """Decision C: session_id comes from the JWT sid claim carried on AuthUser."""
    repo = _RecordingAuditRepo()
    session_id = "22222222-2222-2222-2222-222222222222"
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo), user=_user(session_id=session_id))

    assert str(repo.entries[0].session_id) == session_id


async def test_patch_with_attribution_records_none_session_id_when_absent(
    clean_env: None, netbox_env: None
) -> None:
    repo = _RecordingAuditRepo()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device())
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json=_device(_NEW_VERSION))
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            await _call(_service(client, repo), user=_user(session_id=None))

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
        user=_user(session_id="22222222-2222-2222-2222-222222222222"),
        request_id=request_id,
        original=_device(),
        changes={"name": "sw-01-new"},
    )
    assert "Modified by alice@example.com" in comment
    assert f"Request ID: {request_id}" in comment
    assert "Session: 22222222-2222-2222-2222-222222222222" in comment
    assert "name: 'sw-01' → 'sw-01-new'" in comment


def test_format_journal_comment_falls_back_when_email_and_session_absent() -> None:
    comment = _format_journal_comment(
        user=_user(email=None, session_id=None),
        request_id=UUID("8400e7f2-aaaa-bbbb-cccc-1234567890ab"),
        original=_device(),
        changes={"name": "x"},
    )
    assert "Modified by unknown" in comment
    assert "Session: unknown" in comment
