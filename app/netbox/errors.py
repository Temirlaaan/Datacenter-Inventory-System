"""Typed exceptions for NetBox client failures. Callers map these to HTTP responses."""

from __future__ import annotations


class NetBoxClientError(Exception):
    """Base for all NetBox client failures."""


class NetBoxNotFound(NetBoxClientError):
    """404 from NetBox — the requested resource doesn't exist. No retry."""


class NetBoxServerError(NetBoxClientError):
    """5xx from NetBox after exhausting retries."""


class NetBoxTimeout(NetBoxClientError):
    """Connection or read timeout after exhausting retries."""
