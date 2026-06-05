"""Unit tests for app.main top-level handlers (post-Sprint 8b fixes)."""

from __future__ import annotations

import pytest

from app.main import root_redirect


@pytest.mark.asyncio
async def test_root_redirect_307s_to_web() -> None:
    """Bare-hostname convenience: ``GET /`` → 307 ``/web/``.

    Added 2026-06-05 after first-deployment feedback — users hitting the
    bare hostname were seeing FastAPI's default JSON 404 instead of the
    admin surface. Direct-await per the project's endpoint-test rule.
    """
    response = await root_redirect()
    assert response.status_code == 307
    assert response.headers["location"] == "/web/"
