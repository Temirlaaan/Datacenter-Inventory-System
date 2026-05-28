"""Endpoint tests for POST /api/v1/devices/{id}/comments — Sprint 5 Task 3.

Handler logic by direct ``await``; ``AsyncClient`` proves routing, role-gating,
422 on validation failures, and the NetBox-error → HTTP mapping. The endpoint
touches no database — ``CommentService`` is stubbed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import httpx

from app.api.v1.devices import (
    AddCommentRequest,
    AddCommentResponse,
    add_comment,
    get_comment_service,
)
from app.auth.dependencies import AuthUser
from app.main import app
from app.netbox.errors import NetBoxNotFound, NetBoxServerError
from app.services.comment import CommentService


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        sub="11111111-1111-1111-1111-111111111111",
        email="alice@example.com",
        roles=roles,
        session_id=None,
    )


class _StubCommentService:
    """Stand-in for CommentService — returns a canned journal entry or raises."""

    def __init__(
        self,
        *,
        created: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._created = created or {"id": 42}
        self._error = error
        self.last_kwargs: dict[str, Any] | None = None

    async def add_comment(self, *, device_id: int, comment: str, user: AuthUser) -> dict[str, Any]:
        self.last_kwargs = {"device_id": device_id, "comment": comment, "user": user}
        if self._error is not None:
            raise self._error
        return self._created


# ---------- handler logic (direct await) ----------


async def test_add_comment_handler_returns_201_with_journal_id() -> None:
    stub = _StubCommentService(created={"id": 42, "comments": "test"})
    result = await add_comment(
        device_id=5,
        request=AddCommentRequest(comment="Replaced PSU 1"),
        user=_user("dcinv-mobile-user"),
        comment_service=cast(CommentService, stub),
    )
    assert isinstance(result, AddCommentResponse)
    assert result.id == 42


async def test_add_comment_handler_passes_device_id_comment_user_through() -> None:
    stub = _StubCommentService()
    await add_comment(
        device_id=5,
        request=AddCommentRequest(comment="Replaced PSU 1"),
        user=_user("dcinv-mobile-user"),
        comment_service=cast(CommentService, stub),
    )
    assert stub.last_kwargs is not None
    assert stub.last_kwargs["device_id"] == 5
    assert stub.last_kwargs["comment"] == "Replaced PSU 1"


# ---------- routing / role / validation (AsyncClient) ----------


async def test_post_add_comment_endpoint_returns_201_with_id_only(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService(
        created={"id": 42, "comments": "Replaced PSU 1"}
    )

    resp = await client.post("/api/v1/devices/5/comments", json={"comment": "Replaced PSU 1"})

    assert resp.status_code == 201
    assert resp.json() == {"id": 42}


async def test_post_add_comment_endpoint_returns_422_for_empty_comment(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """min_length=1 — empty comment is not a useful journal entry."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService()

    resp = await client.post("/api/v1/devices/5/comments", json={"comment": ""})

    assert resp.status_code == 422


async def test_post_add_comment_endpoint_returns_422_for_over_length_comment(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Correction 3: max_length=2000."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService()

    resp = await client.post("/api/v1/devices/5/comments", json={"comment": "x" * 2001})

    assert resp.status_code == 422


async def test_post_add_comment_endpoint_accepts_exactly_2000_chars(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService(created={"id": 42})

    resp = await client.post("/api/v1/devices/5/comments", json={"comment": "x" * 2000})

    assert resp.status_code == 201


async def test_post_add_comment_endpoint_returns_422_for_missing_comment_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService()

    resp = await client.post("/api/v1/devices/5/comments", json={})

    assert resp.status_code == 422


async def test_post_add_comment_endpoint_returns_422_for_extra_field(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """extra='forbid' catches typos (e.g. 'text' vs 'comment')."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService()

    resp = await client.post("/api/v1/devices/5/comments", json={"comment": "x", "text": "y"})

    assert resp.status_code == 422


async def test_post_add_comment_endpoint_returns_404_when_device_not_found(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """NetBox returns 404 on the device journal POST → propagates."""
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService(
        error=NetBoxNotFound("device 999 not found")
    )

    resp = await client.post("/api/v1/devices/999/comments", json={"comment": "x"})

    assert resp.status_code == 404


async def test_post_add_comment_endpoint_returns_502_on_netbox_5xx(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService(
        error=NetBoxServerError("POST /api/extras/journal-entries/ → 503"),
    )

    resp = await client.post("/api/v1/devices/5/comments", json={"comment": "x"})

    assert resp.status_code == 502


async def test_post_add_comment_endpoint_returns_403_without_mobile_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")  # admin only, no mobile role
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService()

    resp = await client.post("/api/v1/devices/5/comments", json={"comment": "x"})

    assert resp.status_code == 403


async def test_post_add_comment_endpoint_returns_422_for_non_integer_device_id(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    app.dependency_overrides[get_comment_service] = lambda: _StubCommentService()

    resp = await client.post("/api/v1/devices/not-an-int/comments", json={"comment": "x"})

    assert resp.status_code == 422
