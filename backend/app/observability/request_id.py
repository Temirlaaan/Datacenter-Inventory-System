"""Shared request-id helper.

Pulls the request_id bound by the FastAPI middleware. Background tasks (health
check fan-out, startup jobs, ad-hoc admin tools) can call this without a
request context — minting a UUID keeps every outgoing call traceable end-to-end
even when there is no inbound HTTP request to inherit one from.
"""

from __future__ import annotations

import uuid

import structlog


def current_request_id() -> str:
    """Return the bound ``request_id`` as a UUID string.

    Mints a fresh UUID4 when the request_id is unbound, non-string, or not a
    parseable UUID. The middleware binds whatever ``X-Request-ID`` the client
    sent — which may be junk — but the audit log types ``request_id`` as UUID,
    so callers that do ``UUID(current_request_id())`` must never see an
    unparseable value.
    """
    rid = structlog.contextvars.get_contextvars().get("request_id")
    if isinstance(rid, str):
        try:
            uuid.UUID(rid)
        except ValueError:
            pass
        else:
            return rid
    return str(uuid.uuid4())
