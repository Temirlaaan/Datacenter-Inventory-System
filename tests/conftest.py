"""Shared test fixtures."""

import pytest

_APP_ENV_KEYS = (
    "NETBOX_URL",
    "NETBOX_SERVICE_TOKEN",
    "KEYCLOAK_BASE_URL",
    "KEYCLOAK_REALM",
    "DATABASE_URL",
    "LOG_LEVEL",
    "JWKS_CACHE_TTL_SECONDS",
    "DCINV_SECRETS_DIR",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Function-scoped: clear app env vars and the cached factories before each test."""
    for key in _APP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
