# syntax=docker/dockerfile:1

# Stage 1 — build the React SPA. The compiled static bundle is served by the same
# FastAPI process at runtime, so one container is the whole dashboard.
FROM node:20-alpine AS frontend

WORKDIR /web
# Copy manifests first so `npm ci` is cached until dependencies change.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# Stage 2 — build the Python venv (build tools needed to compile lxml never ship
# in the runtime image).
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
# The [api] extra pulls in fastapi + uvicorn + pypdf so the JSON API — which also
# serves the built SPA — ships in the image.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[api]"


FROM python:3.12-slim AS runtime

# tzdata: the scheduler runs on Europe/Berlin wall-clock time, which needs a
# real timezone database in the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 tzdata \
    && rm -rf /var/lib/apt/lists/*

# Never run as root.
RUN useradd --create-home --uid 1000 alerts

COPY --from=builder /opt/venv /opt/venv
# The built React SPA. FastAPI mounts it at / (see JOB_ALERTS_STATIC_DIR) and
# serves /api from the same process — one container, one port.
COPY --from=frontend /web/dist /app/web

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Berlin \
    JOB_ALERTS_DATABASE_PATH=/data/jobs.db \
    JOB_ALERTS_STATIC_DIR=/app/web \
    # Route any temp/cache writes to /tmp, which is a writable tmpfs even when the
    # container root filesystem is read-only. HOME is set for the same reason.
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

WORKDIR /app
COPY config/ ./config/

# The database lives on a mounted volume so it survives `docker run --rm` and
# image rebuilds. Without this, every run would re-notify every job.
RUN mkdir -p /data && chown -R alerts:alerts /data /app
VOLUME ["/data"]

USER alerts

# The dashboard (SPA + API) listens here; compose maps it to the host.
EXPOSE 7860

# Fails if the package or its config cannot load. Deliberately generic: the same
# image serves the dashboard AND the scheduler, and this must pass during the
# "waiting for the LLM endpoint" phase before the web server is even up.
HEALTHCHECK --interval=5m --timeout=10s --start-period=5s --retries=2 \
    CMD python -c "import job_alerts; import sys; sys.exit(0)" || exit 1

# Default: serve the dashboard — the React SPA and its JSON API from one process
# (JOB_ALERTS_STATIC_DIR points at the built bundle). Browsing needs no LLM, so
# there is no startup gate; translation and new searches degrade gracefully when
# the tunnel is down.
# Override for the scheduler or a one-off:
#   docker run ... germany-research-job-alerts run-scheduler
#   docker run --rm ... germany-research-job-alerts search --dry-run
ENTRYPOINT ["python", "-m", "job_alerts"]
CMD ["serve", "--host", "0.0.0.0"]
