"""Unit tests for app.services.comment.CommentService — Sprint 5 Task 3."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.auth.dependencies import AuthUser
from app.netbox.errors import NetBoxClientError, NetBoxNotFound
from app.services.comment import CommentService
from app.services.netbox_write import NetBoxWriteService


def _user() -> AuthUser:
    return AuthUser(
        sub="11111111-1111-1111-1111-111111111111",
        email="alice@example.com",
        roles=("dcinv-mobile-user",),
        session_id=None,
    )


class _StubWriteService:
    """Stand-in for NetBoxWriteService — records post_with_attribution calls."""

    def __init__(
        self,
        *,
        created: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._created = created or {"id": 42}
        self._error = error
        self.last_kwargs: dict[str, Any] | None = None

    async def post_with_attribution(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._created


async def test_add_comment_calls_post_with_attribution_with_device_attribution() -> None:
    stub = _StubWriteService(created={"id": 42, "comments": "test"})
    service = CommentService(cast(NetBoxWriteService, stub))

    await service.add_comment(device_id=5, comment="Replaced PSU 1", user=_user())

    assert stub.last_kwargs is not None
    kwargs = stub.last_kwargs
    assert kwargs["netbox_path"] == "/api/extras/journal-entries/"
    assert kwargs["netbox_object_type"] == "dcim.device"
    assert kwargs["netbox_object_id"] == 5
    assert kwargs["entity_type"] == "device"
    assert kwargs["entity_id"] == "5"  # explicit per Task 3 plan
    assert kwargs["operation"] == "device.add_comment"


async def test_add_comment_uses_attach_journal_false() -> None:
    """The POST IS the journal entry — must not write a second one."""
    stub = _StubWriteService()
    service = CommentService(cast(NetBoxWriteService, stub))

    await service.add_comment(device_id=5, comment="x", user=_user())

    assert stub.last_kwargs is not None
    assert stub.last_kwargs["attach_journal"] is False


async def test_add_comment_payload_is_a_journal_entry_for_the_device() -> None:
    stub = _StubWriteService()
    service = CommentService(cast(NetBoxWriteService, stub))

    await service.add_comment(device_id=5, comment="Replaced PSU 1", user=_user())

    assert stub.last_kwargs is not None
    payload = stub.last_kwargs["payload"]
    assert payload["assigned_object_type"] == "dcim.device"
    assert payload["assigned_object_id"] == 5
    assert payload["kind"] == "info"  # hardcoded per decision E
    assert payload["comments"] == "Replaced PSU 1"


async def test_add_comment_returns_created_journal_entry() -> None:
    created = {"id": 42, "comments": "Replaced PSU 1", "created": "2026-05-28T10:00:00Z"}
    stub = _StubWriteService(created=created)
    service = CommentService(cast(NetBoxWriteService, stub))

    result = await service.add_comment(device_id=5, comment="Replaced PSU 1", user=_user())

    assert result == created


async def test_add_comment_propagates_netbox_not_found() -> None:
    """Device gone → NetBoxNotFound; endpoint maps to 404 via global handler."""
    stub = _StubWriteService(error=NetBoxNotFound("device 999 not found"))
    service = CommentService(cast(NetBoxWriteService, stub))

    with pytest.raises(NetBoxNotFound):
        await service.add_comment(device_id=999, comment="x", user=_user())


async def test_add_comment_propagates_netbox_client_error() -> None:
    """Generic NetBox client error propagates → endpoint maps to 502 via global handler."""
    stub = _StubWriteService(error=NetBoxClientError("transport failure"))
    service = CommentService(cast(NetBoxWriteService, stub))

    with pytest.raises(NetBoxClientError):
        await service.add_comment(device_id=5, comment="x", user=_user())
