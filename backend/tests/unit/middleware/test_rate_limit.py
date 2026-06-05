"""Unit tests for app.middleware.rate_limit (Sprint 8a Task 3, ToR §5.4.7).

Covers classification, _consume budget bookkeeping, _extract_user_sub JWT
parsing, and window rollover. Full-stack integration (middleware → 429 wire
shape) lives in tests/integration/test_rate_limit.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from app.middleware.rate_limit import (
    RateLimitClass,
    _classify_request,
    _consume,
    _current_window_index,
    _extract_user_sub,
    _limit_for_class,
    _seconds_until_next_window,
    reset_rate_limit_buckets,
)

# ---------- _classify_request ------------------------------------------------


@pytest.mark.parametrize(
    "method,path,expected",
    [
        # /admin/* is ADMIN regardless of method (decision 2)
        ("GET", "/api/v1/admin/audit", RateLimitClass.ADMIN),
        ("POST", "/api/v1/admin/batches/", RateLimitClass.ADMIN),
        ("POST", "/api/v1/admin/sessions/start", RateLimitClass.ADMIN),
        (
            "POST",
            "/api/v1/admin/sessions/abc/force-close",
            RateLimitClass.ADMIN,
        ),
        # Non-/admin/ reads
        ("GET", "/api/v1/devices/5", RateLimitClass.READ),
        ("GET", "/api/v1/meta/sites", RateLimitClass.READ),
        ("GET", "/api/v1/qr/DCQR-0001", RateLimitClass.READ),
        ("HEAD", "/api/v1/devices/5", RateLimitClass.READ),
        ("OPTIONS", "/api/v1/devices/5", RateLimitClass.READ),
        # Non-/admin/ writes
        ("POST", "/api/v1/qr/DCQR-0001/bind", RateLimitClass.WRITE),
        ("POST", "/api/v1/qr/DCQR-0001/retire", RateLimitClass.WRITE),
        ("PATCH", "/api/v1/devices/5", RateLimitClass.WRITE),
        ("POST", "/api/v1/devices/", RateLimitClass.WRITE),
        ("POST", "/api/v1/devices/5/comments", RateLimitClass.WRITE),
        ("POST", "/api/v1/devices/5/decommission", RateLimitClass.WRITE),
        # Mobile-driven /sessions/* — WRITE (decision 10)
        ("POST", "/api/v1/sessions/start", RateLimitClass.WRITE),
        ("POST", "/api/v1/sessions/end", RateLimitClass.WRITE),
        ("GET", "/api/v1/sessions/active", RateLimitClass.READ),
        # UNLIMITED paths
        ("GET", "/", RateLimitClass.UNLIMITED),
        ("GET", "/health", RateLimitClass.UNLIMITED),
        ("GET", "/docs", RateLimitClass.UNLIMITED),
        ("GET", "/openapi.json", RateLimitClass.UNLIMITED),
        ("GET", "/redoc", RateLimitClass.UNLIMITED),
        # Sprint 8b Task 0 decision I: /web/* + /static/* bypass rate limiting.
        ("GET", "/web/", RateLimitClass.UNLIMITED),
        ("GET", "/web/login", RateLimitClass.UNLIMITED),
        ("GET", "/web/oidc/callback", RateLimitClass.UNLIMITED),
        ("GET", "/web/batches/", RateLimitClass.UNLIMITED),
        ("POST", "/web/sessions/123/force-close", RateLimitClass.UNLIMITED),
        ("GET", "/static/admin.css", RateLimitClass.UNLIMITED),
    ],
)
def test_classify_request_routes_to_correct_class(
    method: str, path: str, expected: RateLimitClass
) -> None:
    assert _classify_request(method, path) is expected


# ---------- _extract_user_sub -----------------------------------------------


_SECRET = "test-secret-not-used-for-verification"


def _make_token(*, sub: str) -> str:
    """Build a valid-shape JWT — signature isn't verified by the middleware."""
    token: str = jwt.encode({"sub": sub, "exp": 9999999999}, _SECRET, algorithm="HS256")
    return token


def test_extract_user_sub_returns_sub_from_valid_bearer_jwt() -> None:
    sub = "11111111-1111-1111-1111-111111111111"
    token = _make_token(sub=sub)
    assert _extract_user_sub(f"Bearer {token}") == sub


def test_extract_user_sub_returns_none_for_missing_header() -> None:
    assert _extract_user_sub(None) is None


def test_extract_user_sub_returns_none_for_empty_header() -> None:
    assert _extract_user_sub("") is None


def test_extract_user_sub_returns_none_for_non_bearer_scheme() -> None:
    """Basic auth / other schemes don't unlock the rate-limit key."""
    assert _extract_user_sub("Basic dXNlcjpwYXNz") is None


def test_extract_user_sub_returns_none_for_malformed_jwt() -> None:
    assert _extract_user_sub("Bearer not-a-jwt-at-all") is None


def test_extract_user_sub_returns_none_when_sub_missing_from_claims() -> None:
    """JWT parses but has no ``sub`` claim — treat as anonymous."""
    token = jwt.encode({"exp": 9999999999}, _SECRET, algorithm="HS256")
    assert _extract_user_sub(f"Bearer {token}") is None


def test_extract_user_sub_returns_none_when_sub_is_not_a_string() -> None:
    """Defensive: ``sub`` must be a string per OIDC spec; reject anything else."""
    token = jwt.encode({"sub": 12345, "exp": 9999999999}, _SECRET, algorithm="HS256")
    assert _extract_user_sub(f"Bearer {token}") is None


# ---------- _consume -----------------------------------------------


_NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)


def test_consume_allows_first_request_within_budget() -> None:
    reset_rate_limit_buckets()
    allowed, retry = _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)
    assert allowed is True
    assert retry == 0


def test_consume_allows_exactly_limit_requests_then_rejects() -> None:
    reset_rate_limit_buckets()
    for _ in range(3):
        allowed, _ = _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)
        assert allowed is True
    # Fourth call → rejected; retry-after is seconds left in the window
    allowed, retry = _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)
    assert allowed is False
    assert retry > 0


def test_consume_separate_classes_have_separate_budgets() -> None:
    """Same user, same window — READ budget exhaustion doesn't block WRITE."""
    reset_rate_limit_buckets()
    for _ in range(3):
        assert _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)[0]
    assert _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)[0] is False
    # WRITE still allowed
    assert _consume(sub="alice", cls=RateLimitClass.WRITE, limit=3, now=_NOW)[0]


def test_consume_separate_users_have_separate_budgets() -> None:
    reset_rate_limit_buckets()
    for _ in range(3):
        assert _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)[0]
    # alice exhausted
    assert _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)[0] is False
    # bob unaffected
    assert _consume(sub="bob", cls=RateLimitClass.READ, limit=3, now=_NOW)[0]


def test_consume_window_rollover_resets_count() -> None:
    """A request in the NEXT minute lands in a new bucket — fresh budget."""
    reset_rate_limit_buckets()
    for _ in range(3):
        assert _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)[0]
    assert _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)[0] is False

    next_minute = _NOW + timedelta(seconds=60)
    allowed, _ = _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=next_minute)
    assert allowed is True


def test_consume_rejected_request_does_not_increment_count() -> None:
    """Rejected requests must NOT count — a client respecting Retry-After
    shouldn't get a bigger backlog by hammering."""
    reset_rate_limit_buckets()
    for _ in range(3):
        _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)
    # 10 rejected attempts
    for _ in range(10):
        _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=_NOW)
    # At the start of the next window, the budget is full again.
    next_minute = _NOW + timedelta(seconds=60)
    for _ in range(3):
        assert _consume(sub="alice", cls=RateLimitClass.READ, limit=3, now=next_minute)[0]


# ---------- window helpers ---------------------------------------------------


def test_current_window_index_is_stable_within_one_minute() -> None:
    a = _current_window_index(_NOW)
    b = _current_window_index(_NOW + timedelta(seconds=59))
    assert a == b


def test_current_window_index_rolls_at_minute_boundary() -> None:
    a = _current_window_index(_NOW)
    b = _current_window_index(_NOW + timedelta(seconds=60))
    assert b == a + 1


def test_seconds_until_next_window_at_window_start_is_60() -> None:
    # At t = X * 60s exactly, the full 60s budget of the new window is ahead.
    aligned = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
    assert _seconds_until_next_window(aligned) == 60


def test_seconds_until_next_window_decreases_within_window() -> None:
    aligned = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
    assert _seconds_until_next_window(aligned + timedelta(seconds=15)) == 45
    assert _seconds_until_next_window(aligned + timedelta(seconds=59)) == 1


# ---------- _limit_for_class --------------------------------------------------


def test_limit_for_class_returns_sentinel_for_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UNLIMITED branch should never be reached in normal flow (the
    middleware early-returns on UNLIMITED), but the function must remain
    total over the enum. The sentinel is a large-enough number that an
    accidental caller won't trip a real budget."""
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test"
    )
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "test-web-client-secret")
    monkeypatch.setenv("SESSION_COOKIE_KEY", "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo=")
    from app.config import get_settings

    get_settings.cache_clear()
    assert _limit_for_class(RateLimitClass.UNLIMITED) >= 1 << 30
