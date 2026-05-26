"""Integration tests for app.services.qr.generation.QRGenerationService.

Headline tests are the atomicity checks: a failure inside the inner transaction
must leave no orphan batch or codes, but must produce a ``result='failure'``
audit row in a separate transaction.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from uuid import UUID

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser
from app.db.repositories import (
    AuditLogRepository,
    QRBatchRepository,
    QRCodeRepository,
)
from app.db.session import get_engine, get_sessionmaker
from app.domain.audit import AuditLogEntry
from app.domain.qr import QR
from app.services.qr.generation import GenerateBatchRequest, QRGenerationService

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_KEYCLOAK_ID = "11111111-1111-1111-1111-111111111111"


def _alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=_BACKEND_DIR,
        timeout=30,
    )
    assert (
        result.returncode == 0
    ), f"alembic {args!r} failed: stdout={result.stdout!r} stderr={result.stderr!r}"


@pytest.fixture(scope="module", autouse=True)
def _clean_schema() -> Generator[None, None, None]:
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


@pytest.fixture(autouse=True)
async def _truncate() -> AsyncGenerator[None, None]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    structlog.contextvars.clear_contextvars()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE qr_codes, qr_batches, audit_log CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    structlog.contextvars.clear_contextvars()


def _user() -> AuthUser:
    return AuthUser(
        sub=_USER_KEYCLOAK_ID,
        email="alice@example.com",
        roles=("dcinv-admin",),
        session_id=None,
    )


def _service(session: AsyncSession) -> QRGenerationService:
    return QRGenerationService(
        session=session,
        qr_batch_repo=QRBatchRepository(session),
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=AuditLogRepository(session),
    )


# === Happy path ===============================================================


async def test_generate_batch_writes_batch_codes_and_one_success_audit_row() -> None:
    request = GenerateBatchRequest(count=50)
    async with get_sessionmaker()() as session:
        batch = await _service(session).generate_batch(request, _user())
        await session.commit()

    async with get_sessionmaker()() as session:
        batches = (await session.execute(text("SELECT COUNT(*) FROM qr_batches"))).scalar_one()
        codes = (await session.execute(text("SELECT COUNT(*) FROM qr_codes"))).scalar_one()
        audit_rows = (
            await session.execute(text("SELECT result::text, operation, entity_id FROM audit_log"))
        ).all()

    assert batches == 1
    assert codes == 50
    assert len(audit_rows) == 1
    assert audit_rows[0].result == "success"
    assert audit_rows[0].operation == "qr.generate_batch"
    assert audit_rows[0].entity_id == str(batch.id)


async def test_generate_batch_persists_intended_site_location_rack_and_comment() -> None:
    request = GenerateBatchRequest(
        count=3,
        intended_site_id=1,
        intended_location_id=2,
        intended_rack_id=3,
        comment="rack 14",
    )
    async with get_sessionmaker()() as session:
        batch = await _service(session).generate_batch(request, _user())
        await session.commit()

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT intended_site_id, intended_location_id,"
                    " intended_rack_id, comment FROM qr_batches"
                    " WHERE id = :id"
                ),
                {"id": batch.id},
            )
        ).one()
    assert row.intended_site_id == 1
    assert row.intended_location_id == 2
    assert row.intended_rack_id == 3
    assert row.comment == "rack 14"


async def test_generate_batch_codes_all_have_status_free_and_no_binding_fields() -> None:
    async with get_sessionmaker()() as session:
        batch = await _service(session).generate_batch(GenerateBatchRequest(count=5), _user())
        await session.commit()

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT status::text, bound_to_device_id, bound_at,"
                    " bound_by_email, retired_at, retired_reason"
                    " FROM qr_codes WHERE batch_id = :id"
                ),
                {"id": batch.id},
            )
        ).all()
    assert len(rows) == 5
    for row in rows:
        assert row.status == "free"
        assert row.bound_to_device_id is None
        assert row.bound_at is None
        assert row.bound_by_email is None
        assert row.retired_at is None
        assert row.retired_reason is None


async def test_generate_batch_tokens_are_unique_within_and_across_batches() -> None:
    async with get_sessionmaker()() as session:
        batch_a = await _service(session).generate_batch(GenerateBatchRequest(count=20), _user())
        await session.commit()
    async with get_sessionmaker()() as session:
        batch_b = await _service(session).generate_batch(GenerateBatchRequest(count=20), _user())
        await session.commit()

    async with get_sessionmaker()() as session:
        ids_a = {
            r[0]
            for r in (
                await session.execute(
                    text("SELECT id FROM qr_codes WHERE batch_id = :id"),
                    {"id": batch_a.id},
                )
            ).all()
        }
        ids_b = {
            r[0]
            for r in (
                await session.execute(
                    text("SELECT id FROM qr_codes WHERE batch_id = :id"),
                    {"id": batch_b.id},
                )
            ).all()
        }
    assert len(ids_a) == 20
    assert len(ids_b) == 20
    assert ids_a.isdisjoint(ids_b)


async def test_generate_batch_audit_request_id_matches_structlog_contextvar() -> None:
    bound_id = "8400e7f2-aaaa-bbbb-cccc-1234567890ab"
    structlog.contextvars.bind_contextvars(request_id=bound_id)
    try:
        async with get_sessionmaker()() as session:
            await _service(session).generate_batch(GenerateBatchRequest(count=2), _user())
            await session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("request_id")

    async with get_sessionmaker()() as session:
        rid = (await session.execute(text("SELECT request_id::text FROM audit_log"))).scalar_one()
    assert rid == bound_id


# === Atomicity failures =======================================================


async def test_generate_batch_failure_in_bulk_insert_writes_failure_audit_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(RuntimeError):
        pass

    async def _explode(self: QRCodeRepository, codes: list[QR]) -> None:
        raise _Boom("simulated bulk_insert failure")

    monkeypatch.setattr(QRCodeRepository, "bulk_insert", _explode)

    async with get_sessionmaker()() as session:
        with pytest.raises(_Boom):
            await _service(session).generate_batch(GenerateBatchRequest(count=10), _user())

    async with get_sessionmaker()() as session:
        batches = (await session.execute(text("SELECT COUNT(*) FROM qr_batches"))).scalar_one()
        codes = (await session.execute(text("SELECT COUNT(*) FROM qr_codes"))).scalar_one()
        audit_rows = (
            await session.execute(text("SELECT result::text, operation FROM audit_log"))
        ).all()

    assert batches == 0
    assert codes == 0
    assert len(audit_rows) == 1
    assert audit_rows[0].result == "failure"
    assert audit_rows[0].operation == "qr.generate_batch"


async def test_generate_batch_failure_in_audit_insert_rolls_back_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit insert raises on the success-path call but succeeds on the failure-path
    call. After the dust settles: no batch, no codes, one ``result='failure'``
    audit row.
    """
    real_insert = AuditLogRepository.insert
    call_count = 0

    async def _flaky_insert(self: AuditLogRepository, entry: AuditLogEntry) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated audit insert failure on success path")
        await real_insert(self, entry)

    monkeypatch.setattr(AuditLogRepository, "insert", _flaky_insert)

    async with get_sessionmaker()() as session:
        with pytest.raises(RuntimeError, match="simulated audit insert failure"):
            await _service(session).generate_batch(GenerateBatchRequest(count=5), _user())

    async with get_sessionmaker()() as session:
        batches = (await session.execute(text("SELECT COUNT(*) FROM qr_batches"))).scalar_one()
        codes = (await session.execute(text("SELECT COUNT(*) FROM qr_codes"))).scalar_one()
        audit_rows = (await session.execute(text("SELECT result::text FROM audit_log"))).all()

    assert batches == 0
    assert codes == 0
    assert len(audit_rows) == 1
    assert audit_rows[0].result == "failure"
    assert call_count == 2  # one for success path (raised), one for failure path


async def test_generate_batch_failure_audit_carries_intended_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(RuntimeError):
        pass

    async def _explode(self: QRCodeRepository, codes: list[QR]) -> None:
        raise _Boom("oops")

    monkeypatch.setattr(QRCodeRepository, "bulk_insert", _explode)

    async with get_sessionmaker()() as session:
        with pytest.raises(_Boom):
            await _service(session).generate_batch(GenerateBatchRequest(count=3), _user())

    async with get_sessionmaker()() as session:
        entity_id = (await session.execute(text("SELECT entity_id FROM audit_log"))).scalar_one()
    # entity_id is a parseable UUID even though the batch row itself doesn't
    # exist — gives a forensic correlation between request_id and intended id.
    UUID(entity_id)


async def test_generate_batch_with_no_email_writes_empty_string_to_audit() -> None:
    # AuthUser.email is str | None; the audit row column is NOT NULL — service
    # falls back to "". Calling this out so a later policy decision is testable.
    user_without_email = AuthUser(
        sub=_USER_KEYCLOAK_ID,
        email=None,
        roles=("dcinv-admin",),
        session_id=None,
    )
    async with get_sessionmaker()() as session:
        await _service(session).generate_batch(GenerateBatchRequest(count=1), user_without_email)
        await session.commit()

    async with get_sessionmaker()() as session:
        row = (await session.execute(text("SELECT user_email FROM audit_log"))).one()
        batch_row = (await session.execute(text("SELECT created_by_email FROM qr_batches"))).one()
    assert row.user_email == ""
    assert batch_row.created_by_email == ""
