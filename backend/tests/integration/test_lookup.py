"""Integration tests for app.services.qr.lookup.QRLookupService.

Exercises the service directly (not through HTTP) so each branch of
``get_by_id`` is covered against the real test DB. Sprint 4 Task 3:
DeviceService is now a required dependency; NetBox is faked with respx for
the BOUND-path test (so the test doesn't need a live NetBox).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
import structlog
from sqlalchemy import text

from app.config import get_settings
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.qr import QR, QRBatch, QRStatus
from app.netbox.client import NetBoxClient, get_netbox_client
from app.services.device import DeviceService
from app.services.qr.lookup import QRLookupResponse, QRLookupService

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
_DEVICE_PATH = "/api/dcim/devices/42/"


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
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_netbox_client.cache_clear()
    structlog.contextvars.clear_contextvars()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE qr_codes, qr_batches CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_netbox_client.cache_clear()
    structlog.contextvars.clear_contextvars()


async def _seed(qr: QR, *, intended_site_id: int | None = 9) -> QRBatch:
    batch = QRBatch(
        id=qr.batch_id,
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=1,
        intended_site_id=intended_site_id,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()
    return batch


def _free(qr_id: str, batch_id: UUID) -> QR:
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


def _device_payload() -> dict[str, Any]:
    return {
        "id": 42,
        "name": "sw-42",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 10,
        "serial": "X9",
        "comments": "",
        "asset_tag": None,
        "custom_fields": {"qr_id": None},
        "last_updated": "2026-05-24T08:00:00.000000Z",
        "device_type": {
            "id": 11,
            "display": "C9300-48U",
            "manufacturer": {"id": 21, "name": "Cisco"},
            "u_height": 1,
        },
        "role": {"id": 31, "name": "Access Switch"},
        "primary_ip4": None,
        "primary_ip6": None,
    }


def _netbox_base() -> str:
    return str(get_settings().netbox_url).rstrip("/")


def _build(session: Any, client: NetBoxClient) -> QRLookupService:
    return QRLookupService(
        QRCodeRepository(session),
        QRBatchRepository(session),
        DeviceService(client),
    )


async def test_get_by_id_returns_none_for_unknown_id() -> None:
    async with NetBoxClient.from_settings() as client, get_sessionmaker()() as session:
        service = _build(session, client)
        assert await service.get_by_id("DCQR-ZZZZZZZZ") is None


async def test_get_by_id_returns_combined_response_for_free_qr() -> None:
    batch_id = uuid4()
    await _seed(_free("DCQR-AAAAAAAA", batch_id), intended_site_id=9)

    async with NetBoxClient.from_settings() as client, get_sessionmaker()() as session:
        service = _build(session, client)
        result = await service.get_by_id("DCQR-AAAAAAAA")

    assert isinstance(result, QRLookupResponse)
    assert result.qr.id == "DCQR-AAAAAAAA"
    assert result.qr.status is QRStatus.FREE
    assert result.qr.batch.intended_site_id == 9
    # FREE: no device fetch, no device_error
    assert result.device is None
    assert result.device_error is None


async def test_get_by_id_resolves_bound_code_and_fetches_device() -> None:
    batch_id = uuid4()
    bound = QR(
        id="DCQR-BBBBBBBB",
        batch_id=batch_id,
        status=QRStatus.BOUND,
        bound_to_device_id=42,
        bound_at=_NOW,
        bound_by_email="bob@example.com",
        retired_at=None,
        retired_reason=None,
    )
    await _seed(bound)
    base = _netbox_base()

    async with NetBoxClient.from_settings() as client, get_sessionmaker()() as session:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{base}{_DEVICE_PATH}").respond(json=_device_payload())
            service = _build(session, client)
            result = await service.get_by_id("DCQR-BBBBBBBB")

    assert result is not None
    assert result.qr.status is QRStatus.BOUND
    assert result.qr.bound_to_device_id == 42
    assert result.device is not None
    assert result.device.id == 42
    # Decision H: qr_id on device comes from app DB (the QR token), not NetBox
    assert result.device.qr_id == "DCQR-BBBBBBBB"
    assert result.device_error is None
