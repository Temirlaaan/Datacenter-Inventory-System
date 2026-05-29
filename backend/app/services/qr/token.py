"""DCQR token generation. ToR §4.2.1.

Tokens are ``DCQR-XXXXXXXX`` where the 8-char suffix is uniformly sampled from
a 32-char alphabet that drops visually-confusable characters (``I, O, 0, 1``).
The keyspace is 32^8 ≈ 1.1 trillion — collisions are vanishingly rare, but the
``generate_unique_token`` wrapper still guards against them defensively against
the registry so a duplicate cannot slip into the PK.
"""

from __future__ import annotations

import secrets
from typing import Protocol

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars; no I, O, 0, 1
_PREFIX = "DCQR-"
_SUFFIX_LEN = 8


class TokenGenerationExhausted(Exception):
    """Raised when ``generate_unique_token`` cannot find an unused token in time."""

    def __init__(self, attempts: int) -> None:
        super().__init__(f"could not generate a unique QR token after {attempts} attempts")
        self.attempts = attempts


class _QRExistenceChecker(Protocol):
    """Structural type for the QR registry lookup used by ``generate_unique_token``.

    Task 4's ``QRCodeRepository.exists`` satisfies this Protocol without needing
    an explicit ``isinstance``/inheritance link.
    """

    async def exists(self, qr_id: str, /) -> bool: ...


def generate_token() -> str:
    """Return one freshly-minted DCQR token. Cryptographically random via ``secrets``."""
    return _PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(_SUFFIX_LEN))


async def generate_unique_token(repo: _QRExistenceChecker, *, max_retries: int = 10) -> str:
    """Generate a DCQR token not present in ``repo``; retry on collision.

    Raises ``TokenGenerationExhausted`` if every attempt up to ``max_retries``
    collides. Useful as a circuit breaker — collisions in a healthy registry
    are statistically negligible, so repeated exhaustion is a signal of a sick
    registry or a buggy ``exists`` implementation rather than bad luck.
    """
    for _ in range(max_retries):
        token = generate_token()
        if not await repo.exists(token):
            return token
    raise TokenGenerationExhausted(max_retries)
