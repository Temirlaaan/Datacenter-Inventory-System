"""Unit tests for app.api.v1._helpers (Sprint 7 Task 5)."""

from __future__ import annotations

import json

from app.api.v1._helpers import netbox_validation_error_response
from app.netbox.errors import NetBoxValidationError


def test_netbox_validation_error_response_uses_str_detail_as_message() -> None:
    """When NetBox returns a plain text body (e.g. HTML 403), surface it
    verbatim in ``message`` for a single-line mobile toast."""
    resp = netbox_validation_error_response(
        NetBoxValidationError(status_code=403, detail="Forbidden")
    )
    assert resp.status_code == 422
    body = json.loads(bytes(resp.body))
    assert body == {
        "error": {
            "code": "NETBOX_VALIDATION_ERROR",
            "message": "Forbidden",
            "netbox_status": 403,
            "netbox_detail": "Forbidden",
        }
    }


def test_netbox_validation_error_response_uses_fallback_when_detail_not_str() -> None:
    """JSON-dict detail (NetBox's per-field errors) is surfaced in
    ``netbox_detail`` for structured display; ``message`` carries the
    caller-provided fallback so a single-line toast stays meaningful."""
    netbox_body = {"name": ["device with this name already exists."]}
    resp = netbox_validation_error_response(
        NetBoxValidationError(status_code=400, detail=netbox_body),
        fallback_message="NetBox rejected the create request",
    )
    body = json.loads(bytes(resp.body))
    assert body["error"]["code"] == "NETBOX_VALIDATION_ERROR"
    assert body["error"]["message"] == "NetBox rejected the create request"
    assert body["error"]["netbox_status"] == 400
    assert body["error"]["netbox_detail"] == netbox_body


def test_netbox_validation_error_response_default_fallback_message() -> None:
    """No fallback_message provided → defaults to 'NetBox rejected the request'."""
    resp = netbox_validation_error_response(
        NetBoxValidationError(status_code=400, detail={"field": ["bad"]})
    )
    body = json.loads(bytes(resp.body))
    assert body["error"]["message"] == "NetBox rejected the request"
