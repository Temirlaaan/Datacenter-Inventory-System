"""Application settings. Loaded from env vars and (optionally) /run/secrets at startup."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, PostgresDsn, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REALM_NAME_RE = re.compile(r"[A-Za-z0-9_\-]+")


def _resolve_secrets_dir() -> str | None:
    """Return /run/secrets (or DCINV_SECRETS_DIR override) iff it exists, else None.

    pydantic-settings warns if secrets_dir points at a missing path, which is noisy in
    tests and local dev where the dir doesn't exist. We resolve dynamically so the
    setting is only active in containers where the secrets are actually mounted.
    """
    candidate = os.environ.get("DCINV_SECRETS_DIR", "/run/secrets")
    return candidate if os.path.isdir(candidate) else None


class Settings(BaseSettings):
    """Backend settings. Missing required fields fail fast at startup."""

    netbox_url: HttpUrl
    netbox_service_token: SecretStr
    keycloak_base_url: HttpUrl
    keycloak_realm: str = "prod-v1"
    database_url: PostgresDsn
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    jwks_cache_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    shift_auto_end_enabled: bool = True
    shift_auto_end_interval_seconds: int = Field(default=300, ge=1, le=86400)
    shift_auto_end_threshold_hours: int = Field(default=12, ge=1, le=168)
    netbox_circuit_enabled: bool = True
    netbox_circuit_failure_threshold: int = Field(default=5, ge=1, le=1000)
    netbox_circuit_recovery_timeout_seconds: int = Field(default=30, ge=1, le=3600)
    rate_limit_enabled: bool = True
    rate_limit_read_per_minute: int = Field(default=60, ge=1, le=100000)
    rate_limit_write_per_minute: int = Field(default=20, ge=1, le=100000)
    rate_limit_admin_per_minute: int = Field(default=30, ge=1, le=100000)
    keycloak_web_client_id: str = "dcinv-web"
    keycloak_web_client_secret: SecretStr
    session_cookie_key: SecretStr

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
        secrets_dir=_resolve_secrets_dir(),
    )

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, v: PostgresDsn) -> PostgresDsn:
        # SQLAlchemy 2.0 async (CLAUDE.md stack constraint) requires the asyncpg driver.
        if v.scheme != "postgresql+asyncpg":
            raise ValueError(
                f"DATABASE_URL must use postgresql+asyncpg:// driver "
                f"(SQLAlchemy 2.0 async requires it); got scheme={v.scheme!r}"
            )
        return v

    @field_validator("keycloak_realm")
    @classmethod
    def _validate_realm(cls, v: str) -> str:
        # Realm name flows into keycloak_issuer / jwks_url. Reject anything that
        # would produce a malformed URL — fail fast at startup, not at first JWKS fetch.
        if not _REALM_NAME_RE.fullmatch(v):
            raise ValueError(
                f"KEYCLOAK_REALM must match [A-Za-z0-9_-]+ "
                f"(Keycloak realm naming rules); got {v!r}"
            )
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def keycloak_issuer(self) -> str:
        """OIDC issuer URL = {base}/realms/{realm}. Matches `iss` claim in JWTs."""
        base = str(self.keycloak_base_url).rstrip("/")
        return f"{base}/realms/{self.keycloak_realm}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jwks_url(self) -> str:
        """JWKS endpoint for JWT signature verification."""
        return f"{self.keycloak_issuer}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    """Cached factory used as a FastAPI dependency. Clear via .cache_clear() in tests."""
    return Settings()
