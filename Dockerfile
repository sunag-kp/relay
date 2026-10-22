# syntax=docker/dockerfile:1.7

# Overridable so builds behind a corporate proxy/mirror can use base images that trust it.
ARG PYTHON_IMAGE=python:3.12-slim
ARG NODE_IMAGE=node:22-alpine

# ---------------------------------------------------------------- dashboard (React + TS)
FROM ${NODE_IMAGE} AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---------------------------------------------------------------- python deps
FROM ${PYTHON_IMAGE} AS builder
# Optional extras, e.g. --build-arg EXTRAS=local-embeddings for the fastembed embedder.
ARG EXTRAS=""
RUN pip install --no-cache-dir uv==0.11.32
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project ${EXTRAS:+--extra $EXTRAS}
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable ${EXTRAS:+--extra $EXTRAS}

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
RUN useradd --create-home --uid 10001 relay
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY --from=ui /ui/dist ./ui/dist
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER relay
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=2)"
CMD ["relay-gateway"]
