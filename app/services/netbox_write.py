"""Three-record NetBox write with optimistic concurrency. Architecture §3.1-3.2.

``NetBoxWriteService.patch_with_attribution`` is the apparatus every NetBox write
rides on (device update in Sprint 3, decommission/bind/retire in Sprint 4):

1. **Optimistic concurrency** (Sprint 3 decision A) — the backend re-reads the
   object and compares ``last_updated`` to the client's expected version itself,
   rather than passing ``If-Unmodified-Since`` to NetBox (NetBox does not honour
   conditional PATCH reliably). A version mismatch raises ``WriteConflictError``;
   no NetBox write happens. A small TOCTOU window between re-read and PATCH is
   accepted — mobile write concurrency is low.
2. **Three-record write** (CLAUDE.md cross-cutting #2, decision B) — on a match:
   NetBox PATCH, then a NetBox journal entry, then an app-DB ``audit_log`` row,
   all sharing one ``request_id``. The NetBox PATCH is the source-of-truth change
   and is durable once it returns; the journal entry and audit row are
   *best-effort* attribution — a failure there is logged loudly and never rolls
   back the PATCH (no distributed transaction exists).

Every outcome — success, conflict, failure — produces an ``audit_log`` row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogRepository
from app.domain.audit import AuditLogEntry, AuditResult
from app.netbox.client import NetBoxClient
from app.observability.request_id import current_request_id

logger = structlog.get_logger()

_JOURNAL_PATH = "/api/extras/journal-entries/"


class WriteConflictError(Exception):
    """The NetBox object changed since the client read it — no write was made.

    Carries the current object and its version so the endpoint can return the
    Architecture §3.2 ``409 / DEVICE_CONFLICT`` body with current state.
    """

    def __init__(self, *, current_object: dict[str, Any], current_version: str) -> None:
        super().__init__(f"NetBox object version mismatch; current version is {current_version!r}")
        self.current_object = current_object
        self.current_version = current_version


def _format_diff(original: dict[str, Any], changes: dict[str, Any]) -> str:
    """Render each changed field as ``  field: <old> → <new>`` for the journal entry."""
    return "\n".join(
        f"  {field}: {original.get(field)!r} → {new_value!r}"
        for field, new_value in changes.items()
    )


def _format_journal_comment(
    *,
    user: AuthUser,
    request_id: UUID,
    original: dict[str, Any],
    changes: dict[str, Any],
) -> str:
    """The human-readable attribution comment posted to the NetBox journal (§3.1)."""
    return (
        f"Modified by {user.email or 'unknown'} via mobile app.\n"
        f"Request ID: {request_id}\n"
        f"Session: {user.session_id or 'unknown'}\n"
        f"Changes:\n{_format_diff(original, changes)}"
    )


class NetBoxWriteService:
    """Performs a NetBox object PATCH with optimistic concurrency + three-record write."""

    def __init__(
        self,
        netbox_client: NetBoxClient,
        session: AsyncSession,
        audit_log_repo: AuditLogRepository,
    ) -> None:
        self._netbox = netbox_client
        self._session = session
        self._audit_log_repo = audit_log_repo

    async def patch_with_attribution(
        self,
        *,
        netbox_path: str,
        netbox_object_type: str,
        netbox_object_id: int,
        entity_type: str,
        operation: str,
        expected_version: str,
        changes: dict[str, Any],
        user: AuthUser,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        """Re-read, conflict-check, PATCH, then attribute via journal + audit row.

        Returns the updated NetBox object. Raises ``WriteConflictError`` on a
        version mismatch; re-raises any NetBox client error from the re-read or
        PATCH (after recording a ``failure`` audit row).

        ``entity_id`` defaults to ``str(netbox_object_id)`` — the right value
        for an entity that *is* the NetBox object (Sprint 3 device.update).
        Sprint 4's ``qr.bind``/``qr.retire`` pass the QR token explicitly so
        the audit row reflects which entity actually owns the operation,
        independent of which NetBox object the PATCH targets.
        """
        request_id = UUID(current_request_id())
        timestamp = datetime.now(UTC)
        if entity_id is None:
            entity_id = str(netbox_object_id)

        async def record_audit(
            before_json: dict[str, Any],
            after_json: dict[str, Any],
            result: AuditResult,
        ) -> None:
            """Best-effort audit-row insert (decision B): log on failure, never raise."""
            try:
                entry = AuditLogEntry(
                    request_id=request_id,
                    timestamp=timestamp,
                    user_email=user.email or "",
                    user_keycloak_id=UUID(user.sub),
                    session_id=UUID(user.session_id) if user.session_id else None,
                    operation=operation,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    before_json=before_json,
                    after_json=after_json,
                    result=result,
                )
                async with self._session.begin():
                    await self._audit_log_repo.insert(entry)
            except Exception as exc:
                logger.error(
                    "audit_log_write_failed",
                    request_id=str(request_id),
                    operation=operation,
                    entity_id=entity_id,
                    result=result.value,
                    error=repr(exc),
                )

        # Re-read. A failure here — including a NetBox response that omits
        # `last_updated` — still gets a FAILURE audit row (decision B).
        try:
            original: dict[str, Any] = (await self._netbox.get(netbox_path)).json()
            # Optimistic-concurrency check (decision A): opaque string compare,
            # never parsed. Extracted inside the try so a missing key routes
            # through the same FAILURE record.
            observed_version = original["last_updated"]
        except Exception:
            await record_audit({"expected_version": expected_version}, {}, AuditResult.FAILURE)
            raise

        if observed_version != expected_version:
            await record_audit(
                {"expected_version": expected_version},
                {"object": original, "observed_version": observed_version},
                AuditResult.CONFLICT,
            )
            raise WriteConflictError(current_object=original, current_version=observed_version)

        # The PATCH. The re-read object is now available for the failure record.
        try:
            updated: dict[str, Any] = (await self._netbox.patch(netbox_path, json=changes)).json()
        except Exception:
            await record_audit(
                {"object": original, "expected_version": expected_version},
                {},
                AuditResult.FAILURE,
            )
            raise

        # The NetBox PATCH is durable from here — journal + audit are best-effort.
        await self._post_journal_entry(
            netbox_object_type=netbox_object_type,
            netbox_object_id=netbox_object_id,
            request_id=request_id,
            user=user,
            original=original,
            changes=changes,
        )
        await record_audit(
            {"object": original, "expected_version": expected_version},
            {"object": updated, "observed_version": observed_version},
            AuditResult.SUCCESS,
        )
        return updated

    async def _post_journal_entry(
        self,
        *,
        netbox_object_type: str,
        netbox_object_id: int,
        request_id: UUID,
        user: AuthUser,
        original: dict[str, Any],
        changes: dict[str, Any],
    ) -> None:
        """Best-effort journal POST (decision B): log on failure, never raise."""
        payload = {
            "assigned_object_type": netbox_object_type,
            "assigned_object_id": netbox_object_id,
            "kind": "info",
            "comments": _format_journal_comment(
                user=user, request_id=request_id, original=original, changes=changes
            ),
        }
        try:
            await self._netbox.post(_JOURNAL_PATH, json=payload)
        except Exception as exc:
            logger.error(
                "netbox_journal_write_failed",
                request_id=str(request_id),
                netbox_object_type=netbox_object_type,
                netbox_object_id=netbox_object_id,
                error=repr(exc),
            )
