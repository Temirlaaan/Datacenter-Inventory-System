"""Unit tests for the request_id middleware in app.main."""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, io.StringIO]]:
    """Set env vars, enter TestClient context, then redirect logging to an in-memory stream."""
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "test-token")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.main import app as fastapi_app
    from app.observability.logging import configure_logging

    with TestClient(fastapi_app) as client:
        # Lifespan has run and configured logging to stdout. Reroute to our capture stream.
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        yield client, stream


def test_request_id_generated_when_header_missing(
    app_with_logs: tuple[TestClient, io.StringIO],
) -> None:
    client, _ = app_with_logs
    response = client.get("/_test")
    assert response.status_code == 200
    rid = response.headers["x-request-id"]
    uuid.UUID(rid)  # raises if not a valid UUID


def test_request_id_propagated_from_header(
    app_with_logs: tuple[TestClient, io.StringIO],
) -> None:
    client, _ = app_with_logs
    response = client.get("/_test", headers={"X-Request-ID": "client-supplied-abc"})
    assert response.headers["x-request-id"] == "client-supplied-abc"


def test_request_id_appears_in_logs(
    app_with_logs: tuple[TestClient, io.StringIO],
) -> None:
    client, stream = app_with_logs
    client.get("/_test", headers={"X-Request-ID": "log-test-xyz"})

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    matching = [entry for entry in parsed if entry.get("request_id") == "log-test-xyz"]
    assert len(matching) >= 2, f"expected route + middleware log; got {parsed}"

    events = {entry["event"] for entry in matching}
    assert "test_route_hit" in events
    assert "request_completed" in events


def test_each_request_gets_fresh_contextvars(
    app_with_logs: tuple[TestClient, io.StringIO],
) -> None:
    client, _ = app_with_logs
    r1 = client.get("/_test")
    r2 = client.get("/_test")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
