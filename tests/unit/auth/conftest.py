"""Auth test fixtures: RSA keypairs and a JWT signer that mimics Keycloak.

Tests sign tokens locally with an RSA private key and serve the matching public JWK
through respx — so JWKS fetches succeed without ever touching real Keycloak.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from jose import jwt

# Settings the auth tests need; matches what tests/conftest.py:_APP_ENV_KEYS clears.
NETBOX_URL = "https://netbox.example.com"
KEYCLOAK_BASE_URL = "https://sso.example.com"
KEYCLOAK_REALM = "prod-v1"
JWKS_URL = f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
ISSUER = f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"


def _b64u_uint(n: int) -> str:
    """Base64url-encode a big-endian unsigned int — JWK spec for `n` and `e`."""
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class RSAKeyPair:
    """A test RSA keypair plus its JWK form. Use to sign tokens and serve JWKS."""

    kid: str
    private_pem: bytes
    public_jwk: dict[str, str]


def _make_key(kid: str) -> RSAKeyPair:
    # 2048 is the smallest RSA size that python-jose's RS256 accepts; smaller for speed.
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    nums = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": _b64u_uint(nums.n),
        "e": _b64u_uint(nums.e),
    }
    return RSAKeyPair(kid=kid, private_pem=pem, public_jwk=jwk)


@pytest.fixture(scope="session")
def test_key() -> RSAKeyPair:
    """Default signing key reused across tests — RSA gen is slow, do it once."""
    return _make_key("test-kid-1")


@pytest.fixture(scope="session")
def rotated_key() -> RSAKeyPair:
    """Second keypair to simulate Keycloak key rotation."""
    return _make_key("test-kid-2")


@pytest.fixture(scope="session")
def foreign_key() -> RSAKeyPair:
    """Keypair not in JWKS — used to test signature mismatch."""
    return _make_key("test-kid-foreign")


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the env vars Settings requires so get_settings() works under test."""
    monkeypatch.setenv("NETBOX_URL", NETBOX_URL)
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", KEYCLOAK_BASE_URL)
    monkeypatch.setenv("KEYCLOAK_REALM", KEYCLOAK_REALM)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")


def make_token(
    key: RSAKeyPair,
    *,
    sub: str = "user-1",
    email: str | None = "alice@example.com",
    roles: list[str] | None = None,
    session_id: str | None = "sess-1",
    issuer: str = ISSUER,
    expires_in: int = 300,
    extra_claims: dict[str, Any] | None = None,
    omit_claims: tuple[str, ...] = (),
) -> str:
    """Build and sign a JWT that looks like a Keycloak access token."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": sub,
        "iat": now,
        "exp": now + expires_in,
    }
    if email is not None:
        claims["email"] = email
    if roles is not None:
        claims["realm_access"] = {"roles": roles}
    if session_id is not None:
        claims["sid"] = session_id
    if extra_claims:
        claims.update(extra_claims)
    for omit in omit_claims:
        claims.pop(omit, None)
    token: str = jwt.encode(
        claims,
        key.private_pem,
        algorithm="RS256",
        headers={"kid": key.kid},
    )
    return token


@pytest.fixture
def token_factory() -> Callable[..., str]:
    """Convenience wrapper so tests can call `token_factory(test_key, roles=[...])`."""
    return make_token
