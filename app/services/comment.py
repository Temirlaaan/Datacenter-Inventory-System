"""Add-comment service. Sprint 5 Task 3, ToR §4.3.6.

``CommentService.add_comment`` appends a free-form journal entry to a NetBox
device. No device PATCH — the journal entry IS the operation. Thin wrapper
over ``NetBoxWriteService.post_with_attribution`` with
``attach_journal=False`` (the POST is the journal; writing a second one
would be redundant noise).

Audit attribution: ``operation="device.add_comment"``, ``entity_type="device"``,
``entity_id=str(device_id)`` — Sprint 5 Task 3 plan + the
``entity_id``-explicit branch of ``post_with_attribution``.
"""

from __future__ import annotations

from typing import Any

from app.auth.dependencies import AuthUser
from app.services.netbox_write import NetBoxWriteService


class CommentService:
    """Posts NetBox journal entries on devices."""

    def __init__(self, write_service: NetBoxWriteService) -> None:
        self._write_service = write_service

    async def add_comment(self, *, device_id: int, comment: str, user: AuthUser) -> dict[str, Any]:
        """Append a comment as a NetBox journal entry on the given device.

        Returns the created journal entry dict. Raises ``NetBoxNotFound`` if
        the device is gone (NetBox rejects with 404 — endpoint maps to 404).
        ``NetBoxValidationError`` for 4xx; other NetBox errors propagate as
        ``NetBoxClientError`` / ``NetBoxServerError`` and flow through the
        global handlers.
        """
        payload = {
            "assigned_object_type": "dcim.device",
            "assigned_object_id": device_id,
            "kind": "info",
            "comments": comment,
        }
        return await self._write_service.post_with_attribution(
            netbox_path="/api/extras/journal-entries/",
            netbox_object_type="dcim.device",
            netbox_object_id=device_id,
            entity_type="device",
            entity_id=str(device_id),
            operation="device.add_comment",
            payload=payload,
            user=user,
            # The POST IS the journal entry — don't write a second one.
            attach_journal=False,
        )
