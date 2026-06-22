"""Unit tests for app.auth.jwks — fetch, TTL, kid rotation, network failure, concurrency."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from tests.unit.auth.conftest import JWKS_URL, RSAKeyPair


def _jwks_payload(*keys: RSAKeyPair) -> dict[str, list[dict[str, str]]]:
    return {"keys": [k.public_jwk for k in keys]}


async def test_get_key_returns_jwk_for_known_kid(
    clean_env: None, auth_env: None, test_key: RSAKeyPair
) -> None:
    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)
    with respx.mock(assert_all_called=True) as router:
        router.get(JWKS_URL).respond(json=_jwks_payload(test_key))
        jwk = await cache.get_key(test_key.kid)

    assert jwk is not None
    assert jwk["kid"] == test_key.kid
    assert jwk["kty"] == "RSA"


async def test_get_key_returns_none_when_kid_not_in_jwks(
    clean_env: None, auth_env: None, test_key: RSAKeyPair
) -> None:
    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)
    with respx.mock(assert_all_called=True) as router:
        router.get(JWKS_URL).respond(json=_jwks_payload(test_key))
        jwk = await cache.get_key("nonexistent-kid")

    assert jwk is None


async def test_get_key_caches_response_within_ttl(
    clean_env: None, auth_env: None, test_key: RSAKeyPair
) -> None:
    """Second call for the same kid within TTL must NOT hit the network."""
    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(JWKS_URL).respond(json=_jwks_payload(test_key))
        await cache.get_key(test_key.kid)
        await cache.get_key(test_key.kid)

    assert route.call_count == 1


async def test_get_key_refetches_after_ttl_expiry(
    clean_env: None,
    auth_env: None,
    test_key: RSAKeyPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth import jwks as jwks_mod

    cache = jwks_mod.JWKSCache(jwks_url=JWKS_URL, ttl_seconds=60)
    fake_now = [1000.0]
    monkeypatch.setattr(jwks_mod.time, "monotonic", lambda: fake_now[0])

    with respx.mock(assert_all_called=True) as router:
        route = router.get(JWKS_URL).respond(json=_jwks_payload(test_key))
        await cache.get_key(test_key.kid)
        fake_now[0] += 61  # past TTL
        await cache.get_key(test_key.kid)

    assert route.call_count == 2


async def test_get_key_refetches_when_kid_unknown_in_cache(
    clean_env: None,
    auth_env: None,
    test_key: RSAKeyPair,
    rotated_key: RSAKeyPair,
) -> None:
    """Key rotation: ask for a kid that's not cached → trigger refresh even if TTL not expired."""
    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(JWKS_URL).respond(json=_jwks_payload(test_key))
        await cache.get_key(test_key.kid)
        # Simulate Keycloak rotating to include the new kid.
        route.respond(json=_jwks_payload(test_key, rotated_key))
        jwk = await cache.get_key(rotated_key.kid)

    assert route.call_count == 2
    assert jwk is not None
    assert jwk["kid"] == rotated_key.kid


async def test_get_key_propagates_http_error(clean_env: None, auth_env: None) -> None:
    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)
    with respx.mock() as router:
        router.get(JWKS_URL).respond(status_code=500)
        with pytest.raises(httpx.HTTPStatusError):
            await cache.get_key("any-kid")


async def test_get_key_propagates_connection_error(clean_env: None, auth_env: None) -> None:
    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)
    with respx.mock() as router:
        router.get(JWKS_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(httpx.ConnectError):
            await cache.get_key("any-kid")


async def test_concurrent_get_key_results_in_single_fetch(
    clean_env: None, auth_env: None, test_key: RSAKeyPair
) -> None:
    """Thundering herd at startup: N concurrent get_key calls = 1 JWKS HTTP request.

    The respx side_effect adds an `await asyncio.sleep(0)` before responding so the
    first task yields control while holding the lock — without that, the instant
    response lets each task complete via the fast path before the next is scheduled,
    and the post-lock recheck branch never executes.
    """
    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)

    async def slow_response(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=_jwks_payload(test_key))

    with respx.mock(assert_all_called=True) as router:
        route = router.get(JWKS_URL).mock(side_effect=slow_response)
        results = await asyncio.gather(*(cache.get_key(test_key.kid) for _ in range(10)))

    assert route.call_count == 1
    assert all(r is not None and r["kid"] == test_key.kid for r in results)


def test_get_jwks_cache_is_cached(clean_env: None, auth_env: None) -> None:
    """Module-level singleton: same instance across calls within a process."""
    from app.auth.jwks import get_jwks_cache

    c1 = get_jwks_cache()
    c2 = get_jwks_cache()
    assert c1 is c2


def test_get_jwks_cache_uses_settings(clean_env: None, auth_env: None) -> None:
    from app.auth.jwks import get_jwks_cache

    cache = get_jwks_cache()
    assert "openid-connect/certs" in cache.jwks_url
    assert cache.ttl_seconds == 3600  # default from Settings


async def test_get_key_drops_keys_without_kid_and_logs_warning(
    clean_env: None,
    auth_env: None,
    test_key: RSAKeyPair,
) -> None:
    """A JWKS payload with malformed entries (no `kid`) must be filtered AND logged.

    Silent drop would let signature verification fail mysteriously for tokens that
    happen to reference one of the dropped keys.

    Uses ``structlog.testing.capture_logs`` rather than pytest's ``caplog``:
    the app bridges structlog to stdlib logging via ``configure_logging``, but
    that isn't called in unit tests, so structlog falls back to its default
    ``PrintLogger`` (stdout) which ``caplog`` cannot see. ``capture_logs``
    captures the event dicts directly, independent of structlog config.
    """
    from structlog.testing import capture_logs

    from app.auth.jwks import JWKSCache

    cache = JWKSCache(jwks_url=JWKS_URL, ttl_seconds=3600)
    payload = {"keys": [test_key.public_jwk, {"kty": "RSA", "n": "x", "e": "y"}]}
    with respx.mock(assert_all_called=True) as router:
        router.get(JWKS_URL).respond(json=payload)
        with capture_logs() as logs:
            jwk = await cache.get_key(test_key.kid)

    assert jwk is not None
    assert any(entry.get("event") == "jwks_keys_missing_kid" for entry in logs)
