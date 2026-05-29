"""Unit tests for app.services.cache.TTLCache."""

from __future__ import annotations

import pytest

from app.services.cache import TTLCache


class _Clock:
    """Manually-advanced monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Fetcher:
    """Async fetch callable that records how many times it ran."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    async def __call__(self) -> object:
        self.calls += 1
        return self.value


class _FailOnceFetcher:
    """Raises on the first call, succeeds afterwards."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    async def __call__(self) -> object:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("fetch failed")
        return self.value


async def test_get_or_fetch_returns_fetched_value_on_miss() -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    fetcher = _Fetcher(["a", "b"])

    result = await cache.get_or_fetch("sites", fetcher)

    assert result == ["a", "b"]
    assert fetcher.calls == 1


async def test_get_or_fetch_serves_cached_value_within_ttl() -> None:
    clock = _Clock()
    cache: TTLCache = TTLCache(ttl_seconds=300, clock=clock)
    fetcher = _Fetcher(["a"])

    await cache.get_or_fetch("sites", fetcher)
    clock.now += 299  # still inside the 300s window
    result = await cache.get_or_fetch("sites", fetcher)

    assert result == ["a"]
    assert fetcher.calls == 1  # served from cache — NetBox not hit again


async def test_get_or_fetch_refetches_after_ttl_expiry() -> None:
    clock = _Clock()
    cache: TTLCache = TTLCache(ttl_seconds=300, clock=clock)
    fetcher = _Fetcher(["a"])

    await cache.get_or_fetch("sites", fetcher)
    clock.now += 301
    await cache.get_or_fetch("sites", fetcher)

    assert fetcher.calls == 2


async def test_get_or_fetch_refetches_exactly_at_ttl_boundary() -> None:
    clock = _Clock()
    cache: TTLCache = TTLCache(ttl_seconds=300, clock=clock)
    fetcher = _Fetcher(["a"])

    await cache.get_or_fetch("sites", fetcher)
    clock.now += 300  # age == ttl is treated as expired
    await cache.get_or_fetch("sites", fetcher)

    assert fetcher.calls == 2


async def test_get_or_fetch_isolates_distinct_keys() -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    sites = _Fetcher(["site"])
    racks = _Fetcher(["rack"])

    assert await cache.get_or_fetch("sites", sites) == ["site"]
    assert await cache.get_or_fetch("racks", racks) == ["rack"]
    assert sites.calls == 1
    assert racks.calls == 1


async def test_get_or_fetch_does_not_cache_a_failed_fetch() -> None:
    cache: TTLCache = TTLCache(ttl_seconds=300)
    fetcher = _FailOnceFetcher(["a"])

    with pytest.raises(RuntimeError, match="fetch failed"):
        await cache.get_or_fetch("sites", fetcher)

    # The failure was not stored — the retry refetches and succeeds.
    result = await cache.get_or_fetch("sites", fetcher)
    assert result == ["a"]
    assert fetcher.calls == 2
