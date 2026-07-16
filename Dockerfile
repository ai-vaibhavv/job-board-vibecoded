# syntax=docker/dockerfile:1

# Two stages so build tools (needed to compile lxml) never ship in the runtime
# image.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
# The [dashboard] extra pulls in gradio + pypdf so the web UI ships in the image.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[dashboard]"


FROM python:3.12-slim AS runtime

# tzdata: the scheduler runs on Europe/Berlin wall-clock time, which needs a
# real timezone database in the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 tzdata \
    && rm -rf /var/lib/apt/lists/*

# Never run as root.
RUN useradd --create-home --uid 1000 alerts

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Berlin \
    JOB_ALERTS_DATABASE_PATH=/data/jobs.db \
    # Keep every cache/temp the dashboard stack (gradio, huggingface_hub,
    # matplotlib) might write inside /tmp, which is a writable tmpfs even when the
    # container root filesystem is read-only. HOME is set for the same reason.
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    GRADIO_TEMP_DIR=/tmp/gradio \
    GRADIO_ANALYTICS_ENABLED=False \
    HF_HOME=/tmp/hf \
    MPLCONFIGDIR=/tmp/mpl

WORKDIR /app
COPY config/ ./config/

# The database lives on a mounted volume so it survives `docker run --rm` and
# image rebuilds. Without this, every run would re-notify every job.
RUN mkdir -p /data && chown -R alerts:alerts /data /app
VOLUME ["/data"]

USER alerts

# The dashboard listens here; compose maps it to the host.
EXPOSE 7860

# Fails if the package or its config cannot load. Deliberately generic: the same
# image serves the dashboard AND the scheduler, and this must pass during the
# "waiting for the LLM endpoint" phase before the web server is even up.
HEALTHCHECK --interval=5m --timeout=10s --start-period=5s --retries=2 \
    CMD python -c "import job_alerts; import sys; sys.exit(0)" || exit 1

# Default: wait for the self-hosted LLM to come online, then serve the dashboard.
# Override for the scheduler or a one-off:
#   docker run ... germany-research-job-alerts run-scheduler
#   docker run --rm ... germany-research-job-alerts search --dry-run
ENTRYPOINT ["python", "-m", "job_alerts"]
CMD ["dashboard", "--host", "0.0.0.0", "--wait-for-llm"]
