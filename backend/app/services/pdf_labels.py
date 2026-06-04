"""PDF batch-label rendering (Sprint 8b Task 2, Architecture §6).

``render_batch_labels_pdf`` is a pure function: in → bytes. No I/O, no DB,
no async. The endpoint at ``GET /api/v1/admin/batches/{id}/labels.pdf``
calls this via ``asyncio.to_thread`` so the event loop stays responsive
while reportlab renders.

Layout: A4 landscape, 8 columns x 4 rows = 32 labels per page. Each label
is a QR code (encoding the raw ``DCQR-XXXXXXXX`` id — what the mobile
scanner reads) with the same id printed as a caption below it. Page break
every 32 labels; codes overflow onto additional pages.

reportlab dep is pre-approved at Sprint 8b plan stage (decision F). The
``reportlab.graphics.barcode.qr.QrCodeWidget`` module renders the QR
without pulling in a separate ``qrcode``/``pillow``-only path.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as canvas_mod

from app.domain.qr import QR, QRBatch

_COLS = 8
_ROWS = 4
LABELS_PER_PAGE = _COLS * _ROWS
"""Public constant — exposed so callers + tests can predict page counts."""

_PAGE_MARGIN_MM = 10.0
_LABEL_CAPTION_HEIGHT_MM = 6.0
_LABEL_INNER_PADDING_MM = 2.0


def _draw_label(
    c: canvas_mod.Canvas,
    *,
    x: float,
    y: float,
    cell_w: float,
    cell_h: float,
    qr_id: str,
) -> None:
    """Render one QR + caption inside a ``(cell_w x cell_h)`` rectangle anchored
    at ``(x, y)`` (lower-left).

    Layout inside the cell:
    - top region: square QR sized to ``min(cell_w, cell_h - caption_height)``
    - bottom region (caption_height tall): centred id text
    """
    inner_padding = _LABEL_INNER_PADDING_MM * mm
    caption_h = _LABEL_CAPTION_HEIGHT_MM * mm
    available_w = cell_w - 2 * inner_padding
    available_h = cell_h - caption_h - 2 * inner_padding
    qr_size = min(available_w, available_h)

    # Centre the QR horizontally inside the cell + place it just above the caption.
    qr_x = x + (cell_w - qr_size) / 2
    qr_y = y + caption_h + inner_padding

    widget = QrCodeWidget(qr_id, barLevel="M")
    bounds = widget.getBounds()
    widget_w = bounds[2] - bounds[0]
    widget_h = bounds[3] - bounds[1]
    drawing = Drawing(
        qr_size, qr_size, transform=[qr_size / widget_w, 0, 0, qr_size / widget_h, 0, 0]
    )
    drawing.add(widget)
    drawing.drawOn(c, qr_x, qr_y)

    # Caption: monospace 8pt, centred underneath the QR.
    c.setFont("Courier", 8)
    text_y = y + inner_padding
    c.drawCentredString(x + cell_w / 2, text_y, qr_id)


def render_batch_labels_pdf(*, batch: QRBatch, codes: list[QR]) -> bytes:
    """Render an A4-landscape PDF with one label per QR code.

    A4 landscape, 8x4 = 32 labels per page. Empty ``codes`` still returns a
    valid (single blank page) PDF so the endpoint always serves a downloadable
    artifact. ``batch.id`` is set as the PDF's Title metadata so a browser tab
    or download manager shows a stable name.
    """
    buf = BytesIO()
    # pageCompression=0 leaves content streams uncompressed so QR-id text is
    # greppable in the byte stream — keeps the unit tests free of a PDF-parser
    # dep. The size cost on a 32-label batch is negligible (~10-15 KB delta).
    c = canvas_mod.Canvas(buf, pagesize=landscape(A4), pageCompression=0)
    c.setTitle(f"batch-{batch.id}")
    page_w, page_h = landscape(A4)
    margin = _PAGE_MARGIN_MM * mm
    cell_w = (page_w - 2 * margin) / _COLS
    cell_h = (page_h - 2 * margin) / _ROWS

    for idx, qr in enumerate(codes):
        if idx > 0 and idx % LABELS_PER_PAGE == 0:
            c.showPage()
        slot = idx % LABELS_PER_PAGE
        row = slot // _COLS
        col = slot % _COLS
        # reportlab's coord origin is bottom-left; row 0 is the top row.
        x = margin + col * cell_w
        y = page_h - margin - (row + 1) * cell_h
        _draw_label(c, x=x, y=y, cell_w=cell_w, cell_h=cell_h, qr_id=qr.id)

    # Finalise the last partial page so c.save() emits it. Also handles the
    # empty-codes case where no draw calls happened: showPage() emits a
    # single blank page so the file is still a valid downloadable PDF.
    c.showPage()
    c.save()
    return buf.getvalue()
