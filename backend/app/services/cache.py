"""In-process TTL cache for NetBox static lookups.

Sprint 1 caching decision: in-process per-instance memory only, no Redis —
single-DC load (~500 devices, a handful of concurrent users) does not justify a
shared cache or a new dependency.

The cache is deliberately lock-free. Two coroutines that miss the same cold key
concurrently will both call ``fetch``; for static lookups that is a harmless
duplicate read, not a correctness problem, and it keeps the hot path trivial.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

_T = TypeVar("_T")


@dataclass
class _Entry:
    value: Any
    stored_at: float


class TTLCache:
    """Time-to-live cache. One instance is shared process-wide per use case.

    Generic over the cached value type: ``get_or_fetch`` returns whatever its
    ``fetch`` callable produces. The ``clock`` is injectable so TTL expiry can be
    tested deterministically; production uses ``time.monotonic``.
    """

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    async def get_or_fetch(self, key: str, fetch: Callable[[], Awaitable[_T]]) -> _T:
        """Return the cached value for ``key``, or ``await fetch()`` and cache it.

        A fetch that raises is not cached — the next call retries it.
        """
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now - entry.stored_at < self._ttl:
            return cast(_T, entry.value)
        value = await fetch()
        # Sweep expired entries before inserting. For static-lookup caches (a
        # handful of fixed keys) this is a no-op; for caches keyed on free-form
        # input (e.g. device search keyed on the query string) it bounds the
        # dict to entries still inside their TTL instead of growing forever.
        # The sweep rides the cold path (we only reach here on a miss, right
        # after an ``await fetch()`` round-trip), so its O(n) cost is
        # negligible relative to the upstream call it follows.
        self._evict_expired(now)
        self._entries[key] = _Entry(value=value, stored_at=now)
        return value

    def _evict_expired(self, now: float) -> None:
        """Drop every entry whose TTL has elapsed as of ``now``."""
        expired = [
            k for k, e in self._entries.items() if now - e.stored_at >= self._ttl
        ]
        for k in expired:
            del self._entries[k]
