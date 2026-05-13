# DC Inventory Backend

FastAPI service for the datacenter inventory system. QR lifecycle + NetBox proxy.
See repo root `Architecture_Overview.md` and `docs/sprint-1.md` for design and current sprint.

## Quick start

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run black --check .
```
