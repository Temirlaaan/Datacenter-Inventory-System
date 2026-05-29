"""JWKS cache for verifying Keycloak JWTs.

Lazy fetch on first use, TTL-bounded (default 1h). On a `kid` miss we refresh once
in case Keycloak rotated keys mid-cycle. An asyncio.Lock around the fetch prevents
a thundering herd at TTL expiry.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import structlog

from app.config import get_settings

_HTTP_TIMEOUT_SECONDS = 5.0

logger = structlog.get_logger()


@dataclass
class _CacheEntry:
    keys_by_kid: dict[str, dict[str, Any]]
    fetched_at: float


class JWKSCache:
    """Cache of JWKS keys keyed by `kid`. Use `get_key(kid)` to retrieve a JWK dict."""

    def __init__(self, jwks_url: str, ttl_seconds: int) -> None:
        self.jwks_url = jwks_url
        self.ttl_seconds = ttl_seconds
        self._entry: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> dict[str, Any] | None:
        """Return the JWK dict for `kid`, refreshing the cache if needed."""
        if self._has_kid_unexpired(kid):
            entry = self._require_entry()
            return entry.keys_by_kid[kid]

        async with self._lock:
            # Re-check after acquiring the lock — another task may have just refreshed.
            if self._has_kid_unexpired(kid):
                entry = self._require_entry()
                return entry.keys_by_kid[kid]
            await self._fetch()

        # _fetch either sets self._entry or raises; no None case to handle here.
        entry = self._require_entry()
        return entry.keys_by_kid.get(kid)

    def _require_entry(self) -> _CacheEntry:
        """Narrow `_entry` to non-None with a real raise, not assert.

        Asserts disappear under `python -O`; in production we want a loud
        crash if this invariant is ever violated rather than a NoneType
        AttributeError two frames deeper.
        """
        if self._entry is None:  # pragma: no cover — invariant guard, not a behavior path
            raise RuntimeError("JWKSCache invariant violated: _entry should be set here")
        return self._entry

    def _has_kid_unexpired(self, kid: str) -> bool:
        if self._entry is None:
            return False
        if (time.monotonic() - self._entry.fetched_at) >= self.ttl_seconds:
            return False
        return kid in self._entry.keys_by_kid

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(self.jwks_url)
            resp.raise_for_status()
        keys = resp.json().get("keys", [])
        by_kid: dict[str, dict[str, Any]] = {}
        dropped = 0
        for key in keys:
            kid = key.get("kid")
            if kid:
                by_kid[kid] = key
            else:
                dropped += 1
        if dropped:
            # A misconfigured Keycloak realm can serve keys without kid; without this
            # signal, signature verification just silently fails for affected tokens.
            logger.warning("jwks_keys_missing_kid", dropped=dropped, jwks_url=self.jwks_url)
        self._entry = _CacheEntry(keys_by_kid=by_kid, fetched_at=time.monotonic())


@lru_cache
def get_jwks_cache() -> JWKSCache:
    """Process-wide JWKS cache bound to the configured Keycloak realm."""
    settings = get_settings()
    return JWKSCache(
        jwks_url=settings.jwks_url,
        ttl_seconds=settings.jwks_cache_ttl_seconds,
    )
