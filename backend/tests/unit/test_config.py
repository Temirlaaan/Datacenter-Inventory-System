"""Unit tests for app.config.Settings — required fields, validation, computed fields, secrets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _set_all_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "test-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    # Sprint 8b Task 0: web admin OIDC + cookie crypto require these.
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "test-web-client-secret")
    monkeypatch.setenv(
        "SESSION_COOKIE_KEY",
        # A valid Fernet key (44 url-safe base64 chars → 32-byte secret).
        # Generated once for the test suite via Fernet.generate_key(); not
        # security-sensitive (this is a test-only secret).
        "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo=",
    )


def test_settings_load_with_all_required_succeeds(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    from app.config import Settings

    s = Settings()
    assert "netbox.example.com" in str(s.netbox_url)
    assert s.netbox_service_token.get_secret_value() == "test-token-xyz"
    assert "sso.example.com" in str(s.keycloak_base_url)
    assert str(s.database_url).startswith("postgresql+asyncpg://")


def test_settings_missing_netbox_url_raises_validation_error(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "x")
    monkeypatch.setenv("SESSION_COOKIE_KEY", "x" * 44)
    from app.config import Settings

    # _env_file=None bypasses any local .env that would otherwise fill in
    # the deliberately-missing NETBOX_URL and let the test silently pass.
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "netbox_url" in str(exc.value).lower()


def test_settings_missing_database_url_raises_validation_error(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "database_url" in str(exc.value).lower()


def test_settings_invalid_log_level_raises(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "banana")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_defaults_applied(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    from app.config import Settings

    s = Settings()
    assert s.keycloak_realm == "prod-v1"
    assert s.log_level == "INFO"
    assert s.jwks_cache_ttl_seconds == 3600


def test_settings_shift_auto_end_defaults_applied(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprint 7 Task 1 (decision A): three knobs control the auto-end loop.

    The defaults are load-bearing — production runs without overrides — so
    each default has a test that locks it in.
    """
    _set_all_required(monkeypatch)
    from app.config import Settings

    s = Settings()
    assert s.shift_auto_end_enabled is True
    assert s.shift_auto_end_interval_seconds == 300
    assert s.shift_auto_end_threshold_hours == 12


def test_settings_shift_auto_end_overrides_via_env(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators tune interval/threshold without a code change."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("SHIFT_AUTO_END_ENABLED", "false")
    monkeypatch.setenv("SHIFT_AUTO_END_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("SHIFT_AUTO_END_THRESHOLD_HOURS", "24")
    from app.config import Settings

    s = Settings()
    assert s.shift_auto_end_enabled is False
    assert s.shift_auto_end_interval_seconds == 60
    assert s.shift_auto_end_threshold_hours == 24


def test_settings_shift_auto_end_interval_seconds_rejects_zero(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("SHIFT_AUTO_END_INTERVAL_SECONDS", "0")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_shift_auto_end_threshold_hours_rejects_zero(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("SHIFT_AUTO_END_THRESHOLD_HOURS", "0")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_netbox_circuit_defaults_applied(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprint 8a Task 2: three knobs control the NetBox circuit breaker."""
    _set_all_required(monkeypatch)
    from app.config import Settings

    s = Settings()
    assert s.netbox_circuit_enabled is True
    assert s.netbox_circuit_failure_threshold == 5
    assert s.netbox_circuit_recovery_timeout_seconds == 30


def test_settings_netbox_circuit_overrides_via_env(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("NETBOX_CIRCUIT_ENABLED", "false")
    monkeypatch.setenv("NETBOX_CIRCUIT_FAILURE_THRESHOLD", "10")
    monkeypatch.setenv("NETBOX_CIRCUIT_RECOVERY_TIMEOUT_SECONDS", "60")
    from app.config import Settings

    s = Settings()
    assert s.netbox_circuit_enabled is False
    assert s.netbox_circuit_failure_threshold == 10
    assert s.netbox_circuit_recovery_timeout_seconds == 60


def test_settings_netbox_circuit_failure_threshold_rejects_zero(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("NETBOX_CIRCUIT_FAILURE_THRESHOLD", "0")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_netbox_circuit_recovery_timeout_seconds_rejects_zero(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("NETBOX_CIRCUIT_RECOVERY_TIMEOUT_SECONDS", "0")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_rate_limit_defaults_applied(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprint 8a Task 3: four knobs control per-user rate limiting."""
    _set_all_required(monkeypatch)
    from app.config import Settings

    s = Settings()
    assert s.rate_limit_enabled is True
    assert s.rate_limit_read_per_minute == 60
    assert s.rate_limit_write_per_minute == 20
    assert s.rate_limit_admin_per_minute == 30


def test_settings_rate_limit_overrides_via_env(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_READ_PER_MINUTE", "120")
    monkeypatch.setenv("RATE_LIMIT_WRITE_PER_MINUTE", "40")
    monkeypatch.setenv("RATE_LIMIT_ADMIN_PER_MINUTE", "100")
    from app.config import Settings

    s = Settings()
    assert s.rate_limit_enabled is False
    assert s.rate_limit_read_per_minute == 120
    assert s.rate_limit_write_per_minute == 40
    assert s.rate_limit_admin_per_minute == 100


def test_settings_rate_limit_read_per_minute_rejects_zero(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("RATE_LIMIT_READ_PER_MINUTE", "0")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_rate_limit_write_per_minute_rejects_zero(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("RATE_LIMIT_WRITE_PER_MINUTE", "0")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_rate_limit_admin_per_minute_rejects_zero(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("RATE_LIMIT_ADMIN_PER_MINUTE", "0")
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_settings_database_url_requires_asyncpg_driver(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")  # missing +asyncpg driver
    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "asyncpg" in str(exc.value).lower()


def test_settings_repr_does_not_leak_token(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SecretStr must redact in repr() and str(); plain access still works for the NetBox client."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "super-secret-token-12345")
    from app.config import Settings

    s = Settings()
    assert "super-secret-token-12345" not in repr(s)
    assert "super-secret-token-12345" not in str(s)
    # And via model_dump (used by ops tooling)
    assert "super-secret-token-12345" not in str(s.model_dump())
    # But explicit access still works
    assert s.netbox_service_token.get_secret_value() == "super-secret-token-12345"


def test_settings_computed_keycloak_issuer_and_jwks_url(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """computed_field properties should produce OIDC-compliant issuer + JWKS URL."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso-ttc.t-cloud.kz")
    monkeypatch.setenv("KEYCLOAK_REALM", "prod-v1")
    from app.config import Settings

    s = Settings()
    assert s.keycloak_issuer == "https://sso-ttc.t-cloud.kz/realms/prod-v1"
    assert s.jwks_url == "https://sso-ttc.t-cloud.kz/realms/prod-v1/protocol/openid-connect/certs"


def test_settings_empty_realm_raises(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_REALM", "")
    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "realm" in str(exc.value).lower()


def test_settings_realm_with_slash_raises(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_REALM", "prod/v1")
    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "realm" in str(exc.value).lower()


def test_settings_realm_with_whitespace_raises(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_REALM", "prod v1")
    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "realm" in str(exc.value).lower()


def test_settings_web_admin_defaults_applied(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprint 8b Task 0: web admin OIDC + cookie crypto."""
    _set_all_required(monkeypatch)
    from app.config import Settings

    s = Settings()
    assert s.keycloak_web_client_id == "dcinv-web"
    # SecretStr — value not exposed in plain repr, but accessible explicitly.
    assert s.keycloak_web_client_secret.get_secret_value() == "test-web-client-secret"
    assert s.session_cookie_key.get_secret_value()  # non-empty Fernet key


def test_settings_missing_keycloak_web_client_secret_raises(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprint 8b Task 0: confidential client secret has no default — fail-fast."""
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("SESSION_COOKIE_KEY", "dGVzdC1mZXJuZXQta2V5LWZvci10ZXN0LXN1aXRlLW9ubHkx")
    # Sprint 8b Task 0: KEYCLOAK_WEB_CLIENT_SECRET isn't in the clean_env
    # wipe list (most tests need it set); delete it explicitly here to
    # prove the Settings validator rejects its absence.
    monkeypatch.delenv("KEYCLOAK_WEB_CLIENT_SECRET", raising=False)
    from app.config import Settings

    # _env_file=None bypasses any local .env that would otherwise fill in
    # the deliberately-missing secret and let the test silently pass.
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "keycloak_web_client_secret" in str(exc.value).lower()


def test_settings_missing_session_cookie_key_raises(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprint 8b Task 0: Fernet key has no default — fail-fast at startup."""
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "x")
    # Same call as the test above for SESSION_COOKIE_KEY.
    monkeypatch.delenv("SESSION_COOKIE_KEY", raising=False)
    from app.config import Settings

    # _env_file=None bypasses any local .env that would otherwise fill in
    # the deliberately-missing key and let the test silently pass.
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "session_cookie_key" in str(exc.value).lower()


def test_settings_web_admin_overrides_via_env(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_ID", "dcinv-web-staging")
    from app.config import Settings

    s = Settings()
    assert s.keycloak_web_client_id == "dcinv-web-staging"
