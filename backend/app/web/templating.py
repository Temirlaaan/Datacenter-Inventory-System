"""Shared Jinja2 templates instance for the ``/web/*`` admin surface.

Extracted into its own module so page-handler modules (``router``, ``users``,
and future per-surface splits) can import one ``templates`` object instead of
reaching into ``app.web.router``. All templates live under ``app/web/templates``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
