# syntax=docker/dockerfile:1.7
#
# Multi-stage build: the builder uses uv to resolve+install dependencies into a venv;
# the runtime image copies that venv onto a slim Python base. No build tools, no uv,
# no caches in the final image.

# ---------- builder ----------
FROM python:3.12-slim AS builder

# uv reads PYTHONDONTWRITEBYTECODE/UV_LINK_MODE from env to avoid hardlink warnings on
# bind mounts; UV_COMPILE_BYTECODE precompiles .pyc into the venv (faster cold start).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Install uv via the official standalone binary — no apt packages, no compilers.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Resolve and install deps in their own layer so source-only changes don't bust the
# dependency cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now copy source and install the project itself (editable=false; this is a build artifact).
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Run as non-root. UID/GID 1000 keeps file ownership predictable on bind-mounted volumes.
RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --no-create-home --shell /sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app scripts/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER app

EXPOSE 8000

# Self-describing healthcheck so the image is usable outside docker-compose
# (k8s, ECS, plain `docker run`). urllib avoids needing curl in the slim image.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=6 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=2).status==200 else 1)" \
    || exit 1

# Migration runs before uvicorn. Failure here aborts startup — by design, since serving
# traffic against an out-of-date schema would corrupt data.
ENTRYPOINT ["/app/entrypoint.sh"]
# ``--proxy-headers`` makes uvicorn read X-Forwarded-Proto + X-Forwarded-For
# set by the reverse proxy (nginx/Caddy/Traefik), so FastAPI sees the
# correct scheme/host/client-ip. Critical for the OIDC redirect URI
# (web/router.py:_keycloak_redirect_uri) — Keycloak rejects a callback
# under ``http://`` when only ``https://...`` is registered.
#
# ``--forwarded-allow-ips '*'`` trusts forwarded headers from any source.
# Safe here because port 8000 is never exposed publicly — it's only reachable
# from the reverse proxy on the same Docker network / host.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
