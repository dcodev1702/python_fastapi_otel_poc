# syntax=docker/dockerfile:1
# =============================================================================
# Foundry Learn Agent - container image
# Author : dcodev1702 & M365 Copilot / Cowork
# Created: 2026-09-28
#
# Build & run through compose (see compose.yaml); direct use for reference:
#   docker build -t foundry-learn-agent:0.3.0 .
#   docker run --rm -p 127.0.0.1:8000:8000 --env-file .env --memory 3g foundry-learn-agent:0.3.0
# =============================================================================
FROM python:3.14.7-slim

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

# Dependencies first so this layer is cached until the requirements change.
#
# requirements/lock.txt is the resolved, pinned set that scripts/lock.sh produced from requirements/base.txt inside
# this very base image - installing it makes every build reproducible. A clone without the lock still builds, from
# the floors in base.txt, with a loud warning, because that build picks up whatever is newest on PyPI today (the
# httpx2 surprise in CHANGELOG 0.2.2 is what that looks like). requirements/dev.txt is kept out by .dockerignore.
# `pip check` fails the build if the installed set is inconsistent (e.g. an OpenTelemetry package out of lockstep).
COPY requirements/ requirements/
RUN python -m pip install --upgrade pip setuptools wheel \
 && if [ -f requirements/lock.txt ]; then \
        echo ">> installing the pinned set from requirements/lock.txt" \
        && python -m pip install -r requirements/lock.txt; \
    else \
        echo ">> WARNING: requirements/lock.txt not found - installing floors from requirements/base.txt." \
        && echo ">>          Run scripts/lock.sh and commit the lock for reproducible builds." \
        && python -m pip install -r requirements/base.txt; \
    fi \
 && python -m pip check \
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

# No --reload in a container. 0.0.0.0 means the container's own interfaces, which Docker needs to publish the
# port; the host side is decided by the -p / ports: mapping, which this project keeps on IPv4 127.0.0.1 (plus
# LAN_IP, if set).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
