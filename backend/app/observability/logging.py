"""Structured logging configuration.

Configures structlog + stdlib logging so that all log output (app, uvicorn, fastapi,
sqlalchemy) is emitted as one-line JSON to a single stream. request_id and other
per-request fields propagate via contextvars.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

import structlog


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure structlog + stdlib logging for JSON output.

    Idempotent: each call clears existing root-logger handlers, so tests can reconfigure
    freely. The stdlib bridge means uvicorn/FastAPI logs flow through the same pipeline
    as `structlog.get_logger()` calls.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Force uvicorn/fastapi/sqlalchemy to use our handler instead of their own defaults,
    # otherwise their startup/access lines would print as plain text alongside our JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "sqlalchemy"):
        sub = logging.getLogger(name)
        sub.handlers.clear()
        sub.propagate = True
