"""Integration tests for the ``/web/`` dashboard page (Sprint 8b Task 1).

Drives the full ASGI stack via httpx.AsyncClient with a valid session cookie.
The Task 0 OIDC flow tests already cover the auth-failure modes (no cookie →
302; no shift → 403 + intermediate page); here we focus on the page-specific
rendering: card labels are present, the counter values come from the live
repo, and ``generated_at`` is in the output.
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
        await session.execute(
            text("TRUNCATE qr_codes, qr_batches, shift_sessions, audit_log CASCADE")
        )
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


# ---------- card labels + counter values ------------------------------------


async def test_dashboard_page_renders_all_six_card_labels(
    client: httpx.AsyncClient,
) -> None:
    """All six counter cards must appear by their human-readable label so the
    template stays in sync with the underlying snapshot fields."""
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/")
    assert resp.status_code == 200, resp.text
    for label in (
        "Free QRs",
        "Bound QRs",
        "Retired QRs",
        "Batches (last 30 days)",
        "Active shifts",
        "Audit rows (last 24h)",
    ):
        assert label in resp.text, f"missing card label: {label!r}"


async def test_dashboard_page_reflects_seeded_counter_values(
    client: httpx.AsyncClient,
) -> None:
    """Seed some QRs and assert the numbers appear in the rendered HTML."""
    async with get_sessionmaker()() as session:
        batch_id = uuid4()
        await QRBatchRepository(session).insert(
            QRBatch(
                id=batch_id,
                created_at=datetime.now(UTC),
                created_by_email="alice@example.com",
                created_by_keycloak_id=_USER_SUB,
                count=0,
                intended_site_id=None,
                intended_location_id=None,
                intended_rack_id=None,
                comment=None,
            )
        )
        await QRCodeRepository(session).bulk_insert(
            [
                QR(
                    id=f"DCQR-F{i:03d}",
                    batch_id=batch_id,
                    status=QRStatus.FREE,
                    bound_to_device_id=None,
                    bound_at=None,
                    bound_by_email=None,
                    retired_at=None,
                    retired_reason=None,
                )
                for i in range(7)
            ]
        )
        await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/")
    assert resp.status_code == 200, resp.text
    # 7 free QRs, 1 batch in the last 30 days — both must surface.
    assert ">7<" in resp.text  # qr_free_count card value
    assert ">1<" in resp.text  # batches_last_30_days card value


async def test_dashboard_page_shows_generated_at_freshness_line(
    client: httpx.AsyncClient,
) -> None:
    """Operator wants freshness — the 'As of YYYY-MM-DD HH:MM:SS UTC' line
    must appear so they don't read stale numbers without realising."""
    client.cookies.set(SESSION_COOKIE_NAME, _valid_session_cookie())
    resp = await client.get("/web/")
    assert resp.status_code == 200
    assert "As of " in resp.text
    assert "UTC" in resp.text


async def test_dashboard_page_redirects_to_login_without_cookie(
    client: httpx.AsyncClient,
) -> None:
    """Smoke: Task 0 already covers this; one assertion kept so a regression
    in the dashboard handler itself (e.g. wrong dep order) is caught."""
    resp = await client.get("/web/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/web/login" in resp.headers["location"]
