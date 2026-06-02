"""Async HTTP client for NetBox.

`get()` for reads; `patch()` / `post()` for writes (Sprint 3). PATCH only — never
PUT (CLAUDE.md cross-cutting #3). The client stays thin: it returns the raw
`httpx.Response` for the caller to parse and holds no conflict logic. Per Sprint 3
decision A, conflict detection is re-read-and-compare in the service layer, so the
client sends no `If-Unmodified-Since` header to NetBox.

Resilience: 3 attempts with backoff [200ms, 600ms, 1800ms] on 5xx (except 501) and
connection/read timeouts. 4xx and 501 are permanent failures — no retry. Timeouts:
5s for reads, 10s for writes (Architecture §3.3).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from types import TracebackType
from typing import Any, cast

import httpx
import structlog
from circuitbreaker import CircuitBreaker

from app.config import get_settings
from app.netbox.errors import (
    NetBoxCircuitOpenError,
    NetBoxNotFound,
    NetBoxServerError,
    NetBoxTimeout,
    NetBoxValidationError,
)
from app.observability.request_id import current_request_id

_READ_TIMEOUT_SECONDS = 5.0
_WRITE_TIMEOUT_SECONDS = 10.0
_BACKOFF_SECONDS: tuple[float, ...] = (0.2, 0.6, 1.8)
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)

logger = structlog.get_logger()

# Sprint 8a Task 2: process-wide NetBox circuit breaker (Architecture §3.3,
# deferred since Sprint 3). Lazy-initialised so tests that wipe env vars via
# ``clean_env`` can re-set them before the first call.
_netbox_circuit_instance: CircuitBreaker | None = None


def _get_netbox_circuit() -> CircuitBreaker:
    """Build (or return cached) NetBox circuit breaker.

    Only ``NetBoxServerError`` and ``NetBoxTimeout`` count as failures —
    ``NetBoxNotFound`` (404) and ``NetBoxValidationError`` (4xx) are
    "NetBox said your request is wrong," not "NetBox is broken," and must
    not contribute to opening the circuit.
    """
    global _netbox_circuit_instance
    if _netbox_circuit_instance is None:
        settings = get_settings()
        _netbox_circuit_instance = CircuitBreaker(
            failure_threshold=settings.netbox_circuit_failure_threshold,
            recovery_timeout=settings.netbox_circuit_recovery_timeout_seconds,
            expected_exception=(NetBoxServerError, NetBoxTimeout),
            name="netbox",
        )
    return _netbox_circuit_instance


def reset_netbox_circuit() -> None:
    """Clear the cached circuit so the next call re-reads settings.

    Used by the test ``clean_env`` fixture so each test starts with a fresh
    circuit (no failure-count leakage across tests).
    """
    global _netbox_circuit_instance
    _netbox_circuit_instance = None


def get_netbox_circuit_state() -> dict[str, Any]:
    """Snapshot of the circuit's current state for the ``/health`` sub-object."""
    settings = get_settings()
    if not settings.netbox_circuit_enabled:
        return {"enabled": False, "state": "closed", "failure_count": 0, "open_until": None}
    circuit = _get_netbox_circuit()
    return {
        "enabled": True,
        "state": circuit.state,
        "failure_count": circuit.failure_count,
        "open_until": circuit.open_until.isoformat() if circuit.opened else None,
    }


class NetBoxClient:
    """Thin async wrapper around `httpx.AsyncClient`. Owns auth + retry + tracing."""

    def __init__(self, base_url: str, service_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=_READ_TIMEOUT_SECONDS,
            headers={"Authorization": f"Token {service_token}"},
        )

    @classmethod
    def from_settings(cls) -> NetBoxClient:
        settings = get_settings()
        return cls(
            base_url=str(settings.netbox_url),
            service_token=settings.netbox_service_token.get_secret_value(),
        )

    async def __aenter__(self) -> NetBoxClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET `path` with retry. Returns the raw `httpx.Response` for the caller to parse."""
        return await self._send("GET", path, params=params)

    async def options(self, path: str) -> httpx.Response:
        """OPTIONS `path` with retry — used to discover NetBox field choice sets.

        OPTIONS is idempotent and read-only, so it shares `get`'s retry profile
        and 5s read timeout.
        """
        return await self._send("OPTIONS", path)

    async def patch(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
        """PATCH `path` with a JSON body. PATCH only — never PUT (CLAUDE.md #3).

        Returns the raw `httpx.Response`; conflict detection (re-read + compare) is
        the service layer's job, not the client's.
        """
        return await self._send("PATCH", path, json=json, timeout_seconds=_WRITE_TIMEOUT_SECONDS)

    async def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
        """POST `path` with a JSON body — used for NetBox journal entries."""
        return await self._send("POST", path, json=json, timeout_seconds=_WRITE_TIMEOUT_SECONDS)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout_seconds: float = _READ_TIMEOUT_SECONDS,
    ) -> httpx.Response:
        """Public send entry point — applies the circuit breaker around
        ``_send_impl`` (Sprint 8a Task 2). When the circuit is OPEN, raises
        :class:`NetBoxCircuitOpenError` without hitting NetBox; when CLOSED
        or HALF_OPEN, delegates to ``_send_impl`` via ``circuit.call_async``
        so the circuit's failure counter tracks ``NetBoxServerError`` /
        ``NetBoxTimeout`` outcomes.
        """
        settings = get_settings()
        if not settings.netbox_circuit_enabled:
            return await self._send_impl(
                method, path, params=params, json=json, timeout_seconds=timeout_seconds
            )
        circuit = _get_netbox_circuit()
        if circuit.opened:
            raise NetBoxCircuitOpenError(
                recovery_timeout_seconds=settings.netbox_circuit_recovery_timeout_seconds
            )
        # circuitbreaker package ships no type stubs (mypy override above);
        # cast at the boundary so the public method's typed return holds.
        return cast(
            httpx.Response,
            await circuit.call_async(
                self._send_impl,
                method,
                path,
                params=params,
                json=json,
                timeout_seconds=timeout_seconds,
            ),
        )

    async def _send_impl(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout_seconds: float = _READ_TIMEOUT_SECONDS,
    ) -> httpx.Response:
        headers = {"X-Request-ID": current_request_id()}
        last_exc: Exception | None = None
        for attempt in range(len(_BACKOFF_SECONDS)):
            try:
                resp = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout_seconds,
                )
            except _RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                logger.warning(
                    "netbox_request_retryable_error",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=type(e).__name__,
                )
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code == 404:
                    raise NetBoxNotFound(f"{method} {path} → 404")
                if resp.status_code < 500:
                    # Sprint 5 Task 2: parse the 4xx body so callers (e.g.
                    # device create) can surface NetBox's actual error message
                    # as a structured 422. NetBoxValidationError IS-A
                    # NetBoxClientError so existing 502 fallbacks still catch it.
                    try:
                        detail: dict[str, Any] | str = resp.json()
                    except Exception:
                        detail = resp.text or f"HTTP {resp.status_code}"
                    raise NetBoxValidationError(
                        status_code=resp.status_code,
                        detail=detail,
                    )
                if resp.status_code == 501:
                    # 501 Not Implemented is permanent — NetBox will never support
                    # this. Retrying just burns the backoff budget (Architecture §3.3).
                    raise NetBoxServerError(f"{method} {path} → 501 Not Implemented")
                # Other 5xx falls through to retry.
                last_exc = NetBoxServerError(
                    f"{method} {path} → {resp.status_code}: {resp.text[:200]}"
                )
                logger.warning(
                    "netbox_request_5xx",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    status=resp.status_code,
                )

            if attempt < len(_BACKOFF_SECONDS) - 1:
                await asyncio.sleep(_BACKOFF_SECONDS[attempt])

        # Exhausted retries — translate the last failure into a typed error.
        if isinstance(last_exc, NetBoxServerError):
            raise last_exc
        raise NetBoxTimeout(f"{method} {path} failed after retries: {last_exc!r}") from last_exc


@lru_cache
def get_netbox_client() -> NetBoxClient:
    """Process-wide NetBox client. Closed by the FastAPI lifespan handler at shutdown.

    WARNING — event-loop binding: the cached `httpx.AsyncClient` inside is
    implicitly bound to the event loop that first issues a request. In
    production (single uvicorn loop) this is fine. In **tests**, prefer
    `NetBoxClient.from_settings()` inside `async with` so each test owns its
    own client. If you must use this singleton across tests, the `clean_env`
    fixture in `tests/conftest.py` calls `aclose()` and clears the cache
    between tests to keep the binding fresh.
    """
    return NetBoxClient.from_settings()
