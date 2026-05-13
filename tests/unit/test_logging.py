"""Unit tests for app.observability.logging — JSON output, contextvars, level filtering."""

from __future__ import annotations

import io
import json

import structlog


def _parse_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_configure_logging_emits_valid_json() -> None:
    from app.observability.logging import configure_logging

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    structlog.contextvars.clear_contextvars()
    structlog.get_logger().info("hello_event", foo="bar", n=42)

    parsed = _parse_lines(stream)
    assert len(parsed) == 1
    entry = parsed[0]
    assert entry["event"] == "hello_event"
    assert entry["foo"] == "bar"
    assert entry["n"] == 42
    assert entry["level"] == "info"
    assert "timestamp" in entry


def test_logging_includes_request_id_when_bound() -> None:
    from app.observability.logging import configure_logging

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="abc-123", user="jane@corp")
    structlog.get_logger().info("event_x")

    parsed = _parse_lines(stream)
    assert len(parsed) == 1
    assert parsed[0]["request_id"] == "abc-123"
    assert parsed[0]["user"] == "jane@corp"


def test_logging_omits_request_id_when_not_bound() -> None:
    from app.observability.logging import configure_logging

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    structlog.contextvars.clear_contextvars()
    structlog.get_logger().info("event_y")

    parsed = _parse_lines(stream)
    assert len(parsed) == 1
    assert "request_id" not in parsed[0]


def test_logging_respects_log_level() -> None:
    from app.observability.logging import configure_logging

    stream = io.StringIO()
    configure_logging("WARNING", stream=stream)
    structlog.contextvars.clear_contextvars()
    logger = structlog.get_logger()
    logger.info("should_be_filtered_out")
    logger.warning("should_appear")

    parsed = _parse_lines(stream)
    events = [entry["event"] for entry in parsed]
    assert "should_be_filtered_out" not in events
    assert "should_appear" in events
