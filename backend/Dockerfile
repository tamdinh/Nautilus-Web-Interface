# syntax=docker/dockerfile:1
# Backend image: FastAPI + NautilusTrader 2.0.0rc8 on Python 3.13.
#
# - Dependencies come from the hashed lock (requirements.lock) via uv; wheels only
#   (nautilus_trader wheels are manylinux_2_34, so use a glibc image, never alpine).
# - NO secrets are baked in. The app refuses to start unless SECRET_KEY, ADMIN_PASSWORD
#   (>= 12 chars) and ENCRYPTION_KEY (Fernet) are provided at runtime, or
#   ENVIRONMENT=development is set explicitly (local use only).
# - Run exactly ONE container/process: app state is in-process plus SQLite.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    NAUTILUS_CATALOG_PATH=/app/data/catalog

# Pinned uv binary (bump together with the lock-regeneration command in requirements.lock)
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv

WORKDIR /app

# Install dependencies first (layer cache): hashes enforced, wheels only
COPY requirements.lock .
RUN uv pip install --system --no-cache --require-hashes --only-binary :all: -r requirements.lock

# Application code only (no tests, venvs, data or env files; see .dockerignore)
COPY *.py ./
COPY adapters/ adapters/
COPY routers/ routers/
COPY strategies/ strategies/

# Writable data dir (SQLite DBs, backups, catalog) on a mounted volume + non-root user
RUN mkdir -p /app/data/catalog \
    && addgroup --system appgroup \
    && adduser --system --ingroup appgroup --no-create-home appuser \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

# /readyz checks DB + engine; startup itself fails closed on bad configuration
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/readyz', timeout=5)"]

# Single process on purpose (do not add uvicorn/gunicorn workers)
CMD ["python3", "nautilus_fastapi.py"]
