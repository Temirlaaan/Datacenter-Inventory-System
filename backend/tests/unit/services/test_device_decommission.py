"""Unit tests for app.services.device_decommission.DeviceDecommissionService.

Sprint 5 Task 4. Strategy: fake ``QRLifecycleService`` directly (its bind/retire
mechanics are covered in test_lifecycle.py), fake ``QRCodeRepository`` for the
bound-QR lookup, and use respx only for the Branch 3 best-effort inconsistency
journal POST. Audit rows written inside ``patch_with_attribution`` are out of
scope here — covered by test_netbox_write.py.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
import respx
import structlog

from app.auth.dependencies import AuthUser
from app.db.repositories.qr_code import QRCodeRepository
from app.domain.qr import QR, QRStatus
from app.netbox.client import NetBoxClient
from app.netbox.errors import NetBoxClientError, NetBoxNotFound
from app.services.device_decommission import (
    DeviceDecommissionInconsistencyError,
    DeviceDecommissionRolledBackError,
    DeviceDecommissionService,
)
from app.services.netbox_write import NetBoxWriteService, WriteConflictError
from app.services.qr.lifecycle import (
    QRLifecycleService,
    QRRetireInconsistencyError,
)

NETBOX_URL = "https://netbox.example.com"
_DEVICE_ID = 99
_DEVICE_PATH = f"/api/dcim/devices/{_DEVICE_ID}/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_QR_ID = "DCQR-FREEKLM2"
_VERSION = "2026-05-21T08:00:00.000000Z"
_POST_RETIRE_VERSION = "2026-05-21T09:00:00.000000Z"
_FINAL_VERSION = "2026-05-21T10:00:00.000000Z"
_USER_SUB = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def netbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


@pytest.fixture
def clean_env() -> Generator[None, None, None]:
    # Mirror the lifecycle test convention so cached Settings doesn't leak in.
    from app.config import get_settings
    from app.netbox.client import get_netbox_client

    get_settings.cache_clear()
    get_netbox_client.cache_clear()
    yield
    get_settings.cache_clear()
    get_netbox_client.cache_clear()


# ---------- fakes ----------


class _FakeSession:
    def __init__(self, *, in_transaction: bool = False) -> None:
        self._in_tx = in_transaction

    def in_transaction(self) -> bool:
        return self._in_tx

    def begin(self) -> _FakeTx:
        return _FakeTx()


class _FakeTx:
    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        return False


class _FakeQRCodeRepo:
    def __init__(self) -> None:
        self.bound_by_device: dict[int, QR] = {}
        self.last_lookup_device_id: int | None = None

    async def find_by_bound_device_id(self, device_id: int) -> QR | None:
        self.last_lookup_device_id = device_id
        return self.bound_by_device.get(device_id)


class _FakeWriteService:
    """Fake ``NetBoxWriteService.patch_with_attribution``."""

    def __init__(self) -> None:
        self.return_value: dict[str, Any] = _device_dict(_FINAL_VERSION, status="decommissioning")
        self.raises: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def patch_with_attribution(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.return_value


class _FakeLifecycleService:
    """Fake ``QRLifecycleService.retire`` and ``.bind``."""

    def __init__(self) -> None:
        self.retire_calls: list[dict[str, Any]] = []
        self.bind_calls: list[dict[str, Any]] = []
        self.retire_raises: Exception | None = None
        self.bind_raises: Exception | None = None
        self.retire_returns_device: dict[str, Any] | None = _device_dict(
            _POST_RETIRE_VERSION, qr_id=None
        )

    async def retire(
        self,
        *,
        qr_id: str,
        expected_version: str | None,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any] | None]:
        self.retire_calls.append(
            {"qr_id": qr_id, "expected_version": expected_version, "user": user}
        )
        if self.retire_raises is not None:
            raise self.retire_raises
        return _retired_qr(qr_id), self.retire_returns_device

    async def bind(
        self,
        *,
        qr_id: str,
        device_id: int,
        expected_version: str,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any]]:
        self.bind_calls.append(
            {
                "qr_id": qr_id,
                "device_id": device_id,
                "expected_version": expected_version,
                "user": user,
            }
        )
        if self.bind_raises is not None:
            raise self.bind_raises
        return _bound_qr(qr_id, device_id), _device_dict(_FINAL_VERSION, qr_id=qr_id)


# ---------- helpers ----------


def _user() -> AuthUser:
    return AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-admin",),
        session_id=None,
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
        retired_reason=None,
    )


def _device_dict(
    version: str, *, status: str = "active", qr_id: str | None = None
) -> dict[str, Any]:
    return {
        "id": _DEVICE_ID,
        "name": "dev-99",
        "last_updated": version,
        "status": {"value": status, "label": status.title()},
        "site": {"id": 1, "name": "site-1", "slug": "site-1"},
        "rack": {"id": 2, "name": "rack-2"},
        "role": {"id": 3, "name": "role-3", "slug": "role-3"},
        "device_type": {"id": 4, "model": "model-4", "display": "Model 4"},
        "position": None,
        "serial": "SN-99",
        "comments": "",
        "asset_tag": None,
        "custom_fields": {"qr_id": qr_id},
    }


def _build_service(
    netbox: NetBoxClient,
    *,
    session: _FakeSession | None = None,
    qr_repo: _FakeQRCodeRepo | None = None,
    write_service: _FakeWriteService | None = None,
    lifecycle: _FakeLifecycleService | None = None,
) -> tuple[
    DeviceDecommissionService,
    _FakeSession,
    _FakeQRCodeRepo,
    _FakeWriteService,
    _FakeLifecycleService,
]:
    session = session or _FakeSession()
    qr_repo = qr_repo or _FakeQRCodeRepo()
    write_service = write_service or _FakeWriteService()
    lifecycle = lifecycle or _FakeLifecycleService()
    service = DeviceDecommissionService(
        netbox_client=netbox,
        session=cast(Any, session),
        qr_code_repo=cast(QRCodeRepository, qr_repo),
        write_service=cast(NetBoxWriteService, write_service),
        lifecycle_service=cast(QRLifecycleService, lifecycle),
    )
    return service, session, qr_repo, write_service, lifecycle


# ========== guard + no-bound-QR happy path ==========


async def test_decommission_raises_runtime_error_when_called_in_active_transaction(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, _q, write_service, lifecycle = _build_service(
            client, session=_FakeSession(in_transaction=True)
        )
        with pytest.raises(RuntimeError, match="active transaction"):
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )
    # Nothing ran past the guard.
    assert write_service.calls == []
    assert lifecycle.retire_calls == []


async def test_decommission_with_no_bound_qr_only_patches_device(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)

        result = await service.decommission(
            device_id=_DEVICE_ID,
            expected_version=_VERSION,
            reason="end of life",
            user=_user(),
        )

    # Looked up bound QR (got None).
    assert qr_repo.last_lookup_device_id == _DEVICE_ID
    # No retire, no bind.
    assert lifecycle.retire_calls == []
    assert lifecycle.bind_calls == []
    # Exactly one PATCH: the device status change with the caller's version.
    assert len(write_service.calls) == 1
    call = write_service.calls[0]
    assert call["netbox_path"] == _DEVICE_PATH
    assert call["operation"] == "device.decommission"
    assert call["entity_type"] == "device"
    assert call["entity_id"] == str(_DEVICE_ID)
    assert call["expected_version"] == _VERSION
    assert call["changes"] == {"status": "decommissioning"}
    # Returns a DeviceResponse carrying the post-PATCH version.
    assert result.version == _FINAL_VERSION


# ========== bound-QR happy path ==========


async def test_decommission_with_bound_qr_retires_first_then_patches_device(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()

        result = await service.decommission(
            device_id=_DEVICE_ID,
            expected_version=_VERSION,
            reason=None,
            user=_user(),
        )

    # Retire happened with the caller-provided version.
    assert len(lifecycle.retire_calls) == 1
    assert lifecycle.retire_calls[0]["qr_id"] == _QR_ID
    assert lifecycle.retire_calls[0]["expected_version"] == _VERSION
    # Device PATCH uses the post-retire version (the retire PATCH bumped
    # last_updated; reusing the caller version would 409).
    assert len(write_service.calls) == 1
    assert write_service.calls[0]["expected_version"] == _POST_RETIRE_VERSION
    # No re-bind on the happy path.
    assert lifecycle.bind_calls == []
    assert result.version == _FINAL_VERSION


# ========== retire failure propagation ==========


async def test_decommission_propagates_write_conflict_from_retire(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()
        lifecycle.retire_raises = WriteConflictError(
            current_object={"id": _DEVICE_ID, "last_updated": _POST_RETIRE_VERSION},
            current_version=_POST_RETIRE_VERSION,
        )

        with pytest.raises(WriteConflictError):
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )
    # Retire failed → device PATCH was never attempted.
    assert write_service.calls == []
    # No compensation re-bind either.
    assert lifecycle.bind_calls == []


async def test_decommission_propagates_retire_failure_without_patching_device(
    clean_env: None, netbox_env: None
) -> None:
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()
        lifecycle.retire_raises = NetBoxNotFound("device gone")

        with pytest.raises(NetBoxNotFound):
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )
    assert write_service.calls == []


async def test_decommission_aborts_when_retire_raises_inconsistency_error(
    clean_env: None, netbox_env: None
) -> None:
    """Correction 4: QRRetireInconsistencyError → log critical + re-raise; no device PATCH."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()
        lifecycle.retire_raises = QRRetireInconsistencyError(_QR_ID, _DEVICE_ID)

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(QRRetireInconsistencyError),
        ):
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )

    # Device PATCH must NOT be attempted (the QR is in an undefined state).
    assert write_service.calls == []
    # No re-bind compensation either — the retire itself blew up.
    assert lifecycle.bind_calls == []
    events = [log.get("event") for log in logs]
    assert "device_decommission_aborted_qr_inconsistent" in events
    # Confirm severity: this is a critical-level event.
    critical_log = next(
        log for log in logs if log.get("event") == "device_decommission_aborted_qr_inconsistent"
    )
    assert critical_log["log_level"] == "critical"


# ========== device PATCH failure WITHOUT bound QR ==========


async def test_decommission_device_patch_failure_with_no_bound_qr_propagates_without_compensation(
    clean_env: None, netbox_env: None
) -> None:
    """No bound QR → nothing to compensate; exception propagates."""
    async with NetBoxClient.from_settings() as client:
        service, _s, _q, write_service, lifecycle = _build_service(client)
        # No bound QR set up.
        write_service.raises = NetBoxClientError("netbox down")

        with pytest.raises(NetBoxClientError):
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )
    # Compensation never attempted.
    assert lifecycle.retire_calls == []
    assert lifecycle.bind_calls == []


async def test_decommission_propagates_write_conflict_from_status_patch(
    clean_env: None, netbox_env: None
) -> None:
    """No bound QR + device PATCH 409 → WriteConflictError propagates verbatim."""
    async with NetBoxClient.from_settings() as client:
        service, _s, _q, write_service, lifecycle = _build_service(client)
        write_service.raises = WriteConflictError(
            current_object={"id": _DEVICE_ID, "last_updated": _FINAL_VERSION},
            current_version=_FINAL_VERSION,
        )

        with pytest.raises(WriteConflictError):
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )
    assert lifecycle.bind_calls == []


# ========== Q3 Branch 2: re-bind compensation succeeds ==========


async def test_decommission_write_conflict_on_status_patch_after_retire_compensates_via_rebind(
    clean_env: None, netbox_env: None
) -> None:
    """WriteConflictError on the status PATCH after a successful retire MUST
    trigger compensation, NOT propagate as 409. Pins the service-layer decision:
    once the QR is retired, system consistency is the priority — even a stale
    device-version conflict gets re-bound. The endpoint's 409 DEVICE_CONFLICT
    handler only fires for retire-step conflicts (no compensation needed) or
    no-bound-QR conflicts (nothing to undo)."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()
        write_service.raises = WriteConflictError(
            current_object={"id": _DEVICE_ID, "last_updated": _FINAL_VERSION},
            current_version=_FINAL_VERSION,
        )

        with pytest.raises(DeviceDecommissionRolledBackError) as exc_info:
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )

    # Re-bind fired with the post-retire OCC token (NOT propagated as 409).
    assert len(lifecycle.bind_calls) == 1
    assert lifecycle.bind_calls[0]["expected_version"] == _POST_RETIRE_VERSION
    assert exc_info.value.device_id == _DEVICE_ID
    assert exc_info.value.qr_id == _QR_ID


async def test_decommission_device_patch_fails_after_qr_retire_compensates_via_rebind(
    clean_env: None, netbox_env: None
) -> None:
    """QR retire ok → device PATCH raises → re-bind ok → DeviceDecommissionRolledBackError."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()
        write_service.raises = NetBoxClientError("transient netbox 5xx on status patch")

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(DeviceDecommissionRolledBackError) as exc_info,
        ):
            await service.decommission(
                device_id=_DEVICE_ID,
                expected_version=_VERSION,
                reason=None,
                user=_user(),
            )

    # Re-bind called with the post-retire version (deterministic OCC token).
    assert len(lifecycle.bind_calls) == 1
    rebind = lifecycle.bind_calls[0]
    assert rebind["qr_id"] == _QR_ID
    assert rebind["device_id"] == _DEVICE_ID
    assert rebind["expected_version"] == _POST_RETIRE_VERSION
    # Terminal exception carries both ids.
    assert exc_info.value.device_id == _DEVICE_ID
    assert exc_info.value.qr_id == _QR_ID
    events = [log.get("event") for log in logs]
    assert "device_decommission_db_failed_qr_recompensated" in events


# ========== Q3 Branch 3: re-bind compensation fails ==========


async def test_decommission_compensation_rebind_also_fails_raises_inconsistency(
    clean_env: None, netbox_env: None
) -> None:
    """QR retire ok → device PATCH raises → re-bind ALSO raises → inconsistency + danger journal."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()
        write_service.raises = NetBoxClientError("status patch failed")
        lifecycle.bind_raises = NetBoxClientError("re-bind also failed")

        with respx.mock(assert_all_called=True) as router:
            journal_route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 1}
            )
            with (
                structlog.testing.capture_logs() as logs,
                pytest.raises(DeviceDecommissionInconsistencyError) as exc_info,
            ):
                await service.decommission(
                    device_id=_DEVICE_ID,
                    expected_version=_VERSION,
                    reason=None,
                    user=_user(),
                )

    # Danger journal landed on the device.
    import json as _json

    body = _json.loads(journal_route.calls.last.request.content)
    assert body["assigned_object_type"] == "dcim.device"
    assert body["assigned_object_id"] == _DEVICE_ID
    assert body["kind"] == "danger"
    assert _QR_ID in body["comments"]
    assert "decommission" in body["comments"]
    # Critical log + terminal exception.
    events = [log.get("event") for log in logs]
    assert "device_decommission_inconsistency_unrecoverable" in events
    assert exc_info.value.device_id == _DEVICE_ID
    assert exc_info.value.qr_id == _QR_ID


async def test_decommission_branch_3_swallows_journal_write_failure(
    clean_env: None, netbox_env: None
) -> None:
    """Inconsistency journal POST itself failing must not mask the inconsistency error."""
    async with NetBoxClient.from_settings() as client:
        service, _s, qr_repo, write_service, lifecycle = _build_service(client)
        qr_repo.bound_by_device[_DEVICE_ID] = _bound_qr()
        write_service.raises = NetBoxClientError("status patch failed")
        lifecycle.bind_raises = NetBoxClientError("re-bind failed")

        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=500)
            with (
                structlog.testing.capture_logs() as logs,
                pytest.raises(DeviceDecommissionInconsistencyError),
            ):
                await service.decommission(
                    device_id=_DEVICE_ID,
                    expected_version=_VERSION,
                    reason=None,
                    user=_user(),
                )

    events = [log.get("event") for log in logs]
    # The inconsistency log still fires, plus a journal-failed warning.
    assert "device_decommission_inconsistency_unrecoverable" in events
    assert "device_decommission_inconsistency_journal_failed" in events
