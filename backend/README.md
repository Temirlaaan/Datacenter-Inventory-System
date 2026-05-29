# DC Inventory Backend

FastAPI service for the datacenter inventory system. Talks to NetBox over HTTP
and persists app-specific state (QR lifecycle, audit log, sessions, idempotency
keys, form config) in PostgreSQL.

**Status (Sprint 1 — Foundation):** runnable foundation with auth, async DB,
NetBox client, `/health`, containerized stack. **No business logic yet** — QR
registry, device updates, audit log, web admin all land in Sprint 2+.

See repo root `Architecture_Overview.md` for design and `docs/work-log.md` for
sprint history.

## Quick start — local dev (no container)

```bash
uv sync --group dev

# Bring up the test Postgres for integration tests (port 5433, tmpfs, fresh on every up).
docker compose -f docker-compose.test.yml up -d

# Tests + lint + types
DATABASE_URL=postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test \
  NETBOX_URL=https://netbox.example.com NETBOX_SERVICE_TOKEN=x \
  KEYCLOAK_BASE_URL=https://sso.example.com \
  uv run pytest --cov=app
uv run ruff check .
uv run black --check .
uv run mypy app tests
```

## Quick start — full containerized stack

```bash
cp .env.example .env       # fill in real values for NetBox + Keycloak
docker compose up -d --build

curl localhost:8000/health
# => {"status":"ok","checks":{"db":{"status":"ok"},"netbox":{...},"keycloak":{...}}}
# (returns 503 + "degraded" if NetBox or Keycloak is unreachable — expected without VPN)

docker compose down        # keeps DB volume
docker compose down -v     # wipes DB volume too
```

## Layout

```
app/
├── main.py            # FastAPI app, lifespan, request_id middleware
├── config.py          # pydantic-settings (env + /run/secrets)
├── api/v1/            # versioned JSON routes (mobile)
├── auth/              # JWKS cache + JWT validation + AuthUser
├── db/                # async SQLAlchemy engine + sessionmaker + Base
├── netbox/            # async client (read-only in Sprint 1) + minimal models
├── observability/     # structlog JSON config
├── domain/            # pure-Python domain types (no SQLAlchemy/Pydantic)  [Sprint 2+]
├── services/          # business operations  [Sprint 2+]
└── web/               # admin HTML routes  [Sprint 2+]

alembic/               # migrations (env.py uses async engine)
tests/{unit,integration}/
```

## Migrations

```bash
uv run alembic upgrade head           # apply pending
uv run alembic revision --autogenerate -m "short description"
```

Container entrypoint runs `alembic upgrade head` before uvicorn — destructive
migrations must be split across two releases (CLAUDE.md cross-cutting #7).
See [alembic/README.md](alembic/README.md) for the full policy.
