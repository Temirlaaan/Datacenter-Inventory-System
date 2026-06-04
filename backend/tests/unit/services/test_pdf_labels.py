"""Unit tests for ``app.services.pdf_labels.render_batch_labels_pdf``.

Pure-function tests — no DB, no async. Asserts on the PDF byte-stream:
the magic header, page-count boundaries (1, 32, 33, 100 codes), and that
the QR id text surfaces somewhere in the file. PDF page count is read
without a new dep by counting ``/Type /Page`` markers in the raw bytes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.qr import QR, QRBatch, QRStatus
from app.services.pdf_labels import LABELS_PER_PAGE, render_batch_labels_pdf

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_USER = UUID("11111111-1111-1111-1111-111111111111")


def _batch() -> QRBatch:
    return QRBatch(
        id=uuid4(),
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=_USER,
        count=0,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )


def _qr(token: str, batch_id: UUID) -> QR:
    return QR(
        id=token,
        batch_id=batch_id,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _qrs(batch: QRBatch, n: int) -> list[QR]:
    return [_qr(f"DCQR-{i:08d}", batch.id) for i in range(n)]


def _page_count(pdf: bytes) -> int:
    """Count ``/Type /Page`` (with the trailing space — distinguishes Page from
    Pages) occurrences in the PDF byte stream. reportlab writes the cross-ref
    table un-compressed by default, so a regex over the raw bytes works."""
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf))


# ---------- magic + emptiness ------------------------------------------------


def test_render_batch_labels_pdf_returns_valid_pdf_bytes_for_empty_codes() -> None:
    """An empty batch still produces a valid (blank) PDF — the endpoint must
    always serve a downloadable artifact, never a 204/empty body."""
    pdf = render_batch_labels_pdf(batch=_batch(), codes=[])
    assert pdf.startswith(b"%PDF-")
    # canvas.save() writes a single blank page even with no draw calls.
    assert _page_count(pdf) == 1


# ---------- page-count boundaries -------------------------------------------


@pytest.mark.parametrize(
    "n,expected_pages",
    [
        (1, 1),
        (LABELS_PER_PAGE - 1, 1),
        (LABELS_PER_PAGE, 1),
        (LABELS_PER_PAGE + 1, 2),
        (LABELS_PER_PAGE * 3, 3),
        (LABELS_PER_PAGE * 3 + 1, 4),
        (100, 4),  # ceil(100 / 32) == 4
    ],
)
def test_render_batch_labels_pdf_page_count_matches_ceiling_of_codes_over_32(
    n: int, expected_pages: int
) -> None:
    batch = _batch()
    pdf = render_batch_labels_pdf(batch=batch, codes=_qrs(batch, n))
    assert _page_count(pdf) == expected_pages


# ---------- caption rendering -----------------------------------------------


def test_render_batch_labels_pdf_embeds_qr_id_text_in_caption() -> None:
    """The id printed under each QR must appear in the PDF byte stream.
    reportlab's Courier font writes the characters individually via TJ ops;
    asserting the id substring is a sufficient signal that the caption
    rendered without needing a PDF-parser dep."""
    batch = _batch()
    qrs = [_qr("DCQR-ABCD1234", batch.id)]
    pdf = render_batch_labels_pdf(batch=batch, codes=qrs)
    assert b"DCQR-ABCD1234" in pdf


def test_render_batch_labels_pdf_sets_title_metadata_from_batch_id() -> None:
    """PDF /Title metadata = "batch-{uuid}" so browser-tab / download-manager
    UIs surface a stable name."""
    batch = _batch()
    pdf = render_batch_labels_pdf(batch=batch, codes=[])
    assert f"batch-{batch.id}".encode() in pdf


# ---------- multi-page slot positions ---------------------------------------


def test_render_batch_labels_pdf_writes_each_code_to_its_own_slot() -> None:
    """All ids must appear in the body — the loop's slot indexing must not
    accidentally overwrite or skip cells."""
    batch = _batch()
    codes = _qrs(batch, LABELS_PER_PAGE + 5)
    pdf = render_batch_labels_pdf(batch=batch, codes=codes)
    for c in codes:
        assert c.id.encode() in pdf, f"missing id in PDF body: {c.id}"
