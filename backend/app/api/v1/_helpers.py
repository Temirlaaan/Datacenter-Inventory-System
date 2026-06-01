"""Shared helpers for /api/v1 endpoints.

``netbox_validation_error_response`` (Sprint 7 Task 5) — translate a
``NetBoxValidationError`` to the structured 422 with
``error.code="NETBOX_VALIDATION_ERROR"`` so the mobile client can show
NetBox's actual rejection message instead of a generic 502.

Per-endpoint catches (not a global ``@app.exception_handler``) keep the
translation explicit. NBV on read endpoints (401/403 against NetBox) is
a backend-token issue, not a user-input issue, and stays on the 502 path
via the global ``NetBoxClientError`` handler.
"""

from __future__ import annotations

from fastapi import status
from fastapi.responses import JSONResponse

from app.netbox.errors import NetBoxValidationError


def netbox_validation_error_response(
    exc: NetBoxValidationError,
    *,
    fallback_message: str = "NetBox rejected the request",
) -> JSONResponse:
    """Build the structured 422 body for a NetBox 4xx.

    ``fallback_message`` is used only when ``exc.detail`` is not a plain
    string — most NetBox 4xx responses parse to a dict (per-field errors) or
    a short text body. The dict case surfaces verbatim in ``netbox_detail``
    so mobile can render it however it likes; ``message`` carries the
    fallback so a single-line error toast stays meaningful.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "NETBOX_VALIDATION_ERROR",
                "message": exc.detail if isinstance(exc.detail, str) else fallback_message,
                "netbox_status": exc.status_code,
                "netbox_detail": exc.detail,
            }
        },
    )
