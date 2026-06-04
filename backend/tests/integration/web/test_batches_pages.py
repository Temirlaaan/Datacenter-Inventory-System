"""Integration tests for the ``/web/batches/`` list + detail pages
(Sprint 8b Task 2).

Drives the full ASGI stack with a valid session cookie. The Task 0 OIDC
flow tests cover auth-failure modes (no cookie / no shift); here we focus
on page-specific rendering: list shows seeded rows + pagination,
detail shows metadata + status counts + Download Labels link, unknown
batch id renders the custom 404 page.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.main import app
from app.web.auth import (
    SESSION_COOKIE_NAME,
    build_session_cookie_payload,
    encode_session_cookie,
    reset_web_auth_cache,
)
from tests.integration.conftest import seed_default_active_shift

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[3]
_USER_SUB = UUID("11111111-1111-1111-1111-111111111111")
_FERNET_KEY = "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo="


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
async def _setup(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None, None]:
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_ID", "dcinv-web")
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "test-web-client-secret")
    monkeypatch.setenv("SESSION_COOKIE_KEY", _FERNET_KEY)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_web_auth_cache()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE qr_codes, qr_batches, shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_web_auth_cache()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _valid_session_cookie() -> str:
    user = build_session_cookie_payload(
        sub=_USER_SUB, email="alice@example.com", roles=("dcinv-admin",)
    )
    return encode_session_cookie(user)


def _seed_batch(*, count: int = 0, comment: str | None = None) -> QRBatch:
    return QRBatch(
        id=uuid4(),
        created_at=datetime.now(UTC),
        created_by_email="alice@example.com",
        created_by_keycloak_id=_USER_SUB,
        count=count,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment=comment,
    )


def _free_qr(qr_id: str, batch_id: UUID) -> QR:
    return QR(
        id=qr_id,
        batch_id=batch_id,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


# ---------- /web/batches/ list -----------------------------------------------


async def test_batches_list_renders_seeded_batch_row(
    client: httpx.AsyncClient,
) -> None:
    batch = _seed_batch(count=3, comment="alpha-batch")
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/batches/")
    assert resp.status_code == 200, resp.text
    assert "alpha-batch" in resp.text
    assert f"/web/batches/{batch.id}" in resp.text
    # Column headers must surface so the table is recognisable.
    for label in ("Created at", "Count", "Created by", "Comment"):
        assert label in resp.text


async def test_batches_list_shows_empty_state_when_no_batches(
    client: httpx.AsyncClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/batches/")
    assert resp.status_code == 200
    assert "No batches yet" in resp.text


async def test_batches_list_pagination_links_carry_page_param(
    client: httpx.AsyncClient,
) -> None:
    """With page_size=20 + 21 batches, page 1 must show a Next link to page 2."""
    async with get_sessionmaker()() as session:
        repo = QRBatchRepository(session)
        for _ in range(21):
            await repo.insert(_seed_batch())
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/batches/")
    assert resp.status_code == 200
    assert "/web/batches/?page=2" in resp.text
    assert "Prev" not in resp.text  # page 1: no Prev link
    resp_p2 = await client.get("/web/batches/?page=2")
    assert resp_p2.status_code == 200
    assert "/web/batches/?page=1" in resp_p2.text  # Prev link back to page 1


# ---------- /web/batches/{id} detail -----------------------------------------


async def test_batches_detail_renders_metadata_and_status_counts(
    client: httpx.AsyncClient,
) -> None:
    batch = _seed_batch(count=2, comment="detail-batch")
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert(
            [_free_qr(f"DCQR-D{i:07d}", batch.id) for i in range(2)]
        )
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get(f"/web/batches/{batch.id}")
    assert resp.status_code == 200, resp.text
    assert "detail-batch" in resp.text
    # Status summary chips (decision D8).
    assert "Free: 2" in resp.text
    assert "Bound: 0" in resp.text
    assert "Retired: 0" in resp.text
    # Download Labels link points at the JSON endpoint, not the web page.
    assert f"/api/v1/admin/batches/{batch.id}/labels.pdf" in resp.text
    # The QR codes appear in the table.
    assert "DCQR-D0000000" in resp.text
    assert "DCQR-D0000001" in resp.text


async def test_batches_detail_renders_empty_codes_state_when_batch_has_no_codes(
    client: httpx.AsyncClient,
) -> None:
    batch = _seed_batch(count=0)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get(f"/web/batches/{batch.id}")
    assert resp.status_code == 200
    assert "This batch has no codes." in resp.text


async def test_batches_detail_renders_404_page_for_unknown_batch_id(
    client: httpx.AsyncClient,
) -> None:
    """Decision 9: unknown batch id → 404 + custom HTML page (NOT JSON)."""
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    unknown = uuid4()
    resp = await client.get(f"/web/batches/{unknown}")
    assert resp.status_code == 404
    assert "Not found" in resp.text
    assert str(unknown) in resp.text
    # Web flow renders HTML, not the FastAPI default JSON body.
    assert "<html" in resp.text.lower()


# ---------- auth smoke (already proven in Task 0; one regression guard) -----


async def test_batches_list_redirects_to_login_without_cookie(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/web/batches/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/web/login" in resp.headers["location"]
