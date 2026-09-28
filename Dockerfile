# syntax=docker/dockerfile:1
# =============================================================================
# Foundry Learn Agent - container image
# Author : dcodev1702 & M365 Copilot / Cowork
# Created: 2026-09-28
#
# Build & run through compose (see compose.yaml); direct use for reference:
#   docker build -t foundry-learn-agent:0.2.0 .
#   docker run --rm -p 8000:8000 --env-file .env --memory 3g foundry-learn-agent:0.2.0
# =============================================================================
FROM python:3.12-slim

# Python behaves better in containers with these set:
#   PYTHONUNBUFFERED        -> ConsoleSpanExporter output and the [healthz] heartbeat reach `docker compose logs`
#                              immediately instead of sitting in a stdout buffer
#   PYTHONDONTWRITEBYTECODE -> no .pyc clutter in the image
#   PIP_*                   -> smaller image, quieter build
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so this layer is cached until requirements.txt changes.
# pip is upgraded before use; requirements.txt uses floors, so each build picks up the newest compatible releases.
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
 && python -m pip install -r requirements.txt \
 && python -m pip list --format=columns

# Application code (everything else is kept out by .dockerignore - notably .env)
COPY app.py .

# Never run as root inside the container.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Container-level liveness probe against /healthz. It is EXCLUDED from tracing in app.py, so this never
# creates a span - but it does show up in the [healthz] request counters, which is the point.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# No --reload in a container; bind to all interfaces so Docker can publish the port.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
