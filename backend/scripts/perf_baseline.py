"""One-time performance baseline (Sprint 8a Task 4, ToR §5.1).

Operator-runnable, NOT CI-wired. Drives an in-process ASGI client against
the test Postgres + respx-mocked NetBox to measure two endpoints' p95
latency against ToR §5.1's targets:

- ``GET /api/v1/qr/{qr_id}``      target ≤ 800ms p95
- ``PATCH /api/v1/devices/{id}``  target ≤ 1500ms p95

This is a development-loop measurement (no real NetBox, no concurrent
load, no production resource limits). Operators should re-run against
production-like infra for acceptance.

Usage::

    cd backend
    DATABASE_URL='postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test' \\
      NETBOX_URL='https://netbox.example.com' \\
      NETBOX_SERVICE_TOKEN='test-token' \\
      KEYCLOAK_BASE_URL='https://sso.example.com' \\
      uv run python scripts/perf_baseline.py

Disables rate limiting via env (``RATE_LIMIT_ENABLED=false``) so the 100-
iteration loop doesn't trip the 60/min READ budget.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

# --- run from a clean state ----------------------------------------------------

os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["SHIFT_AUTO_END_ENABLED"] = "false"
os.environ["NETBOX_CIRCUIT_ENABLED"] = "false"

# Imports AFTER env setup so module-level Settings reads pick up our values.
import httpx  # noqa: E402
import respx  # noqa: E402

from app.auth.dependencies import AuthUser, get_current_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.repositories.qr_batch import QRBatchRepository  # noqa: E402
from app.db.repositories.qr_code import QRCodeRepository  # noqa: E402
from app.db.session import get_engine, get_sessionmaker  # noqa: E402
from app.domain.qr import QR, QRBatch, QRStatus  # noqa: E402
from app.main import app  # noqa: E402
from app.netbox.client import get_netbox_client  # noqa: E402
from tests.integration.conftest import seed_default_active_shift  # noqa: E402

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_USER_SUB = "11111111-1111-1111-1111-111111111111"
_QR_ID = "DCQR-PERF0001"
_DEVICE_ID = 5
_VERSION = "2026-05-21T08:00:00.000000Z"
_NEW_VERSION = "2026-05-21T09:00:00.000000Z"
_NOW = datetime(2026, 6, 3, 9, 0, 0, tzinfo=UTC)
_ITERATIONS = 100


def _alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=_BACKEND_DIR,
        timeout=30,
    )
    if result.returncode != 0:
        sys.stderr.write(
            f"alembic {args!r} failed:\nstdout={result.stdout}\nstderr={result.stderr}\n"
        )
        sys.exit(1)


def _device_dict(version: str = _VERSION) -> dict[str, object]:
    return {
        "id": _DEVICE_ID,
        "name": "sw-01",
        "status": {"value": "active", "label": "Active"},
        "site": {"id": 1, "name": "DC-1"},
        "rack": {"id": 7, "name": "R-14"},
        "position": 42,
        "serial": "ABC123",
        "comments": "",
        "custom_fields": {"asset_tag": "A-9", "qr_id": _QR_ID},
        "last_updated": version,
    }


async def _seed_bound_qr() -> None:
    """Seed a single BOUND QR so the QR-lookup endpoint has something to read."""
    batch_id = uuid4()
    batch = QRBatch(
        id=batch_id,
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID(_USER_SUB),
        count=1,
        intended_site_id=1,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    qr = QR(
        id=_QR_ID,
        batch_id=batch_id,
        status=QRStatus.BOUND,
        bound_to_device_id=_DEVICE_ID,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()


def _percentile(samples: list[float], p: float) -> float:
    """Linear-interpolation percentile (statistics.quantiles is close enough)."""
    quantiles = statistics.quantiles(samples, n=100, method="inclusive")
    return quantiles[int(p) - 1]


def _report(label: str, samples_ms: list[float], target_ms: float) -> None:
    p50 = _percentile(samples_ms, 50)
    p95 = _percentile(samples_ms, 95)
    p99 = _percentile(samples_ms, 99)
    mx = max(samples_ms)
    status = "OK" if p95 <= target_ms else "OVER"
    print(
        f"{label}: p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms "
        f"max={mx:.1f}ms (target ≤ {target_ms:.0f}ms p95) [{status}]"
    )


async def _measure_qr_lookup(client: httpx.AsyncClient) -> list[float]:
    samples: list[float] = []
    netbox_base = str(get_settings().netbox_url).rstrip("/")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{netbox_base}/api/dcim/devices/{_DEVICE_ID}/").respond(
            json=_device_dict()
        )
        for _ in range(_ITERATIONS):
            t0 = time.perf_counter()
            resp = await client.get(f"/api/v1/qr/{_QR_ID}")
            samples.append((time.perf_counter() - t0) * 1000.0)
            if resp.status_code != 200:
                sys.stderr.write(f"qr lookup non-200: {resp.status_code} {resp.text}\n")
                sys.exit(1)
    return samples


async def _measure_device_update(client: httpx.AsyncClient) -> list[float]:
    samples: list[float] = []
    netbox_base = str(get_settings().netbox_url).rstrip("/")
    device_path = f"/api/dcim/devices/{_DEVICE_ID}/"
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{netbox_base}{device_path}").respond(json=_device_dict())
        router.patch(f"{netbox_base}{device_path}").respond(
            json=_device_dict(_NEW_VERSION)
        )
        router.post(f"{netbox_base}/api/extras/journal-entries/").respond(
            status_code=201, json={"id": 1}
        )
        for _ in range(_ITERATIONS):
            t0 = time.perf_counter()
            resp = await client.patch(
                f"/api/v1/devices/{_DEVICE_ID}",
                json={"name": "sw-01-new"},
                headers={"If-Unmodified-Since": _VERSION},
            )
            samples.append((time.perf_counter() - t0) * 1000.0)
            if resp.status_code != 200:
                sys.stderr.write(
                    f"device update non-200: {resp.status_code} {resp.text}\n"
                )
                sys.exit(1)
    return samples


async def main() -> None:
    print(f"Sprint 8a Task 4 — performance baseline ({_ITERATIONS} iterations each)")
    print(
        f"Conditions: in-process ASGI + test Postgres + respx-mocked NetBox + "
        f"single asyncio loop. Started {datetime.now(UTC).isoformat()}."
    )
    print()

    # Bootstrap schema + seed data.
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    async with get_sessionmaker()() as session:
        await seed_default_active_shift(session)
        await session.commit()
    await _seed_bound_qr()

    # Override auth so requests reach the routes without a real JWT.
    mobile_user = AuthUser(
        sub=_USER_SUB,
        email="alice@example.com",
        roles=("dcinv-mobile-user",),
        session_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: mobile_user

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Warm-up — first request pays cold-import + connection-pool costs.
        await client.get(f"/api/v1/qr/{_QR_ID}")

        qr_samples = await _measure_qr_lookup(client)
        device_samples = await _measure_device_update(client)

    _report("GET  /api/v1/qr/{qr_id}    ", qr_samples, target_ms=800.0)
    _report("PATCH /api/v1/devices/{id} ", device_samples, target_ms=1500.0)

    # Cleanup
    app.dependency_overrides.clear()
    await get_netbox_client().aclose()
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
