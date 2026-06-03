"""Unit tests for GET /api/v1/admin/dashboard (Sprint 8b Task 1).

Two styles per the project convention:
- Direct-await handler tests for the shape of the response + the empty-DB
  default (coverage traces these reliably; AsyncClient does not for `await`'d
  returns inside ASGI).
- AsyncClient tests for role gating + the active-shift gate.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.dashboard import (
    DashboardSnapshotResponse,
    _to_response,
    get_dashboard,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.dashboard import DashboardSnapshot
from app.domain.qr import QR, QRBatch, QRStatus
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_USER_KEYCLOAK_ID = UUID("11111111-1111-1111-1111-111111111111")
_SHIFT_SESSION_ID = UUID("33333333-3333-3333-3333-333333333333")


def _admin_user(*, shift_session_id: UUID | None = _SHIFT_SESSION_ID) -> AuthUser:
    return dataclasses.replace(make_user("dcinv-admin"), shift_session_id=shift_session_id)


# ---------- _to_response ----------------------------------------------------


def test_to_response_mirrors_domain_snapshot_field_for_field() -> None:
    """The wire shape is a 1:1 mirror of the domain DTO — no transformations."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    snap = DashboardSnapshot(
        qr_free_count=10,
        qr_bound_count=5,
        qr_retired_count=2,
        batches_last_30_days=7,
        active_shifts_count=3,
        audit_rows_last_24h=42,
        generated_at=now,
    )
    resp = _to_response(snap)
    assert isinstance(resp, DashboardSnapshotResponse)
    assert resp.qr_free_count == 10
    assert resp.qr_bound_count == 5
    assert resp.qr_retired_count == 2
    assert resp.batches_last_30_days == 7
    assert resp.active_shifts_count == 3
    assert resp.audit_rows_last_24h == 42
    assert resp.generated_at == now


# ---------- get_dashboard handler (direct await) -----------------------------


async def test_get_dashboard_returns_zero_counts_with_no_qr_or_audit_data(
    session: AsyncSession,
) -> None:
    """No QRs, batches, or audit rows → those counters are zero. The conftest
    seeds an active shift before each test for the dep-layer gate, so
    ``active_shifts_count == 1``."""
    result = await get_dashboard(user=_admin_user(), session=session)
    assert isinstance(result, DashboardSnapshotResponse)
    assert result.qr_free_count == 0
    assert result.qr_bound_count == 0
    assert result.qr_retired_count == 0
    assert result.batches_last_30_days == 0
    assert result.active_shifts_count == 1
    assert result.audit_rows_last_24h == 0
    # generated_at should be within a few seconds of "now".
    delta_s = abs((datetime.now(UTC) - result.generated_at).total_seconds())
    assert delta_s < 5


async def test_get_dashboard_returns_seeded_counts(
    session: AsyncSession,
) -> None:
    """Seed mixed data and assert each counter matches."""
    async with get_sessionmaker()() as db:
        batch_id = uuid4()
        await QRBatchRepository(db).insert(
            QRBatch(
                id=batch_id,
                created_at=datetime.now(UTC),
                created_by_email="alice@example.com",
                created_by_keycloak_id=_USER_KEYCLOAK_ID,
                count=0,
                intended_site_id=None,
                intended_location_id=None,
                intended_rack_id=None,
                comment=None,
            )
        )
        await QRCodeRepository(db).bulk_insert(
            [
                QR(
                    id="DCQR-F001",
                    batch_id=batch_id,
                    status=QRStatus.FREE,
                    bound_to_device_id=None,
                    bound_at=None,
                    bound_by_email=None,
                    retired_at=None,
                    retired_reason=None,
                ),
                QR(
                    id="DCQR-F002",
                    batch_id=batch_id,
                    status=QRStatus.FREE,
                    bound_to_device_id=None,
                    bound_at=None,
                    bound_by_email=None,
                    retired_at=None,
                    retired_reason=None,
                ),
            ]
        )
        await AuditLogRepository(db).insert(
            AuditLogEntry(
                request_id=uuid4(),
                timestamp=datetime.now(UTC),
                user_email="alice@example.com",
                user_keycloak_id=_USER_KEYCLOAK_ID,
                session_id=None,
                operation="qr.bind",
                entity_type="qr",
                entity_id="DCQR-1",
                before_json={},
                after_json={},
                result=AuditResult.SUCCESS,
            )
        )
        await db.commit()

    result = await get_dashboard(user=_admin_user(), session=session)
    assert result.qr_free_count == 2
    assert result.batches_last_30_days == 1
    assert result.audit_rows_last_24h >= 1  # at least the one we just seeded


# ---------- routing + role + active-shift gating (AsyncClient) ---------------


async def test_get_dashboard_returns_403_when_caller_lacks_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get("/api/v1/admin/dashboard")
    assert resp.status_code == 403


async def test_get_dashboard_returns_409_no_active_shift_when_admin_has_no_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Decision: admin endpoint is gated by require_role_with_active_shift."""
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/dashboard")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_get_dashboard_returns_200_with_counter_envelope(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    expected_keys = {
        "qr_free_count",
        "qr_bound_count",
        "qr_retired_count",
        "batches_last_30_days",
        "active_shifts_count",
        "audit_rows_last_24h",
        "generated_at",
    }
    assert expected_keys <= set(body.keys())
    for k in expected_keys - {"generated_at"}:
        assert isinstance(body[k], int)


async def test_get_dashboard_does_not_write_an_audit_row(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Operational read — no ``audit_log`` row, by design (decision 2,
    parallels ``/admin/sessions``)."""
    as_user("dcinv-admin")
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE audit_log CASCADE"))
        await db.commit()

    resp = await client.get("/api/v1/admin/dashboard")
    assert resp.status_code == 200

    async with get_sessionmaker()() as db:
        rows = await db.execute(text("SELECT COUNT(*) FROM audit_log"))
        assert rows.scalar_one() == 0
