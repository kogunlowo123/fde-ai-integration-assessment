# syntax=docker/dockerfile:1
#
# Multi-stage build.
#
# Stage 1 installs into a virtual environment so the runtime image carries no
# build toolchain, no pip cache and no source distribution. Stage 2 copies that
# environment and the package, and nothing else.
#
# Security posture:
#   * pinned base image digest tags are left to the deploying team's policy;
#     the tag here is pinned to a minor version, not `latest`
#   * runs as a non-root user with no write access to the application code
#   * no secrets baked in, configuration arrives through the environment
#   * a writable data volume is mounted rather than baked into the layer

# ---------------------------------------------------------------------------
# Stage 1: build
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency metadata first: this layer is cached until pyproject.toml changes.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    LOG_FORMAT=json \
    BIND_HOST=0.0.0.0 \
    DATABASE_PATH=/data/gateway.db

# System packages are deliberately not installed: the slim base already has
# everything the application needs, and every added package is added CVE surface.

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app \
 && mkdir -p /app /data \
 && chown -R app:app /app /data

COPY --from=builder --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root corpus /app/corpus

WORKDIR /app
USER app

# /data is the only writable path the application needs (SQLite, WAL files).
VOLUME ["/data"]

EXPOSE 8000 8001

# No curl in the image, so the check uses the interpreter that is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys;\
port=os.environ.get('HEALTHCHECK_PORT','8001');\
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=3).status==200 else 1)"]

# Default to the LLM gateway; compose overrides the command for the others.
CMD ["python", "-m", "fde_assessment.llm_gateway"]
