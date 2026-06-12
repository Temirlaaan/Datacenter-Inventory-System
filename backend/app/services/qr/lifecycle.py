"""QR lifecycle service — bind (Sprint 4 Task 1) and retire (Task 2).

Architecture §4: the qr_codes row transition must be coordinated with the NetBox
device PATCH that attaches/detaches the QR token. SQLAlchemy 2.0 async sessions
don't nest ``session.begin()`` by default and
``NetBoxWriteService.patch_with_attribution`` already owns its audit-row tx
(``app/services/netbox_write.py``). So this service **sequences** the work
rather than nesting:

1. Cheap pre-validation (lookup + state check, no lock).
2. NetBox PATCH via ``patch_with_attribution``.
3. Separate DB transaction: ``SELECT ... FOR UPDATE`` on qr_codes, re-check
   state under the lock, UPDATE qr_codes, commit.
4. If anything in step 3 fails AFTER step 2 succeeded: compensate by clearing
   ``custom_fields.qr_id`` on the device — **conditionally**, only when NetBox
   still shows our token. Two branches:
   - Branch 2 — compensation ok → ``QRBindRolledBackError``.
   - Branch 3 — compensation fails → ``QRBindInconsistencyError`` +
     best-effort NetBox journal entry naming the inconsistency.

Both Branch 2 and Branch 3 also write a best-effort ``audit_log`` row with
``after_json.failure_stage`` ∈ {``db_commit``, ``compensation``} so later
queries can count compensation events without expanding the AuditResult enum.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.qr import QR, QRStatus
from app.netbox.client import NetBoxClient
from app.observability.request_id import current_request_id
from app.services.netbox_write import NetBoxWriteService

logger = structlog.get_logger()

_JOURNAL_PATH = "/api/extras/journal-entries/"


class QRNotFoundError(Exception):
    """The QR id is not registered in qr_codes."""

    def __init__(self, qr_id: str) -> None:
        super().__init__(f"QR not found: {qr_id}")
        self.qr_id = qr_id


class QRStateConflictError(Exception):
    """The QR is in a state that does not permit the requested transition."""

    def __init__(self, current_status: QRStatus) -> None:
        super().__init__(f"QR in conflicting state: {current_status.value}")
        self.current_status = current_status


class QRAlreadyBoundError(Exception):
    """Another concurrent bind won the qr_one_per_device race for this device."""

    def __init__(self, qr_id: str, device_id: int) -> None:
        super().__init__(f"QR {qr_id} race lost to concurrent bind on device {device_id}")
        self.qr_id = qr_id
        self.device_id = device_id


class QRBindRolledBackError(Exception):
    """DB commit failed after NetBox PATCH; compensation restored consistency."""

    def __init__(self, qr_id: str, device_id: int) -> None:
        super().__init__(f"Bind of QR {qr_id} to device {device_id} rolled back")
        self.qr_id = qr_id
        self.device_id = device_id


class QRBindInconsistencyError(Exception):
    """DB commit failed AND compensation failed; manual cleanup required."""

    def __init__(self, qr_id: str, device_id: int) -> None:
        super().__init__(
            f"Bind of QR {qr_id} to device {device_id} left an inconsistency "
            "— manual cleanup required"
        )
        self.qr_id = qr_id
        self.device_id = device_id


class MissingVersionError(Exception):
    """BOUND→RETIRED requires the device's expected ``last_updated`` version."""

    def __init__(self, qr_id: str) -> None:
        super().__init__(f"BOUND QR {qr_id} retire requires device version")
        self.qr_id = qr_id


class QRRetireRolledBackError(Exception):
    """DB commit failed after NetBox PATCH; compensation restored ``qr_id``."""

    def __init__(self, qr_id: str, device_id: int) -> None:
        super().__init__(f"Retire of QR {qr_id} from device {device_id} rolled back")
        self.qr_id = qr_id
        self.device_id = device_id


class QRRetireInconsistencyError(Exception):
    """DB commit failed AND compensation failed; manual cleanup required."""

    def __init__(self, qr_id: str, device_id: int) -> None:
        super().__init__(
            f"Retire of QR {qr_id} from device {device_id} left an inconsistency "
            "— manual cleanup required"
        )
        self.qr_id = qr_id
        self.device_id = device_id


class _PostNetBoxStateRace(Exception):
    """Internal sentinel: FOR-UPDATE-time state mismatch after NetBox PATCH succeeded.

    Not exported. Translated into ``QRBindRolledBackError`` /
    ``QRRetireRolledBackError`` by the compensation handler.
    """


class QRLifecycleService:
    """Coordinates qr_codes state transitions with the NetBox attribution write."""

    def __init__(
        self,
        netbox_client: NetBoxClient,
        session: AsyncSession,
        qr_code_repo: QRCodeRepository,
        audit_log_repo: AuditLogRepository,
        write_service: NetBoxWriteService,
    ) -> None:
        self._netbox = netbox_client
        self._session = session
        self._qr_code_repo = qr_code_repo
        self._audit_log_repo = audit_log_repo
        self._write_service = write_service

    # ---------- bind ----------

    async def bind(
        self,
        qr_id: str,
        device_id: int,
        expected_version: str,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any]]:
        """Transition a FREE QR to BOUND on the given device, with NetBox attribution.

        Returns the new QR (BOUND) and the updated device dict. Raises one of the
        QR* exceptions on a defined failure mode; lets ``WriteConflictError``,
        ``NetBoxNotFound``, ``NetBoxClientError`` propagate from
        ``patch_with_attribution`` so the endpoint maps them to 409/404/502.
        """
        # Q2: defensive runtime guard. Asserts get stripped by `python -O`
        # (Sprint 1 M3). Calling bind inside an active transaction would
        # conflict with patch_with_attribution's own session.begin().
        # This check runs BEFORE any DB op so SQLAlchemy 2.0's autobegin
        # behaviour doesn't trip the guard for legitimate callers.
        if self._session.in_transaction():
            raise RuntimeError(
                "QRLifecycleService.bind called inside an active transaction "
                "— would conflict with patch_with_attribution's audit tx"
            )

        # Step A — cheap pre-validation. Wrap the read in an explicit
        # `session.begin()` so the autobegun tx is closed before
        # patch_with_attribution opens its own — SQLAlchemy 2.0 raises if a tx
        # is pending when `session.begin()` is called.
        async with self._session.begin():
            qr = await self._qr_code_repo.get_by_id(qr_id)
        if qr is None:
            raise QRNotFoundError(qr_id)
        if qr.status is not QRStatus.FREE:
            raise QRStateConflictError(qr.status)

        # Step B — NetBox PATCH via patch_with_attribution.
        # WriteConflictError / NetBoxNotFound / NetBoxClientError propagate;
        # no PATCH happened (or a FAILURE audit row already landed) — endpoint
        # maps them. From here on, any failure demands compensation.
        updated_device = await self._write_service.patch_with_attribution(
            netbox_path=f"/api/dcim/devices/{device_id}/",
            netbox_object_type="dcim.device",
            netbox_object_id=device_id,
            entity_type="qr",
            entity_id=qr_id,
            operation="qr.bind",
            expected_version=expected_version,
            changes={"custom_fields": {"qr_id": qr_id}},
            user=user,
        )

        # Step C — DB transaction: FOR UPDATE + UPDATE + commit, with compensation.
        bound = await self._commit_qr_transition_or_compensate(
            qr_id=qr_id,
            device_id=device_id,
            expected_version=expected_version,
            user=user,
        )
        return bound, updated_device

    # ---------- retire ----------

    async def retire(
        self,
        qr_id: str,
        expected_version: str | None,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any] | None]:
        """Transition a FREE or BOUND QR to RETIRED.

        Returns ``(retired_qr, updated_device_dict | None)``. FREE → RETIRED is
        a pure app-DB transaction (no NetBox call, atomic SUCCESS audit row);
        the second element is ``None``. BOUND → RETIRED clears
        ``custom_fields.qr_id`` on the bound device via
        ``patch_with_attribution`` and returns its response dict as the second
        element so callers (Sprint 5 Task 4 decommission) can read
        ``last_updated`` for follow-on OCC. Uses the same three-branch
        compensation as bind, with the compensation **restoring** ``qr_id``
        instead of clearing it.

        Raises ``MissingVersionError`` if BOUND and ``expected_version`` is
        ``None``; lets ``WriteConflictError`` / ``NetBoxNotFound`` /
        ``NetBoxClientError`` propagate from ``patch_with_attribution``.
        """
        # Q2: defensive runtime guard — see bind() for rationale.
        if self._session.in_transaction():
            raise RuntimeError(
                "QRLifecycleService.retire called inside an active transaction "
                "— would conflict with patch_with_attribution's audit tx"
            )

        # Step A — cheap pre-validation in its own explicit tx (closes the
        # autobegin cleanly; same pattern as bind()).
        async with self._session.begin():
            qr = await self._qr_code_repo.get_by_id(qr_id)
        if qr is None:
            raise QRNotFoundError(qr_id)
        if qr.status is QRStatus.RETIRED:
            raise QRStateConflictError(qr.status)

        if qr.status is QRStatus.FREE:
            retired = await self._retire_free(qr_id=qr_id, user=user)
            return retired, None

        # qr.status is QRStatus.BOUND
        if expected_version is None:
            raise MissingVersionError(qr_id)
        if qr.bound_to_device_id is None:
            # Correction 1: use RuntimeError, not assert — the qr_state_consistency
            # CHECK constraint guarantees a BOUND row has a non-null
            # bound_to_device_id, so this branch is unreachable under correct DB
            # state. RuntimeError (over assert) survives ``python -O``.
            raise RuntimeError(
                f"BOUND QR {qr.id} has no bound_to_device_id; "
                "domain invariant violated (qr_state_consistency CHECK "
                "should prevent this)"
            )
        return await self._retire_bound(
            qr=qr,
            device_id=qr.bound_to_device_id,
            expected_version=expected_version,
            user=user,
        )

    # ---------- internal: DB tx + compensation orchestration ----------

    async def _commit_qr_transition_or_compensate(
        self,
        *,
        qr_id: str,
        device_id: int,
        expected_version: str,
        user: AuthUser,
    ) -> QR:
        """Open the qr_codes write tx; compensate on any post-NetBox-PATCH failure."""
        bound: QR | None = None
        try:
            async with self._session.begin():
                locked = await self._qr_code_repo.get_by_id_for_update(qr_id)
                if locked is None:
                    raise _PostNetBoxStateRace("qr_disappeared")
                if locked.status is not QRStatus.FREE:
                    raise _PostNetBoxStateRace(f"qr_not_free:{locked.status.value}")
                bound = locked.bind(
                    device_id=device_id,
                    by_email=user.email or "",
                    at=datetime.now(UTC),
                )
                await self._qr_code_repo.update(bound)
            # __aexit__ ran commit; if it raised, we fall to the except below.
            assert bound is not None  # invariant: set inside the with block
            return bound
        except IntegrityError as race:
            # qr_one_per_device — concurrent bind won. NetBox already shows our
            # qr_id; compensation must clear it (conditionally, see Step E).
            await self._run_compensation(
                qr_id=qr_id,
                device_id=device_id,
                expected_version=expected_version,
                user=user,
                operation="qr.bind",
                compensate_fn=self._compensate_clear_qr,
                original_err=race,
                terminal_exc=QRAlreadyBoundError,
                inconsistency_exc=QRBindInconsistencyError,
            )
        except _PostNetBoxStateRace as race:
            await self._run_compensation(
                qr_id=qr_id,
                device_id=device_id,
                expected_version=expected_version,
                user=user,
                operation="qr.bind",
                compensate_fn=self._compensate_clear_qr,
                original_err=race,
                terminal_exc=QRBindRolledBackError,
                inconsistency_exc=QRBindInconsistencyError,
            )
        except Exception as err:
            # session.commit() failure, IllegalQRTransition, etc.
            await self._run_compensation(
                qr_id=qr_id,
                device_id=device_id,
                expected_version=expected_version,
                user=user,
                operation="qr.bind",
                compensate_fn=self._compensate_clear_qr,
                original_err=err,
                terminal_exc=QRBindRolledBackError,
                inconsistency_exc=QRBindInconsistencyError,
            )

    async def _run_compensation(
        self,
        *,
        qr_id: str,
        device_id: int,
        expected_version: str,
        user: AuthUser,
        operation: str,
        compensate_fn: Callable[[int, str], Awaitable[str]],
        original_err: BaseException,
        terminal_exc: type[Exception],
        inconsistency_exc: type[Exception],
    ) -> NoReturn:
        """Three-branch compensation. Always raises.

        ``operation`` ("qr.bind" or "qr.retire") flows into the audit row and
        derives the structured-log event names (e.g.
        ``qr_bind_db_failed_netbox_compensated`` for bind,
        ``qr_retire_db_failed_netbox_compensated`` for retire).
        """
        op_prefix = operation.replace(".", "_")
        request_id = current_request_id()
        try:
            outcome = await compensate_fn(device_id, qr_id)
        except Exception as comp_err:
            # ===== Branch 3: COMPENSATION FAILED =====
            logger.critical(
                f"{op_prefix}_inconsistency_unrecoverable",
                qr_id=qr_id,
                device_id=device_id,
                request_id=request_id,
                original_error=repr(original_err),
                compensation_error=repr(comp_err),
            )
            await self._best_effort_inconsistency_journal(
                operation=operation,
                device_id=device_id,
                qr_id=qr_id,
                original_err=original_err,
                comp_err=comp_err,
            )
            await self._best_effort_compensation_audit(
                operation=operation,
                qr_id=qr_id,
                device_id=device_id,
                expected_version=expected_version,
                user=user,
                failure_stage="compensation",
                original_error=original_err,
                compensation_error=comp_err,
                compensation_outcome="failed",
            )
            raise inconsistency_exc(qr_id, device_id) from comp_err

        # ===== Branch 2: COMPENSATION OK =====
        logger.error(
            f"{op_prefix}_db_failed_netbox_compensated",
            qr_id=qr_id,
            device_id=device_id,
            request_id=request_id,
            original_error=repr(original_err),
            compensation_outcome=outcome,
        )
        await self._best_effort_compensation_audit(
            operation=operation,
            qr_id=qr_id,
            device_id=device_id,
            expected_version=expected_version,
            user=user,
            failure_stage="db_commit",
            original_error=original_err,
            compensation_error=None,
            compensation_outcome=outcome,
        )
        raise terminal_exc(qr_id, device_id) from original_err

    # ---------- internal: retire orchestration ----------

    async def _retire_free(self, *, qr_id: str, user: AuthUser) -> QR:
        """FREE → RETIRED — pure app-DB transaction, no NetBox call.

        The qr_codes UPDATE and the SUCCESS audit row commit atomically
        (decision B's best-effort attribution applies only after a durable
        NetBox change; there is none here, so atomic is natural). A FOR-UPDATE
        state mismatch raises ``QRStateConflictError`` directly — no NetBox
        compensation is possible or needed for this branch.
        """
        request_id = UUID(current_request_id())
        timestamp = datetime.now(UTC)
        retired: QR | None = None
        async with self._session.begin():
            locked = await self._qr_code_repo.get_by_id_for_update(qr_id)
            # ``locked is None`` should be unreachable per Sprint 2's invariant
            # "QR IDs are never reused / never deleted" — but raise
            # ``QRNotFoundError`` rather than a misleading
            # ``QRStateConflictError(RETIRED)``: if the invariant ever breaks,
            # the truthful error type lets the caller respond correctly.
            if locked is None:
                raise QRNotFoundError(qr_id)
            if locked.status is not QRStatus.FREE:
                raise QRStateConflictError(locked.status)
            retired = locked.retire(reason=None, at=timestamp)
            await self._qr_code_repo.update(retired)
            await self._audit_log_repo.insert(
                AuditLogEntry(
                    request_id=request_id,
                    timestamp=timestamp,
                    user_email=user.email or "",
                    user_keycloak_id=UUID(user.sub),
                    # Sprint 6 decision D: source from active shift, not JWT sid.
                    session_id=user.shift_session_id,
                    operation="qr.retire",
                    entity_type="qr",
                    entity_id=qr_id,
                    before_json={"status": QRStatus.FREE.value},
                    after_json={"status": QRStatus.RETIRED.value},
                    result=AuditResult.SUCCESS,
                )
            )
        # Invariant: ``retired`` is set inside the with block on the only
        # success path; mypy needs the assertion to narrow ``QR | None``.
        assert retired is not None
        return retired

    async def restore(self, qr_id: str, user: AuthUser) -> QR:
        """Transition RETIRED → FREE. Pure app-DB write — no NetBox call,
        no compensation. Use case: admin retired a working sticker by
        mistake (wrong row, fat-fingered the bulk-retire button) and
        wants it back in the FREE pool.

        Symmetric to ``_retire_free``: lock, re-check state, transition,
        atomic SUCCESS audit row. The new row carries the prior status
        in ``before_json`` so an auditor can pair restore with the
        original retire by request_id / entity_id.

        Historical ``bound_*`` fields on the QR are NOT auto-restored —
        an accidentally-retired-while-BOUND QR comes back as FREE, and
        admin must explicitly ``bind`` it again. Safer default: don't
        recreate a binding that might collide with another QR that
        captured the device in the meantime.

        Raises ``QRNotFoundError`` (unknown id) or ``QRStateConflictError``
        (QR is currently FREE or BOUND — restoring a non-retired QR is a
        UX error, surface it to the caller).
        """
        if self._session.in_transaction():
            raise RuntimeError(
                "QRLifecycleService.restore called inside an active transaction"
            )
        request_id = UUID(current_request_id())
        timestamp = datetime.now(UTC)
        restored: QR | None = None
        async with self._session.begin():
            locked = await self._qr_code_repo.get_by_id_for_update(qr_id)
            if locked is None:
                raise QRNotFoundError(qr_id)
            if locked.status is not QRStatus.RETIRED:
                raise QRStateConflictError(locked.status)
            restored = locked.restore()
            await self._qr_code_repo.update(restored)
            await self._audit_log_repo.insert(
                AuditLogEntry(
                    request_id=request_id,
                    timestamp=timestamp,
                    user_email=user.email or "",
                    user_keycloak_id=UUID(user.sub),
                    session_id=user.shift_session_id,
                    operation="qr.restore",
                    entity_type="qr",
                    entity_id=qr_id,
                    before_json={
                        "status": QRStatus.RETIRED.value,
                        "retired_at": (
                            locked.retired_at.isoformat()
                            if locked.retired_at
                            else None
                        ),
                        "retired_reason": locked.retired_reason,
                        # Capture the historical binding so an auditor can
                        # tell what the QR was attached to before the retire
                        # they're now undoing.
                        "prior_bound_to_device_id": locked.bound_to_device_id,
                    },
                    after_json={"status": QRStatus.FREE.value},
                    result=AuditResult.SUCCESS,
                )
            )
        assert restored is not None
        return restored

    async def _retire_bound(
        self,
        *,
        qr: QR,
        device_id: int,
        expected_version: str,
        user: AuthUser,
    ) -> tuple[QR, dict[str, Any]]:
        """BOUND → RETIRED — clear ``custom_fields.qr_id`` then DB transition.

        Returns ``(retired_qr, updated_device_dict)`` so callers can read the
        post-retire ``last_updated`` (Sprint 5 Task 4 decommission OCC chain).
        """
        updated_device = await self._write_service.patch_with_attribution(
            netbox_path=f"/api/dcim/devices/{device_id}/",
            netbox_object_type="dcim.device",
            netbox_object_id=device_id,
            entity_type="qr",
            entity_id=qr.id,
            operation="qr.retire",
            expected_version=expected_version,
            changes={"custom_fields": {"qr_id": None}},
            user=user,
        )
        # NetBox now shows qr_id=None. From here, any failure demands
        # compensation (restore qr_id=qr.id on the device).
        retired = await self._commit_retire_or_compensate(
            qr_id=qr.id,
            device_id=device_id,
            expected_version=expected_version,
            user=user,
        )
        return retired, updated_device

    async def _commit_retire_or_compensate(
        self,
        *,
        qr_id: str,
        device_id: int,
        expected_version: str,
        user: AuthUser,
    ) -> QR:
        """Open the qr_codes write tx; compensate on any post-NetBox-PATCH failure."""
        try:
            async with self._session.begin():
                locked = await self._qr_code_repo.get_by_id_for_update(qr_id)
                if locked is None:
                    raise _PostNetBoxStateRace("qr_disappeared")
                if locked.status is not QRStatus.BOUND:
                    raise _PostNetBoxStateRace(f"qr_not_bound:{locked.status.value}")
                retired = locked.retire(reason=None, at=datetime.now(UTC))
                await self._qr_code_repo.update(retired)
            return retired
        except _PostNetBoxStateRace as race:
            await self._run_compensation(
                qr_id=qr_id,
                device_id=device_id,
                expected_version=expected_version,
                user=user,
                operation="qr.retire",
                compensate_fn=self._compensate_restore_qr,
                original_err=race,
                terminal_exc=QRRetireRolledBackError,
                inconsistency_exc=QRRetireInconsistencyError,
            )
        except Exception as err:
            await self._run_compensation(
                qr_id=qr_id,
                device_id=device_id,
                expected_version=expected_version,
                user=user,
                operation="qr.retire",
                compensate_fn=self._compensate_restore_qr,
                original_err=err,
                terminal_exc=QRRetireRolledBackError,
                inconsistency_exc=QRRetireInconsistencyError,
            )

    # ---------- internal: NetBox compensation ----------

    async def _compensate_clear_qr(
        self, device_id: int, qr_id: str
    ) -> Literal["cleared", "noop_different_qr"]:
        """Conditional, idempotent clear of ``custom_fields.qr_id``.

        Correction 3: only PATCH if NetBox currently shows our token; otherwise
        no-op (a concurrent winner's binding must not be clobbered). Direct
        client calls — not via ``patch_with_attribution``, which would write a
        misleading second journal entry attributing the compensation as a
        regular bind.
        """
        device = (await self._netbox.get(f"/api/dcim/devices/{device_id}/")).json()
        observed = (device.get("custom_fields") or {}).get("qr_id")
        if observed == qr_id:
            await self._netbox.patch(
                f"/api/dcim/devices/{device_id}/",
                json={"custom_fields": {"qr_id": None}},
            )
            return "cleared"
        logger.info(
            "qr_bind_compensation_noop",
            qr_id=qr_id,
            device_id=device_id,
            request_id=current_request_id(),
            observed_qr_id=observed,
        )
        return "noop_different_qr"

    async def _compensate_restore_qr(
        self, device_id: int, qr_id: str
    ) -> Literal["restored", "noop_already_restored"]:
        """Conditional, idempotent restore of ``custom_fields.qr_id``.

        Symmetric to ``_compensate_clear_qr``: only PATCH when NetBox currently
        shows ``qr_id=None`` (i.e. our retire clear took effect). If NetBox
        already shows some token, do not clobber — either someone re-bound in
        the interim or our clear never landed; either way, restoring our token
        would overwrite the wrong value. Direct client calls (no
        ``patch_with_attribution`` — see ``_compensate_clear_qr`` for the
        rationale).
        """
        device = (await self._netbox.get(f"/api/dcim/devices/{device_id}/")).json()
        observed = (device.get("custom_fields") or {}).get("qr_id")
        if observed is None:
            await self._netbox.patch(
                f"/api/dcim/devices/{device_id}/",
                json={"custom_fields": {"qr_id": qr_id}},
            )
            return "restored"
        logger.info(
            "qr_retire_compensation_noop",
            qr_id=qr_id,
            device_id=device_id,
            request_id=current_request_id(),
            observed_qr_id=observed,
        )
        return "noop_already_restored"

    async def _best_effort_inconsistency_journal(
        self,
        *,
        operation: str,
        device_id: int,
        qr_id: str,
        original_err: BaseException,
        comp_err: BaseException,
    ) -> None:
        """Branch 3 only: post a danger-kind NetBox journal entry. Swallows on failure.

        ``operation`` ("qr.bind" or "qr.retire") templates the message and
        derives the journal-failed log event key.
        """
        op_verb = operation.rsplit(".", 1)[-1]  # "bind" / "retire"
        op_prefix = operation.replace(".", "_")  # "qr_bind" / "qr_retire"
        try:
            await self._netbox.post(
                _JOURNAL_PATH,
                json={
                    "assigned_object_type": "dcim.device",
                    "assigned_object_id": device_id,
                    "kind": "danger",
                    "comments": (
                        f"INCONSISTENCY: QR {qr_id} {op_verb} compensation failed.\n"
                        f"NetBox custom_fields.qr_id state may diverge from qr_codes. "
                        f"Manual cleanup required.\n"
                        f"Original error: {original_err!r}\n"
                        f"Compensation error: {comp_err!r}"
                    ),
                },
            )
        except Exception as journal_err:
            logger.warning(
                f"{op_prefix}_inconsistency_journal_failed",
                qr_id=qr_id,
                device_id=device_id,
                request_id=current_request_id(),
                error=repr(journal_err),
            )

    async def _best_effort_compensation_audit(
        self,
        *,
        operation: str,
        qr_id: str,
        device_id: int,
        expected_version: str,
        user: AuthUser,
        failure_stage: Literal["db_commit", "compensation"],
        original_error: BaseException,
        compensation_error: BaseException | None,
        compensation_outcome: str,
    ) -> None:
        """Forensic record of compensation events. Best-effort: log on failure, never raise.

        Uses ``result=AuditResult.FAILURE`` and distinguishes compensation events
        from regular failures via ``after_json.failure_stage`` — no enum
        expansion, no migration. ``operation`` is "qr.bind" or "qr.retire".
        ``compensation_outcome`` is a free string ("cleared"/"noop_different_qr"
        for bind; "restored"/"noop_already_restored" for retire; "failed" for
        Branch 3 either way).
        """
        after: dict[str, Any] = {
            "failure_stage": failure_stage,
            "original_error": repr(original_error),
            "compensation_outcome": compensation_outcome,
        }
        if compensation_error is not None:
            after["compensation_error"] = repr(compensation_error)

        try:
            entry = AuditLogEntry(
                request_id=UUID(current_request_id()),
                timestamp=datetime.now(UTC),
                user_email=user.email or "",
                user_keycloak_id=UUID(user.sub),
                # Sprint 6 decision D: source from active shift, not JWT sid.
                session_id=user.shift_session_id,
                operation=operation,
                entity_type="qr",
                entity_id=qr_id,
                before_json={
                    "qr_id": qr_id,
                    "attempted_device_id": device_id,
                    "expected_version": expected_version,
                },
                after_json=after,
                result=AuditResult.FAILURE,
            )
            async with self._session.begin():
                await self._audit_log_repo.insert(entry)
        except Exception as audit_err:
            logger.error(
                "compensation_audit_write_failed",
                qr_id=qr_id,
                device_id=device_id,
                request_id=current_request_id(),
                error=repr(audit_err),
            )
