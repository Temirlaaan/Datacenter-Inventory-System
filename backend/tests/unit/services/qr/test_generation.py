"""Unit tests for app.services.qr.generation — Pydantic validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.qr.generation import GenerateBatchRequest


def test_generate_batch_request_accepts_minimal_valid_input() -> None:
    req = GenerateBatchRequest(count=10)
    assert req.count == 10
    assert req.intended_site_id is None
    assert req.intended_location_id is None
    assert req.intended_rack_id is None
    assert req.comment is None


def test_generate_batch_request_accepts_count_one() -> None:
    assert GenerateBatchRequest(count=1).count == 1


def test_generate_batch_request_accepts_count_five_hundred() -> None:
    assert GenerateBatchRequest(count=500).count == 500


def test_generate_batch_request_rejects_count_zero() -> None:
    with pytest.raises(ValidationError):
        GenerateBatchRequest(count=0)


def test_generate_batch_request_rejects_count_negative() -> None:
    with pytest.raises(ValidationError):
        GenerateBatchRequest(count=-1)


def test_generate_batch_request_rejects_count_five_hundred_one() -> None:
    # Sprint 2 anti-criterion: keep memory bounded by capping at 500.
    with pytest.raises(ValidationError):
        GenerateBatchRequest(count=501)


def test_generate_batch_request_accepts_comment_exactly_two_hundred_chars() -> None:
    GenerateBatchRequest(count=1, comment="x" * 200)


def test_generate_batch_request_rejects_comment_longer_than_two_hundred_chars() -> None:
    with pytest.raises(ValidationError):
        GenerateBatchRequest(count=1, comment="x" * 201)


def test_generate_batch_request_persists_all_intended_fields() -> None:
    req = GenerateBatchRequest(
        count=50,
        intended_site_id=1,
        intended_location_id=2,
        intended_rack_id=3,
        comment="rack 14 spares",
    )
    assert req.intended_site_id == 1
    assert req.intended_location_id == 2
    assert req.intended_rack_id == 3
    assert req.comment == "rack 14 spares"
