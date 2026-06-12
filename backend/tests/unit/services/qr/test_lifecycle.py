"""Unit tests for app.services.qr.lifecycle.QRLifecycleService — bind().

Strategy: fake the QRCodeRepository, AuditLogRepository, AsyncSession, and
NetBoxWriteService; use respx to mock compensation HTTP calls (GET/PATCH the
device, optional inconsistency journal POST). Audit rows written by
``patch_with_attribution`` are out of scope here — those are covered by
``test_netbox_write.py``. This file only verifies what the lifecycle service
writes (compensation audit rows on Branches 2/3).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
import respx
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.qr import QR, QRStatus
from app.netbox.client import NetBoxClient
from app.netbox.errors import NetBoxClientError, NetBoxNotFound
from app.services.netbox_write import NetBoxWriteService, WriteConflictError
from app.services.qr.lifecycle import (
    MissingVersionError,
    QRAlreadyBoundError,
    QRBindInconsistencyError,
    QRBindRolledBackError,
    QRLifecycleService,
    QRNotFoundError,
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
)

NETBOX_URL = "https://netbox.example.com"
_DEVICE_ID = 99
_DEVICE_PATH = f"/api/dcim/devices/{_DEVICE_ID}/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_QR_ID = "DCQR-FREEKLM2"
_VERSION = "2026-05-21T08:00:00.000000Z"
_NEW_VERSION = "2026-05-21T09:00:00.000000Z"
_USER_SUB = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def netbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.netbox import client as client_module

    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


# ---------- fakes ----------


class _FakeTx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTx:
        self._session._in_tx = True
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        self._session._in_tx = False
        if exc_type is not None:
            self._session.rollbacks += 1
            return False  # propagate
        # Pre-check transactions (read-only, before the NetBox PATCH) always
        # succeed; ``next_commit_raises`` applies to commits after the first
        # ``commits_to_succeed_first`` — matches bind()'s flow where the
        # Step A pre-check commits first, then the Step C bind tx is the one
        # tests want to fail.
        self._session.commits += 1
        if (
            self._session.commits > self._session.commits_to_succeed_first
            and self._session.next_commit_raises is not None
        ):
            err = self._session.next_commit_raises
            self._session.next_commit_raises = None
            raise err
        return False


class _FakeSession:
    """Stand-in for AsyncSession with controllable commit failures."""

    def __init__(self, *, in_transaction: bool = False) -> None:
        self._in_tx = in_transaction
        self.next_commit_raises: Exception | None = None
        # Number of leading commits that always succeed regardless of
        # ``next_commit_raises``. Defaults to 1 to model bind()'s pre-check
        # commit (Step A); the bind-tx commit (Step C) is then commit #2.
        self.commits_to_succeed_first: int = 1
        self.commits = 0
        self.rollbacks = 0

    def in_transaction(self) -> bool:
        return self._in_tx

    def begin(self) -> _FakeTx:
        return _FakeTx(self)


class _FakeQRCodeRepo:
    def __init__(self) -> None:
        self.by_id: dict[str, QR] = {}
        # If a key is present, get_by_id_for_update returns this value (may be
        # None for "disappeared" race) regardless of by_id.
        self.locked_override: dict[str, QR | None] = {}
        self.next_update_raises: Exception | None = None
        self.updates: list[QR] = []

    async def get_by_id(self, qr_id: str) -> QR | None:
        return self.by_id.get(qr_id)

    async def get_by_id_for_update(self, qr_id: str) -> QR | None:
        if qr_id in self.locked_override:
            return self.locked_override[qr_id]
        return self.by_id.get(qr_id)

    async def update(self, qr: QR) -> None:
        if self.next_update_raises is not None:
            err = self.next_update_raises
            self.next_update_raises = None
            raise err
        self.updates.append(qr)
        self.by_id[qr.id] = qr


class _FakeAuditLogRepo:
    def __init__(self) -> None:
        self.entries: list[AuditLogEntry] = []
        self.raises: Exception | None = None

    async def insert(self, entry: AuditLogEntry) -> None:
        if self.raises is not None:
            raise self.raises
        self.entries.append(entry)


class _FakeWriteService:
    """Fake ``NetBoxWriteService.patch_with_attribution`` — records calls."""

    def __init__(self) -> None:
        self.return_value: dict[str, Any] = _device_dict(_NEW_VERSION, qr_id=_QR_ID)
        self.raises: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def patch_with_attribution(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.return_value


# ---------- helpers ----------


def _user(
    *,
    email: str | None = "alice@example.com",
    shift_session_id: UUID | None = None,
) -> AuthUser:
    # Sprint 6 Task 4 step b2: audit row session_id is sourced from
    # shift_session_id (populated by require_role_with_active_shift), not
    # the JWT sid claim.
    return AuthUser(
        sub=_USER_SUB,
        email=email,
        roles=("dcinv-mobile-user",),
        session_id=None,
        shift_session_id=shift_session_id,
    )


def _free_qr(qr_id: str = _QR_ID) -> QR:
    return QR(
        id=qr_id,
        batch_id=UUID("33333333-3333-3333-3333-333333333333"),
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _bound_qr(qr_id: str = _QR_ID, device_id: int = _DEVICE_ID) -> QR:
    return QR(
        id=qr_id,
        batch_id=UUID("33333333-3333-3333-3333-333333333333"),
        status=QRStatus.BOUND,
        bound_to_device_id=device_id,
        bound_at=datetime(2026, 5, 21, tzinfo=UTC),
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )


def _retired_qr(qr_id: str = _QR_ID) -> QR:
    return QR(
        id=qr_id,
        batch_id=UUID("33333333-3333-3333-3333-333333333333"),
        status=QRStatus.RETIRED,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=datetime(2026, 5, 21, tzinfo=UTC),
        retired_reason="damaged",
    )


def _device_dict(version: str, *, qr_id: str | None = None) -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "dev-99",
        "last_updated": version,
        "custom_fields": {"qr_id": qr_id},
    }


def _build_service(
    netbox: NetBoxClient,
    *,
    session: _FakeSession | None = None,
    qr_repo: _FakeQRCodeRepo | None = None,
    audit_repo: _FakeAuditLogRepo | None = None,
    write_service: _FakeWriteService | None = None,
) -> tuple[
    QRLifecycleService,
    _FakeSession,
    _FakeQRCodeRepo,
    _FakeAuditLogRepo,
    _FakeWriteService,
]:
    session = session or _FakeSession()
    qr_repo = qr_repo or _FakeQRCodeRepo()
    audit_repo = audit_repo or _FakeAuditLogRepo()
    write_service = write_service or _FakeWriteService()
    service = QRLifecycleService(
        netbox_client=netbox,
        session=cast(AsyncSession, session),
        qr_code_repo=cast(QRCodeRepository, qr_repo),
        audit_log_repo=cast(AuditLogRepository, audit_repo),
        write_service=cast(NetBoxWriteService, write_service),
    )
    return service, session, qr_repo, audit_repo, write_service


@contextlib.contextmanager
def _bind_request_id(rid: str) -> Any:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=rid)
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()


# ========== Step A pre-validation ==========


async def test_bind_raises_qr_not_found_when_token_missing(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        # qr_repo has no entries
        with pytest.raises(QRNotFoundError):
            await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())
    # No NetBox PATCH attempted.
    assert write_service.calls == []


async def test_bind_raises_qr_state_conflict_when_already_bound(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        with pytest.raises(QRStateConflictError) as exc_info:
            await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())
    assert exc_info.value.current_status is QRStatus.BOUND
    assert write_service.calls == []


async def test_bind_raises_qr_state_conflict_when_retired(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _retired_qr()
        with pytest.raises(QRStateConflictError) as exc_info:
            await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())
    assert exc_info.value.current_status is QRStatus.RETIRED
    assert write_service.calls == []


async def test_bind_raises_runtime_error_when_called_in_active_transaction(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        session = _FakeSession(in_transaction=True)
        qr_repo = _FakeQRCodeRepo()
        qr_repo.by_id[_QR_ID] = _free_qr()
        service, _s, _q, _a, write_service = _build_service(
            client, session=session, qr_repo=qr_repo
        )
        with pytest.raises(RuntimeError, match="active transaction"):
            await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())
    assert write_service.calls == []


# ========== Step B propagation ==========


async def test_bind_propagates_write_conflict_error(clean_env: None, netbox_env: None) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        current = _device_dict(_NEW_VERSION, qr_id=None)
        write_service.raises = WriteConflictError(
            current_object=current, current_version=_NEW_VERSION
        )

        with pytest.raises(WriteConflictError) as exc_info:
            await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    # No compensation, no UPDATE, no compensation audit row.
    assert exc_info.value.current_version == _NEW_VERSION
    assert qr_repo.updates == []
    assert audit_repo.entries == []


async def test_bind_propagates_netbox_not_found_for_unknown_device(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        write_service.raises = NetBoxNotFound("device 99 not found")

        with pytest.raises(NetBoxNotFound):
            await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())
    assert qr_repo.updates == []
    assert audit_repo.entries == []


async def test_bind_propagates_netbox_client_error_on_netbox_failure(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        write_service.raises = NetBoxClientError("netbox down")

        with pytest.raises(NetBoxClientError):
            await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())
    assert qr_repo.updates == []
    assert audit_repo.entries == []


# ========== Happy path (Branch 1) ==========


async def test_bind_returns_bound_qr_and_device_on_happy_path(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()

        bound, device = await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    assert bound.status is QRStatus.BOUND
    assert bound.bound_to_device_id == _DEVICE_ID
    assert bound.bound_by_email == "alice@example.com"
    assert device == write_service.return_value
    assert qr_repo.updates == [bound]
    # Two commits: the Step A pre-check tx + the Step C bind tx.
    assert session.commits == 2
    # No compensation audit row on the happy path; the regular SUCCESS row is
    # written inside patch_with_attribution (faked here).
    assert audit_repo.entries == []


async def test_bind_calls_patch_with_attribution_with_qr_bind_metadata(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()

        await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    call = write_service.calls[0]
    assert call["netbox_path"] == _DEVICE_PATH
    assert call["netbox_object_type"] == "dcim.device"
    assert call["netbox_object_id"] == _DEVICE_ID
    assert call["entity_type"] == "qr"
    assert call["operation"] == "qr.bind"
    assert call["expected_version"] == _VERSION
    assert call["changes"] == {"custom_fields": {"qr_id": _QR_ID}}


# ========== Branch 2: compensation succeeded ==========


async def test_bind_db_commit_fails_compensation_cleared_raises_rolled_back(
    clean_env: None, netbox_env: None
) -> None:
    """Branch 2 happy: NetBox shows our token → compensation PATCHes to clear."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)  # shows our token
            )
            patch_route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with pytest.raises(QRBindRolledBackError) as exc_info:
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    import json as _json

    body = _json.loads(patch_route.calls.last.request.content)
    assert body == {"custom_fields": {"qr_id": None}}

    assert exc_info.value.qr_id == _QR_ID
    assert exc_info.value.device_id == _DEVICE_ID
    # Compensation audit row landed
    assert len(audit_repo.entries) == 1
    entry = audit_repo.entries[0]
    assert entry.result is AuditResult.FAILURE
    assert entry.operation == "qr.bind"
    assert entry.entity_type == "qr"
    assert entry.entity_id == _QR_ID
    assert entry.after_json["failure_stage"] == "db_commit"
    assert entry.after_json["compensation_outcome"] == "cleared"
    assert "compensation_error" not in entry.after_json


async def test_bind_db_commit_fails_compensation_logs_expected_event(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, _a, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with structlog.testing.capture_logs() as logs, pytest.raises(QRBindRolledBackError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    events = [log.get("event") for log in logs]
    assert "qr_bind_db_failed_netbox_compensated" in events
    # Branch 2 — must NOT have the Branch 3 critical event
    assert "qr_bind_inconsistency_unrecoverable" not in events


async def test_bind_compensation_noop_when_device_already_has_different_qr(
    clean_env: None, netbox_env: None
) -> None:
    """Correction 3: NetBox shows a different qr_id (concurrent winner) → no PATCH."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        # assert_all_called=False because the no-op path must NOT call PATCH;
        # the registered patch_route is a tripwire we assert never fires.
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id="DCQR-OTHERWIN")
            )
            patch_route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}")
            with structlog.testing.capture_logs() as logs, pytest.raises(QRBindRolledBackError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    # The PATCH route was registered but must NOT have been called.
    assert patch_route.call_count == 0
    events = [log.get("event") for log in logs]
    assert "qr_bind_compensation_noop" in events
    assert "qr_bind_db_failed_netbox_compensated" in events
    assert audit_repo.entries[0].after_json["compensation_outcome"] == "noop_different_qr"


async def test_bind_state_race_under_lock_triggers_compensation(
    clean_env: None, netbox_env: None
) -> None:
    """Concurrent bind happened between pre-check and FOR UPDATE: state is BOUND under lock."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        # Under the lock, the QR is now BOUND (concurrent bind committed first).
        qr_repo.locked_override[_QR_ID] = _bound_qr(device_id=999)

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with pytest.raises(QRBindRolledBackError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    assert audit_repo.entries[0].after_json["failure_stage"] == "db_commit"
    assert audit_repo.entries[0].after_json["compensation_outcome"] == "cleared"
    # The UPDATE never happened (state check raised first).
    assert qr_repo.updates == []


async def test_bind_state_race_under_lock_qr_disappeared_triggers_compensation(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _audit, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        qr_repo.locked_override[_QR_ID] = None  # disappeared under the lock

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with pytest.raises(QRBindRolledBackError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())


async def test_bind_qr_one_per_device_race_raises_already_bound(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        # Simulate IntegrityError on update (qr_one_per_device race)
        qr_repo.next_update_raises = IntegrityError(
            "unique violation", params=None, orig=Exception("qr_one_per_device")
        )

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with pytest.raises(QRAlreadyBoundError) as exc_info:
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    assert exc_info.value.qr_id == _QR_ID
    assert exc_info.value.device_id == _DEVICE_ID
    assert audit_repo.entries[0].after_json["failure_stage"] == "db_commit"


# ========== Branch 3: compensation failed ==========


async def test_bind_compensation_get_fails_raises_inconsistency(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """Branch 3a: the compensation GET to NetBox fails (e.g. 500)."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=500)
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            with (
                structlog.testing.capture_logs() as logs,
                pytest.raises(QRBindInconsistencyError) as exc_info,
            ):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    events = [log.get("event") for log in logs]
    assert "qr_bind_inconsistency_unrecoverable" in events
    assert exc_info.value.qr_id == _QR_ID
    # Inconsistency journal was posted with kind=danger
    import json as _json

    body = _json.loads(journal_route.calls.last.request.content)
    assert body["kind"] == "danger"
    assert _QR_ID in body["comments"]
    assert "INCONSISTENCY" in body["comments"]
    # Compensation audit row
    entry = audit_repo.entries[0]
    assert entry.after_json["failure_stage"] == "compensation"
    assert entry.after_json["compensation_outcome"] == "failed"
    assert "compensation_error" in entry.after_json


async def test_bind_compensation_patch_fails_raises_inconsistency(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """Branch 3b: GET succeeds + shows our token, but the clearing PATCH fails."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=500)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            with pytest.raises(QRBindInconsistencyError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    assert audit_repo.entries[0].after_json["failure_stage"] == "compensation"


async def test_bind_branch_3_inconsistency_journal_failure_is_swallowed(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """A journal-POST failure on the inconsistency entry must not change the response."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, _audit, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=500)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=500)
            with structlog.testing.capture_logs() as logs, pytest.raises(QRBindInconsistencyError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    events = [log.get("event") for log in logs]
    assert "qr_bind_inconsistency_journal_failed" in events
    assert "qr_bind_inconsistency_unrecoverable" in events


# ========== Best-effort compensation audit ==========


async def test_bind_compensation_audit_write_failure_is_swallowed(
    clean_env: None, netbox_env: None
) -> None:
    """An audit-write failure logs but does not change the terminal exception."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")
        audit_repo.raises = RuntimeError("audit db down")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with structlog.testing.capture_logs() as logs, pytest.raises(QRBindRolledBackError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    events = [log.get("event") for log in logs]
    assert "compensation_audit_write_failed" in events
    assert audit_repo.entries == []  # nothing persisted


# ========== Attribution sourcing on the compensation audit row ==========


async def test_bind_compensation_audit_request_id_matches_contextvar(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        bound_id = "8400e7f2-aaaa-bbbb-cccc-1234567890ab"
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with _bind_request_id(bound_id), pytest.raises(QRBindRolledBackError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user())

    assert str(audit_repo.entries[0].request_id) == bound_id


async def test_bind_compensation_audit_records_user_attribution(
    clean_env: None, netbox_env: None
) -> None:
    """Sprint 6 decision D: compensation audit row sources session_id from
    the active shift on AuthUser, not the JWT sid claim."""
    shift_session_id = UUID("22222222-2222-2222-2222-222222222222")
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with pytest.raises(QRBindRolledBackError):
                await service.bind(
                    _QR_ID, _DEVICE_ID, _VERSION, _user(shift_session_id=shift_session_id)
                )

    entry = audit_repo.entries[0]
    assert entry.user_email == "alice@example.com"
    assert entry.user_keycloak_id == UUID(_USER_SUB)
    assert entry.session_id == shift_session_id


async def test_bind_compensation_audit_user_with_no_email_writes_empty_string(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            with pytest.raises(QRBindRolledBackError):
                await service.bind(_QR_ID, _DEVICE_ID, _VERSION, _user(email=None))

    assert audit_repo.entries[0].user_email == ""


# ========== Exception classes carry context ==========


def test_qr_not_found_error_carries_id() -> None:
    err = QRNotFoundError(_QR_ID)
    assert err.qr_id == _QR_ID


def test_qr_state_conflict_error_carries_current_status() -> None:
    err = QRStateConflictError(QRStatus.BOUND)
    assert err.current_status is QRStatus.BOUND


def test_qr_already_bound_error_carries_ids() -> None:
    err = QRAlreadyBoundError(_QR_ID, _DEVICE_ID)
    assert err.qr_id == _QR_ID
    assert err.device_id == _DEVICE_ID


def test_qr_bind_rolled_back_error_carries_ids() -> None:
    err = QRBindRolledBackError(_QR_ID, _DEVICE_ID)
    assert err.qr_id == _QR_ID
    assert err.device_id == _DEVICE_ID


def test_qr_bind_inconsistency_error_carries_ids() -> None:
    err = QRBindInconsistencyError(_QR_ID, _DEVICE_ID)
    assert err.qr_id == _QR_ID
    assert err.device_id == _DEVICE_ID


def test_missing_version_error_carries_qr_id() -> None:
    err = MissingVersionError(_QR_ID)
    assert err.qr_id == _QR_ID


def test_qr_retire_rolled_back_error_carries_ids() -> None:
    err = QRRetireRolledBackError(_QR_ID, _DEVICE_ID)
    assert err.qr_id == _QR_ID
    assert err.device_id == _DEVICE_ID


def test_qr_retire_inconsistency_error_carries_ids() -> None:
    err = QRRetireInconsistencyError(_QR_ID, _DEVICE_ID)
    assert err.qr_id == _QR_ID
    assert err.device_id == _DEVICE_ID


# ========== retire — pre-validation ==========


async def test_retire_raises_qr_not_found_when_token_missing(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, _qr, _a, write_service = _build_service(client)
        with pytest.raises(QRNotFoundError):
            await service.retire(_QR_ID, _VERSION, _user())
    assert write_service.calls == []


async def test_retire_raises_qr_state_conflict_when_already_retired(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _retired_qr()
        with pytest.raises(QRStateConflictError) as exc_info:
            await service.retire(_QR_ID, _VERSION, _user())
    assert exc_info.value.current_status is QRStatus.RETIRED
    assert write_service.calls == []


async def test_retire_raises_runtime_error_when_called_in_active_transaction(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        session = _FakeSession(in_transaction=True)
        qr_repo = _FakeQRCodeRepo()
        qr_repo.by_id[_QR_ID] = _free_qr()
        service, _s, _q, _a, write_service = _build_service(
            client, session=session, qr_repo=qr_repo
        )
        with pytest.raises(RuntimeError, match="active transaction"):
            await service.retire(_QR_ID, _VERSION, _user())
    assert write_service.calls == []


# ========== retire — FREE path ==========


async def test_retire_free_transitions_to_retired_with_zero_netbox_calls(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()

        # respx mock with no routes — any NetBox call would fail
        with respx.mock(assert_all_called=False) as router:
            retired, updated_device = await service.retire(_QR_ID, None, _user())

        assert router.calls.call_count == 0

    assert retired.status is QRStatus.RETIRED
    assert retired.bound_to_device_id is None
    # FREE path: no NetBox PATCH, so updated_device is None
    # (Sprint 5 Task 4 contract — pins the tuple shape for decommission callers).
    assert updated_device is None
    assert qr_repo.updates == [retired]
    assert write_service.calls == []


async def test_retire_free_writes_atomic_success_audit_row(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()

        await service.retire(_QR_ID, None, _user())

    assert len(audit_repo.entries) == 1
    entry = audit_repo.entries[0]
    assert entry.result is AuditResult.SUCCESS
    assert entry.operation == "qr.retire"
    assert entry.entity_type == "qr"
    assert entry.entity_id == _QR_ID
    assert entry.before_json == {"status": "free"}
    assert entry.after_json == {"status": "retired"}


async def test_retire_free_with_version_provided_silently_ignores_it(
    clean_env: None, netbox_env: None
) -> None:
    """A version on a FREE retire is harmless overhead — no warning, no error."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()

        retired, updated_device = await service.retire(_QR_ID, "any-version-string", _user())

    assert retired.status is QRStatus.RETIRED
    assert updated_device is None  # FREE path: still no device dict regardless of version arg
    assert write_service.calls == []  # still no NetBox call


async def test_retire_free_state_race_under_lock_raises_state_conflict(
    clean_env: None, netbox_env: None
) -> None:
    """Concurrent retire/bind landed in the gap between pre-check and FOR UPDATE."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        # Under the lock, the QR is now BOUND.
        qr_repo.locked_override[_QR_ID] = _bound_qr(device_id=42)

        with pytest.raises(QRStateConflictError) as exc_info:
            await service.retire(_QR_ID, None, _user())
    assert exc_info.value.current_status is QRStatus.BOUND


async def test_retire_free_qr_disappeared_under_lock_raises_qr_not_found(
    clean_env: None, netbox_env: None
) -> None:
    """Defensive: ``locked is None`` under FOR UPDATE in the FREE path.

    Unreachable per Sprint 2's "QR IDs are never reused / never deleted"
    invariant, but the M2 review-fix swapped the misleading
    ``QRStateConflictError(RETIRED)`` for a truthful ``QRNotFoundError``.
    This test pins that behaviour so the M2 fix can't regress.
    """
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()
        qr_repo.locked_override[_QR_ID] = None  # disappeared

        with pytest.raises(QRNotFoundError):
            await service.retire(_QR_ID, None, _user())


# ========== restore — RETIRED → FREE undo path ==========


async def test_restore_retired_transitions_to_free_with_zero_netbox_calls(
    clean_env: None, netbox_env: None
) -> None:
    """Pure app-DB operation — no NetBox PATCH, no journal entry."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _retired_qr()

        with respx.mock(assert_all_called=False) as router:
            restored = await service.restore(_QR_ID, _user())

        assert router.calls.call_count == 0

    assert restored.status is QRStatus.FREE
    assert restored.bound_to_device_id is None
    assert restored.retired_at is None
    assert restored.retired_reason is None
    assert qr_repo.updates == [restored]
    assert write_service.calls == []


async def test_restore_writes_atomic_success_audit_row_with_prior_state(
    clean_env: None, netbox_env: None
) -> None:
    """Audit row captures the retired→free transition. before_json carries
    the retired_at / retired_reason / prior_bound_to_device_id so an
    auditor can correlate this restore to the original retire by
    request_id / entity_id."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _retired_qr()

        await service.restore(_QR_ID, _user())

    assert len(audit_repo.entries) == 1
    entry = audit_repo.entries[0]
    assert entry.result is AuditResult.SUCCESS
    assert entry.operation == "qr.restore"
    assert entry.entity_type == "qr"
    assert entry.entity_id == _QR_ID
    assert entry.before_json["status"] == "retired"
    # The retired_at / retired_reason from the now-undone retire are captured.
    assert entry.before_json["retired_at"] is not None
    assert "retired_reason" in entry.before_json
    assert entry.after_json == {"status": "free"}


async def test_restore_unknown_qr_id_raises_qr_not_found(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, _qr, _a, _ws = _build_service(client)

        with pytest.raises(QRNotFoundError):
            await service.restore("DCQR-NOT-EXIST", _user())


async def test_restore_on_free_qr_raises_state_conflict(
    clean_env: None, netbox_env: None
) -> None:
    """Restoring an already-FREE QR is a UX error (admin clicked Restore
    on the wrong row) — surface it to the caller, who maps it to a flash."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _free_qr()

        with pytest.raises(QRStateConflictError) as exc_info:
            await service.restore(_QR_ID, _user())
    assert exc_info.value.current_status is QRStatus.FREE


async def test_restore_on_bound_qr_raises_state_conflict(
    clean_env: None, netbox_env: None
) -> None:
    """BOUND → FREE via restore is not allowed — admin must explicitly
    retire then restore, or decommission via the device endpoint."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr(device_id=42)

        with pytest.raises(QRStateConflictError) as exc_info:
            await service.restore(_QR_ID, _user())
    assert exc_info.value.current_status is QRStatus.BOUND


# ========== retire — BOUND path: Step B errors ==========


async def test_retire_bound_raises_missing_version_when_version_none(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        with pytest.raises(MissingVersionError) as exc_info:
            await service.retire(_QR_ID, None, _user())
    assert exc_info.value.qr_id == _QR_ID
    assert write_service.calls == []  # no NetBox call


async def test_retire_bound_propagates_write_conflict_error(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        current = _device_dict(_NEW_VERSION, qr_id=_QR_ID)
        write_service.raises = WriteConflictError(
            current_object=current, current_version=_NEW_VERSION
        )

        with pytest.raises(WriteConflictError):
            await service.retire(_QR_ID, _VERSION, _user())
    # No QR mutation, no compensation audit row.
    assert qr_repo.updates == []
    assert audit_repo.entries == []


async def test_retire_bound_propagates_netbox_not_found(clean_env: None, netbox_env: None) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        write_service.raises = NetBoxNotFound("device gone")

        with pytest.raises(NetBoxNotFound):
            await service.retire(_QR_ID, _VERSION, _user())


async def test_retire_bound_propagates_netbox_client_error(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        write_service.raises = NetBoxClientError("netbox down")

        with pytest.raises(NetBoxClientError):
            await service.retire(_QR_ID, _VERSION, _user())


# ========== retire — BOUND path: happy ==========


async def test_retire_bound_happy_path_returns_retired_qr(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, write_service = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()

        retired, updated_device = await service.retire(_QR_ID, _VERSION, _user())

    assert retired.status is QRStatus.RETIRED
    # BOUND path: updated_device is patch_with_attribution's return value
    # (Sprint 5 Task 4 contract — decommission reads .last_updated for OCC chain).
    assert updated_device == write_service.return_value
    assert qr_repo.updates == [retired]
    assert len(write_service.calls) == 1
    call = write_service.calls[0]
    assert call["operation"] == "qr.retire"
    assert call["entity_id"] == _QR_ID
    assert call["changes"] == {"custom_fields": {"qr_id": None}}
    # No compensation audit row on happy path; patch_with_attribution's
    # regular audit is faked here.
    assert audit_repo.entries == []
    # Two commits: Step A pre-check + Step C retire tx.
    assert session.commits == 2


# ========== retire — BOUND path: three-branch compensation ==========


async def test_retire_bound_db_commit_fails_compensation_restored_raises_rolled_back(
    clean_env: None, netbox_env: None
) -> None:
    """Branch 2 happy: NetBox shows None → compensation PATCHes qr_id back."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)  # cleared by retire's PATCH
            )
            patch_route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            with pytest.raises(QRRetireRolledBackError) as exc_info:
                await service.retire(_QR_ID, _VERSION, _user())

    import json as _json

    body = _json.loads(patch_route.calls.last.request.content)
    assert body == {"custom_fields": {"qr_id": _QR_ID}}  # token restored
    assert exc_info.value.qr_id == _QR_ID
    assert exc_info.value.device_id == _DEVICE_ID
    # Compensation audit row landed with operation="qr.retire"
    assert len(audit_repo.entries) == 1
    entry = audit_repo.entries[0]
    assert entry.result is AuditResult.FAILURE
    assert entry.operation == "qr.retire"
    assert entry.entity_id == _QR_ID
    assert entry.after_json["failure_stage"] == "db_commit"
    assert entry.after_json["compensation_outcome"] == "restored"


async def test_retire_bound_compensation_logs_expected_event(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, _a, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            with structlog.testing.capture_logs() as logs, pytest.raises(QRRetireRolledBackError):
                await service.retire(_QR_ID, _VERSION, _user())

    events = [log.get("event") for log in logs]
    assert "qr_retire_db_failed_netbox_compensated" in events
    assert "qr_retire_inconsistency_unrecoverable" not in events


async def test_retire_bound_compensation_noop_when_device_has_token(
    clean_env: None, netbox_env: None
) -> None:
    """Symmetric to bind's no-op: NetBox shows non-None → don't restore (don't clobber)."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id="DCQR-OTHERWIN")
            )
            patch_route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}")
            with structlog.testing.capture_logs() as logs, pytest.raises(QRRetireRolledBackError):
                await service.retire(_QR_ID, _VERSION, _user())

    assert patch_route.call_count == 0  # restore PATCH never fired
    events = [log.get("event") for log in logs]
    assert "qr_retire_compensation_noop" in events
    assert audit_repo.entries[0].after_json["compensation_outcome"] == "noop_already_restored"


async def test_retire_bound_state_race_under_lock_triggers_compensation(
    clean_env: None, netbox_env: None
) -> None:
    """FOR UPDATE re-check finds the QR not BOUND (concurrent retire beat us)."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        qr_repo.locked_override[_QR_ID] = _retired_qr()  # already retired under lock

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            with pytest.raises(QRRetireRolledBackError):
                await service.retire(_QR_ID, _VERSION, _user())

    assert audit_repo.entries[0].after_json["failure_stage"] == "db_commit"
    assert audit_repo.entries[0].after_json["compensation_outcome"] == "restored"


async def test_retire_bound_compensation_get_fails_raises_inconsistency(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """Branch 3a: the compensation GET fails."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=500)
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            with (
                structlog.testing.capture_logs() as logs,
                pytest.raises(QRRetireInconsistencyError),
            ):
                await service.retire(_QR_ID, _VERSION, _user())

    events = [log.get("event") for log in logs]
    assert "qr_retire_inconsistency_unrecoverable" in events
    import json as _json

    body = _json.loads(journal_route.calls.last.request.content)
    assert body["kind"] == "danger"
    assert "retire" in body["comments"]
    assert _QR_ID in body["comments"]
    entry = audit_repo.entries[0]
    assert entry.operation == "qr.retire"
    assert entry.after_json["failure_stage"] == "compensation"
    assert entry.after_json["compensation_outcome"] == "failed"


async def test_retire_bound_compensation_patch_fails_raises_inconsistency(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """Branch 3b: GET shows None + PATCH (the restore) fails."""
    async with NetBoxClient.from_settings() as client:
        service, session, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        session.next_commit_raises = RuntimeError("commit failed")

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=500)
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 1})
            with pytest.raises(QRRetireInconsistencyError):
                await service.retire(_QR_ID, _VERSION, _user())

    assert audit_repo.entries[0].after_json["failure_stage"] == "compensation"
    assert audit_repo.entries[0].operation == "qr.retire"


async def test_retire_bound_qr_disappears_under_lock_triggers_compensation(
    clean_env: None, netbox_env: None
) -> None:
    """Defensive: ``locked is None`` under FOR UPDATE — unreachable per Sprint 2's
    "QR IDs are never reused / never deleted" invariant, but still produces
    correct compensation behaviour if the invariant breaks."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, audit_repo, _ws = _build_service(client)
        qr_repo.by_id[_QR_ID] = _bound_qr()
        qr_repo.locked_override[_QR_ID] = None  # disappeared

        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=None)
            )
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                json=_device_dict(_NEW_VERSION, qr_id=_QR_ID)
            )
            with pytest.raises(QRRetireRolledBackError):
                await service.retire(_QR_ID, _VERSION, _user())
    assert audit_repo.entries[0].after_json["compensation_outcome"] == "restored"


async def test_retire_raises_runtime_error_when_bound_qr_missing_device_id(
    clean_env: None, netbox_env: None
) -> None:
    """Defensive: BOUND with bound_to_device_id=None is forbidden by both the
    qr_state_consistency CHECK and ``QR.__post_init__``. This test bypasses
    the dataclass invariant to confirm the runtime guard still fires under
    ``python -O`` (where ``assert`` would be stripped)."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, _a, write_service = _build_service(client)
        broken = _bound_qr()
        # Forced bad state — bypasses frozen dataclass + __post_init__.
        object.__setattr__(broken, "bound_to_device_id", None)
        qr_repo.by_id[_QR_ID] = broken

        with pytest.raises(RuntimeError, match="bound_to_device_id"):
            await service.retire(_QR_ID, _VERSION, _user())
    assert write_service.calls == []
