"""End-to-end integration tests for /api/v1/admin/sessions (Sprint 7 Task 3)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.auth.dependencies import AuthUser, get_current_user
from app.config import get_settings
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.shift_session import ShiftEndReason, ShiftSession
from app.main import app
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_KEYCLOAK_ID = "11111111-1111-1111-1111-111111111111"
_TARGET_USER = UUID("44444444-4444-4444-4444-444444444444")


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
async def _truncate_and_seed_shift() -> AsyncGenerator[None, None]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE audit_log, shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
async def admin_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    admin = AuthUser(
        sub=_USER_KEYCLOAK_ID,
        email="alice@example.com",
        roles=("dcinv-admin",),
        session_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _target_shift(*, start_at: datetime, tablet_id: str = "tablet-bob") -> ShiftSession:
    return ShiftSession(
        id=uuid4(),
        user_email="bob@example.com",
        user_keycloak_id=_TARGET_USER,
        shift_start_at=start_at,
        shift_end_at=None,
        tablet_id=tablet_id,
        end_reason=None,
    )


async def test_list_filters_active_only_excludes_ended_against_real_db(
    admin_client: httpx.AsyncClient,
) -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    active = _target_shift(start_at=base - timedelta(hours=1))
    ended = _target_shift(start_at=base - timedelta(hours=2), tablet_id="tablet-bob-2").end(
        reason=ShiftEndReason.MANUAL, at=base - timedelta(minutes=30)
    )
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(active)
        await repo.insert(ended)
        await db.commit()

    resp = await admin_client.get("/api/v1/admin/sessions?active_only=true")
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["results"]}
    assert str(active.id) in ids
    assert str(ended.id) not in ids


async def test_list_paginates_with_has_more_against_real_db(
    admin_client: httpx.AsyncClient,
) -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    # Seed 4 shifts with unique users (partial unique index allows only one
    # active per user). page_size=2 + the admin's own seeded shift = 5 rows
    # total → page 1 has 2 + has_more, page 2 has 2 + has_more, page 3 has 1.
    users = [UUID(f"{i:08x}-0000-0000-0000-000000000000") for i in range(2, 6)]
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        for i, uid in enumerate(users):
            await repo.insert(
                ShiftSession(
                    id=uuid4(),
                    user_email=f"u{i}@example.com",
                    user_keycloak_id=uid,
                    shift_start_at=base - timedelta(hours=i + 1),
                    shift_end_at=None,
                    tablet_id=f"t{i}",
                    end_reason=None,
                )
            )
        await db.commit()

    resp = await admin_client.get("/api/v1/admin/sessions?page_size=2&page=1")
    p1 = resp.json()
    assert len(p1["results"]) == 2 and p1["has_more"] is True

    resp = await admin_client.get("/api/v1/admin/sessions?page_size=2&page=2")
    p2 = resp.json()
    assert len(p2["results"]) == 2 and p2["has_more"] is True

    resp = await admin_client.get("/api/v1/admin/sessions?page_size=2&page=3")
    p3 = resp.json()
    assert len(p3["results"]) == 1 and p3["has_more"] is False


async def test_force_close_ends_target_shift_and_persists_audit_row_against_real_db(
    admin_client: httpx.AsyncClient,
) -> None:
    target = _target_shift(start_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(target)
        await db.commit()

    resp = await admin_client.post(
        f"/api/v1/admin/sessions/{target.id}/force-close",
        json={"reason": "Engineer left without ending shift"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(target.id)
    assert body["end_reason"] == "forced"
    assert body["shift_end_at"] is not None

    # Verify persisted state.
    async with get_sessionmaker()() as db:
        persisted = await ShiftSessionRepository(db).get_by_id(target.id)
    assert persisted is not None
    assert persisted.end_reason is ShiftEndReason.FORCED

    # Audit row carries the reason.
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text(
                "SELECT result, after_json, entity_id"
                " FROM audit_log WHERE operation = 'shift_session.force_close'"
            )
        )
        records = rows.fetchall()
    assert len(records) == 1
    res, after, eid = records[0]
    assert res == "success"
    assert eid == str(target.id)
    assert after["reason"] == "Engineer left without ending shift"


async def test_force_close_idempotent_no_op_against_real_db(
    admin_client: httpx.AsyncClient,
) -> None:
    already_ended = _target_shift(start_at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)).end(
        reason=ShiftEndReason.MANUAL, at=datetime(2026, 6, 1, 17, 0, 0, tzinfo=UTC)
    )
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(already_ended)
        await db.commit()

    resp = await admin_client.post(
        f"/api/v1/admin/sessions/{already_ended.id}/force-close",
        json={"reason": "Late attempt after manual end"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # end_reason stays MANUAL — the no-op does NOT overwrite the prior end.
    assert body["end_reason"] == "manual"

    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text(
                "SELECT result, after_json"
                " FROM audit_log WHERE operation = 'shift_session.force_close'"
            )
        )
        records = rows.fetchall()
    assert len(records) == 1
    res, after = records[0]
    assert res == "conflict"
    assert after["no_op"] is True


async def test_force_close_returns_404_for_unknown_session_id(
    admin_client: httpx.AsyncClient,
) -> None:
    resp = await admin_client.post(
        f"/api/v1/admin/sessions/{uuid4()}/force-close",
        json={"reason": "any reason"},
    )
    assert resp.status_code == 404

    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text("SELECT COUNT(*) FROM audit_log" " WHERE operation = 'shift_session.force_close'")
        )
        assert rows.scalar_one() == 0


# ---------- Sprint 8a Task 0: admin-shift-open unblocks live admin use -------


async def test_admin_can_open_shift_then_use_admin_audit_endpoint(
    admin_client: httpx.AsyncClient,
) -> None:
    """End-to-end: without an active shift, /admin/audit returns 409. After
    /admin/sessions/start succeeds, the same /admin/audit call returns 200.
    This is the contract that makes Sprint 7's admin endpoints live-usable."""
    # Wipe the conftest's pre-seeded shift to simulate a fresh admin login.
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()

    # Step 1 — /admin/audit returns 409 before the shift exists.
    pre = await admin_client.get("/api/v1/admin/audit")
    assert pre.status_code == 409
    assert pre.json()["error"]["code"] == "NO_ACTIVE_SHIFT"

    # Step 2 — open the admin shift.
    start = await admin_client.post(
        "/api/v1/admin/sessions/start", json={"workstation_id": "admin-ws-01"}
    )
    assert start.status_code == 200, start.text

    # Step 3 — /admin/audit now works.
    post = await admin_client.get("/api/v1/admin/audit")
    assert post.status_code == 200
    body = post.json()
    assert {"results", "page", "page_size", "has_more"} <= set(body.keys())


async def test_admin_can_open_shift_then_create_batch_with_attributed_audit_row(
    admin_client: httpx.AsyncClient,
) -> None:
    """End-to-end: after /admin/sessions/start, POST /admin/batches/ produces
    an audit row whose session_id matches the admin's freshly-opened shift —
    proving the Sprint 8a Task 0 source swap (QRGenerationService:
    session_id=None → user.shift_session_id) is wired end-to-end."""
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()

    start = await admin_client.post(
        "/api/v1/admin/sessions/start", json={"workstation_id": "admin-ws-01"}
    )
    assert start.status_code == 200
    shift_id = start.json()["id"]

    create = await admin_client.post("/api/v1/admin/batches/", json={"count": 2})
    assert create.status_code == 201

    # Audit row for qr.generate_batch carries the admin's shift session_id.
    async with get_sessionmaker()() as db:
        row = (
            await db.execute(
                text(
                    "SELECT session_id::text, operation FROM audit_log"
                    " WHERE operation = 'qr.generate_batch'"
                )
            )
        ).one()
    assert row.session_id == shift_id
