"""Typed exceptions for NetBox client failures. Callers map these to HTTP responses."""

from __future__ import annotations

from typing import Any


class NetBoxClientError(Exception):
    """Base for all NetBox client failures."""


class NetBoxNotFound(NetBoxClientError):
    """404 from NetBox — the requested resource doesn't exist. No retry."""


class NetBoxValidationError(NetBoxClientError):
    """NetBox rejected the request with a 4xx (other than 404).

    Carries the parsed response body so callers can surface NetBox's actual
    error message to the user (e.g. "device with this name already exists"
    → mobile shows it instead of "bad gateway"). Sprint 5 Task 2 introduced
    this for the device-create UX; broader use lands in Sprint 6+.

    ``detail`` is the parsed JSON body when NetBox returned JSON, or the raw
    text body otherwise.
    """

    def __init__(self, *, status_code: int, detail: dict[str, Any] | str) -> None:
        super().__init__(f"NetBox validation failed: {status_code}")
        self.status_code = status_code
        self.detail = detail


class NetBoxServerError(NetBoxClientError):
    """5xx from NetBox after exhausting retries."""


class NetBoxTimeout(NetBoxClientError):
    """Connection or read timeout after exhausting retries."""


class NetBoxCircuitOpenError(NetBoxClientError):
    """NetBox circuit breaker is OPEN — request rejected without hitting NetBox.

    Raised by :class:`~app.netbox.client.NetBoxClient` when the per-process
    circuit breaker (Architecture §3.3, Sprint 8a Task 2) has tripped due to
    consecutive ``NetBoxServerError`` / ``NetBoxTimeout`` failures and is in
    its OPEN state. The ``main.py`` handler translates this to a 503 +
    structured body + ``Retry-After`` header so clients know to back off.

    Distinguished from ``NetBoxServerError`` (which means "NetBox responded
    with 5xx") by the fact that no NetBox call was made — the backend is
    fast-failing on its own.
    """

    def __init__(self, *, recovery_timeout_seconds: int) -> None:
        super().__init__(
            f"NetBox circuit is open; reject without upstream call "
            f"(recovery in ~{recovery_timeout_seconds}s)"
        )
        self.recovery_timeout_seconds = recovery_timeout_seconds
