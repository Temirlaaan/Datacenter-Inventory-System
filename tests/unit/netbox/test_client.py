"""NetBox client tests — request shape, retry policy, error mapping, request_id propagation.

Marked `unit` (not `integration`) because no real NetBox runs; respx fully fakes the wire.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import structlog

NETBOX_URL = "https://netbox.example.com"
SERVICE_TOKEN = "secret-token-xyz"


@pytest.fixture
def netbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip retry sleeps so tests aren't gated on real wall time."""
    from app.netbox import client as client_module

    monkeypatch.setattr(client_module, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


# ---------- happy path & request shape ----------


async def test_get_returns_response_for_200(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/status/").respond(json={"netbox-version": "4.1.0"})
            resp = await client.get("/api/status/")

    assert resp.status_code == 200
    assert resp.json()["netbox-version"] == "4.1.0"


async def test_get_sets_authorization_header(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/status/").respond(json={"netbox-version": "x"})
            await client.get("/api/status/")

    sent = route.calls.last.request
    assert sent.headers["authorization"] == f"Token {SERVICE_TOKEN}"


async def test_get_propagates_request_id_from_contextvars(
    clean_env: None, netbox_env: None
) -> None:
    """X-Request-ID flows from structlog contextvars (set by request middleware)."""
    from app.netbox.client import NetBoxClient

    # current_request_id() returns the bound value only when it is a valid UUID.
    bound_id = "11111111-2222-3333-4444-555555555555"
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=bound_id)
    try:
        async with NetBoxClient.from_settings() as client:
            with respx.mock(assert_all_called=True) as router:
                route = router.get(f"{NETBOX_URL}/api/status/").respond(
                    json={"netbox-version": "x"}
                )
                await client.get("/api/status/")
    finally:
        structlog.contextvars.clear_contextvars()

    assert route.calls.last.request.headers["x-request-id"] == bound_id


async def test_get_generates_request_id_when_contextvars_empty(
    clean_env: None, netbox_env: None
) -> None:
    """No middleware in the call chain (e.g., startup task) → client mints a fresh UUID."""
    from app.netbox.client import NetBoxClient

    structlog.contextvars.clear_contextvars()
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/status/").respond(json={"netbox-version": "x"})
            await client.get("/api/status/")

    sent = route.calls.last.request
    assert "x-request-id" in sent.headers
    assert len(sent.headers["x-request-id"]) > 0


async def test_get_passes_query_params(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/dcim/devices/").respond(json={"results": []})
            await client.get("/api/dcim/devices/", params={"site_id": 1, "limit": 50})

    url = route.calls.last.request.url
    assert url.params["site_id"] == "1"
    assert url.params["limit"] == "50"


# ---------- retry policy ----------


async def test_get_retries_three_times_on_500(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxServerError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/status/").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await client.get("/api/status/")

    assert route.call_count == 3


async def test_get_retries_then_succeeds(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """Transient 500 → second attempt 200. Verifies retry actually re-fires the request."""
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/status/")
            route.side_effect = [
                httpx.Response(500),
                httpx.Response(200, json={"netbox-version": "4.1.0"}),
            ]
            resp = await client.get("/api/status/")

    assert route.call_count == 2
    assert resp.status_code == 200


async def test_get_does_not_retry_on_404(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxNotFound

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/dcim/devices/9999/").respond(status_code=404)
            with pytest.raises(NetBoxNotFound):
                await client.get("/api/dcim/devices/9999/")

    assert route.call_count == 1


async def test_get_does_not_retry_on_400(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """4xx (other than 404) is a client error — retrying just hammers NetBox."""
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxClientError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/dcim/devices/").respond(
                status_code=400, json={"detail": "bad request"}
            )
            with pytest.raises(NetBoxClientError):
                await client.get("/api/dcim/devices/")

    assert route.call_count == 1


async def test_get_retries_on_connect_error(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxTimeout

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/status/").mock(
                side_effect=httpx.ConnectError("conn refused")
            )
            with pytest.raises(NetBoxTimeout):
                await client.get("/api/status/")

    assert route.call_count == 3


async def test_get_retries_on_read_timeout(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxTimeout

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(f"{NETBOX_URL}/api/status/").mock(
                side_effect=httpx.ReadTimeout("slow")
            )
            with pytest.raises(NetBoxTimeout):
                await client.get("/api/status/")

    assert route.call_count == 3


async def test_get_uses_backoff_between_retries(
    clean_env: None, netbox_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the configured backoff sequence is consumed in order."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    from app.netbox import client as client_module

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxServerError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/status/").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await client.get("/api/status/")

    # 3 attempts → 2 sleeps between them.
    assert sleeps == [0.2, 0.6]


# ---------- write methods: patch ----------

_DEVICE_PATH = "/api/dcim/devices/5/"
_JOURNAL_PATH = "/api/extras/journal-entries/"
_WRITE_TIMEOUT = {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


async def test_patch_returns_response_for_200(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json={"id": 5, "name": "sw-01"})
            resp = await client.patch(_DEVICE_PATH, json={"name": "sw-01"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "sw-01"


async def test_patch_sends_json_body(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    payload = {"name": "sw-01", "serial": "ABC123"}
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json={"id": 5})
            await client.patch(_DEVICE_PATH, json=payload)

    assert json.loads(route.calls.last.request.content) == payload


async def test_patch_uses_method_patch(clean_env: None, netbox_env: None) -> None:
    """Guards CLAUDE.md cross-cutting #3: device writes are PATCH, never PUT."""
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json={"id": 5})
            await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.calls.last.request.method == "PATCH"


async def test_patch_uses_write_timeout(clean_env: None, netbox_env: None) -> None:
    """Writes get the 10s budget (Architecture §3.3), not the 5s read budget."""
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json={"id": 5})
            await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.calls.last.request.extensions["timeout"] == _WRITE_TIMEOUT


async def test_patch_retries_three_times_on_500(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxServerError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.call_count == 3


async def test_patch_retries_then_succeeds(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}")
            route.side_effect = [
                httpx.Response(500),
                httpx.Response(200, json={"id": 5}),
            ]
            resp = await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.call_count == 2
    assert resp.status_code == 200


async def test_patch_does_not_retry_on_404(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxNotFound

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=404)
            with pytest.raises(NetBoxNotFound):
                await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.call_count == 1


async def test_patch_does_not_retry_on_400(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """A NetBox validation rejection (4xx) is the caller's fault — no retry."""
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxClientError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(
                status_code=400, json={"detail": "invalid"}
            )
            with pytest.raises(NetBoxClientError):
                await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.call_count == 1


async def test_patch_retries_on_connect_error(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxTimeout

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").mock(
                side_effect=httpx.ConnectError("conn refused")
            )
            with pytest.raises(NetBoxTimeout):
                await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.call_count == 3


async def test_send_does_not_retry_on_501(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """501 Not Implemented is permanent (Architecture §3.3) — fail fast, no retry."""
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxServerError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.patch(f"{NETBOX_URL}{_DEVICE_PATH}").respond(status_code=501)
            with pytest.raises(NetBoxServerError):
                await client.patch(_DEVICE_PATH, json={"name": "x"})

    assert route.call_count == 1


# ---------- write methods: post ----------


async def test_post_returns_response_for_201(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=201, json={"id": 99})
            resp = await client.post(_JOURNAL_PATH, json={"comments": "edited"})

    assert resp.status_code == 201
    assert resp.json()["id"] == 99


async def test_post_sends_json_body(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    payload = {"assigned_object_id": 5, "kind": "info", "comments": "edited"}
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 99}
            )
            await client.post(_JOURNAL_PATH, json=payload)

    assert json.loads(route.calls.last.request.content) == payload


async def test_post_uses_write_timeout(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=201, json={"id": 99}
            )
            await client.post(_JOURNAL_PATH, json={"comments": "x"})

    assert route.calls.last.request.extensions["timeout"] == _WRITE_TIMEOUT


async def test_post_retries_three_times_on_500(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxServerError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(status_code=500)
            with pytest.raises(NetBoxServerError):
                await client.post(_JOURNAL_PATH, json={"comments": "x"})

    assert route.call_count == 3


async def test_post_does_not_retry_on_400(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxClientError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(f"{NETBOX_URL}{_JOURNAL_PATH}").respond(
                status_code=400, json={"detail": "invalid"}
            )
            with pytest.raises(NetBoxClientError):
                await client.post(_JOURNAL_PATH, json={"comments": "x"})

    assert route.call_count == 1


# ---------- options ----------


async def test_options_returns_response_for_200(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.options(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json={"actions": {}})
            resp = await client.options(_DEVICE_PATH)

    assert resp.status_code == 200
    assert resp.json() == {"actions": {}}


async def test_options_uses_method_options(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import NetBoxClient

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.options(f"{NETBOX_URL}{_DEVICE_PATH}").respond(json={})
            await client.options(_DEVICE_PATH)

    assert route.calls.last.request.method == "OPTIONS"


# ---------- module-level dependency ----------


def test_get_netbox_client_returns_singleton(clean_env: None, netbox_env: None) -> None:
    from app.netbox.client import get_netbox_client

    c1 = get_netbox_client()
    c2 = get_netbox_client()
    assert c1 is c2


# ---------- NetBoxValidationError (Sprint 5 Task 2) ----------


async def test_send_raises_netbox_validation_error_on_400_with_json_body(
    clean_env: None, netbox_env: None
) -> None:
    """400 with a JSON body — detail must be the parsed dict so callers can
    surface NetBox's actual error message (Sprint 5 Task 2)."""
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxValidationError

    netbox_body = {"name": ["device with this name already exists."]}
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}/api/dcim/devices/").respond(
                status_code=400, json=netbox_body
            )
            with pytest.raises(NetBoxValidationError) as exc_info:
                await client.post("/api/dcim/devices/", json={"name": "sw-01"})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == netbox_body


async def test_send_raises_netbox_validation_error_on_422_with_dict_body(
    clean_env: None, netbox_env: None
) -> None:
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxValidationError

    netbox_body = {"position": ["U position 42 is already occupied."]}
    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}/api/dcim/devices/").respond(
                status_code=422, json=netbox_body
            )
            with pytest.raises(NetBoxValidationError) as exc_info:
                await client.post("/api/dcim/devices/", json={"position": 42})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == netbox_body


async def test_send_falls_back_to_text_when_4xx_body_is_not_json(
    clean_env: None, netbox_env: None
) -> None:
    """Some NetBox proxies return non-JSON 4xx bodies (e.g. HTML 403 from nginx).
    The detail falls back to the raw text rather than crashing on json parse."""
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxValidationError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{NETBOX_URL}/api/dcim/devices/").respond(
                status_code=403,
                content=b"<html><body>Forbidden</body></html>",
                headers={"content-type": "text/html"},
            )
            with pytest.raises(NetBoxValidationError) as exc_info:
                await client.post("/api/dcim/devices/", json={})

    assert exc_info.value.status_code == 403
    assert isinstance(exc_info.value.detail, str)
    assert "Forbidden" in exc_info.value.detail


async def test_send_404_still_raises_netbox_not_found(clean_env: None, netbox_env: None) -> None:
    """Regression: 404 is special-cased BEFORE the new 4xx branch — must
    still raise NetBoxNotFound (not NetBoxValidationError)."""
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxNotFound

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/999/").respond(status_code=404)
            with pytest.raises(NetBoxNotFound):
                await client.get("/api/dcim/devices/999/")


async def test_send_5xx_still_raises_netbox_server_error(
    clean_env: None, netbox_env: None, fast_backoff: None
) -> None:
    """Regression: 5xx flows through the retry loop and raises NetBoxServerError
    (not NetBoxValidationError) after exhausting retries."""
    from app.netbox.client import NetBoxClient
    from app.netbox.errors import NetBoxServerError

    async with NetBoxClient.from_settings() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{NETBOX_URL}/api/dcim/devices/").respond(status_code=503)
            with pytest.raises(NetBoxServerError):
                await client.get("/api/dcim/devices/")


def test_netbox_validation_error_carries_status_and_detail() -> None:
    """Smoke: the exception class exposes both attributes the endpoint reads."""
    from app.netbox.errors import NetBoxClientError, NetBoxValidationError

    detail = {"name": ["already exists"]}
    err = NetBoxValidationError(status_code=400, detail=detail)
    assert err.status_code == 400
    assert err.detail == detail
    # Subclass relationship — Sprint 5 plan invariant
    assert isinstance(err, NetBoxClientError)
