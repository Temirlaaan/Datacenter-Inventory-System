"""Unit tests for app.services.qr.token — DCQR token format and collision retry.

The collision-retry tests use a tiny fake repo with a pre-arranged answer queue
plus monkeypatched ``generate_token`` so the test is deterministic and doesn't
depend on a real ``QRCodeRepository`` (Task 4).
"""

from __future__ import annotations

import inspect
import re
import string
from collections.abc import Callable

import pytest

from app.services.qr import token as token_module
from app.services.qr.token import (
    _ALPHABET,
    TokenGenerationExhausted,
    generate_token,
    generate_unique_token,
)

# Test helpers -------------------------------------------------------------------


class _FakeRepo:
    """Stand-in for QRCodeRepository — implements only the ``exists`` method.

    ``existing`` is the set of tokens that should report as already-taken.
    ``calls`` records every token ``exists`` was asked about, in order.
    """

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.calls: list[str] = []

    async def exists(self, qr_id: str, /) -> bool:
        self.calls.append(qr_id)
        return qr_id in self.existing


def _make_sequence(values: list[str]) -> Callable[[], str]:
    """Build a zero-arg callable that returns ``values`` in order — used to
    monkeypatch ``generate_token`` for the collision-retry tests.
    """
    it = iter(values)
    return lambda: next(it)


# Format / alphabet --------------------------------------------------------------


def test_generate_token_has_dcqr_prefix() -> None:
    assert generate_token().startswith("DCQR-")


def test_generate_token_has_exactly_13_chars_total() -> None:
    # Matches the VARCHAR(13) primary-key column from the Task 2 migration.
    assert len(generate_token()) == 13


def test_generate_token_body_matches_alphabet_regex() -> None:
    pattern = re.compile(r"^DCQR-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$")
    assert pattern.match(generate_token())


def test_generate_token_alphabet_excludes_forbidden_chars_across_many_samples() -> None:
    # ToR §4.2.1 excludes I, O, 0, 1 to avoid visual confusion on printed labels.
    forbidden = {"I", "O", "0", "1"}
    seen_chars: set[str] = set()
    for _ in range(1000):
        seen_chars.update(generate_token()[5:])  # drop the DCQR- prefix
    assert seen_chars.isdisjoint(forbidden)


def test_generate_token_source_uses_secrets_not_random() -> None:
    # Cryptographically secure RNG required (ToR §4.2.1). Reading the source
    # rather than monkey-patching avoids flaky tests and catches anyone swapping
    # ``secrets`` for ``random`` in a hot-path "optimization".
    source = inspect.getsource(token_module)
    assert re.search(r"^import secrets\b", source, re.MULTILINE)
    assert not re.search(r"^import random\b", source, re.MULTILINE)
    assert not re.search(r"^from random\b", source, re.MULTILINE)


# generate_unique_token happy path ----------------------------------------------


async def test_generate_unique_token_returns_first_token_when_no_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(token_module, "generate_token", _make_sequence(["DCQR-AAAAAAAA"]))
    repo = _FakeRepo()

    result = await generate_unique_token(repo)

    assert result == "DCQR-AAAAAAAA"
    assert repo.calls == ["DCQR-AAAAAAAA"]


# Collision retry ----------------------------------------------------------------


async def test_generate_unique_token_retries_on_collision_and_returns_next_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        token_module,
        "generate_token",
        _make_sequence(["DCQR-AAAAAAAA", "DCQR-BBBBBBBB"]),
    )
    repo = _FakeRepo(existing={"DCQR-AAAAAAAA"})

    result = await generate_unique_token(repo)

    assert result == "DCQR-BBBBBBBB"
    assert repo.calls == ["DCQR-AAAAAAAA", "DCQR-BBBBBBBB"]


async def test_generate_unique_token_raises_when_max_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        token_module,
        "generate_token",
        _make_sequence(["DCQR-AAAAAAAA", "DCQR-BBBBBBBB", "DCQR-CCCCCCCC"]),
    )
    repo = _FakeRepo(existing={"DCQR-AAAAAAAA", "DCQR-BBBBBBBB", "DCQR-CCCCCCCC"})

    with pytest.raises(TokenGenerationExhausted) as exc:
        await generate_unique_token(repo, max_retries=3)

    assert exc.value.attempts == 3
    assert "3" in str(exc.value)
    assert len(repo.calls) == 3


# Constants ----------------------------------------------------------------------


def test_alphabet_constant_is_exactly_32_chars() -> None:
    # 32 chars * 8 positions = 32^8 ≈ 1.1T keyspace per ToR §4.2.1.
    assert len(_ALPHABET) == 32


def test_alphabet_constant_is_uppercase_letters_and_digits_only() -> None:
    allowed = set(string.ascii_uppercase + string.digits)
    assert set(_ALPHABET).issubset(allowed)
