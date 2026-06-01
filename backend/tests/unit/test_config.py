"""Unit tests for app.config.Settings — required fields, validation, computed fields, secrets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _set_all_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "test-token-xyz")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


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
    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings()
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
