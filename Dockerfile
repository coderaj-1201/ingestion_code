# ── Multi-stage build for all three ingestion agents ─────────────────────────
# Each agent uses the same image; only CMD differs.
# Build once, run three times:
#
#   docker build -t ingestion-pipeline .
#   docker run -p 8010:8010 --env-file .env -e AGENT=ingestion  ingestion-pipeline
#   docker run -p 8011:8011 --env-file .env -e AGENT=processing ingestion-pipeline
#   docker run -p 8012:8012 --env-file .env -e AGENT=embedding  ingestion-pipeline
#
# Or use docker-compose (see docker-compose.yml).

# ── Stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System packages needed to compile native extensions (pdfplumber → pdfminer)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime-only system packages (no compilers)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY agents/    agents/
COPY processors/ processors/
COPY shared/    shared/

# Non-root user for security — ACA runs as non-root by default
RUN addgroup --system app && adduser --system --ingroup app app
USER app

# Health check — each agent exposes /health on its own port.
# The HEALTHCHECK port is overridden per-agent in docker-compose / ACA.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8010}/health || exit 1

# Entrypoint selects the agent via the AGENT env var.
# This avoids maintaining three separate Dockerfiles for an identical image.
ENV AGENT=ingestion \
    PORT=8010 \
    PYTHONPATH=/app

CMD python -m uvicorn \
        agents.${AGENT}_agent:app \
        --host 0.0.0.0 \
        --port ${PORT} \
        --workers 1 \
        --log-level warning
