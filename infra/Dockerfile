# ============================================================
# Stage 1: lean-base  --  Lean toolchain + lake build
# ============================================================
FROM ubuntu:22.04 AS lean-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git && \
    rm -rf /var/lib/apt/lists/*

# Install elan (Lean version manager) and the project's toolchain.
RUN curl https://elan-init.github.io/elan/elan-init.sh -sSf | sh -s -- -y
ENV PATH="/root/.elan/bin:${PATH}"

WORKDIR /app/lean
COPY lean/ .

# Fetch deps & build.
RUN lake update && \
    (lake exe cache get || true) && \
    lake build ForgeLean

# ============================================================
# Stage 2: python-base  --  Python runtime + pip deps
# ============================================================
FROM python:3.11-slim AS python-base

WORKDIR /app

COPY pyproject.toml setup.cfg* setup.py* ./
COPY services/ services/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e "." || \
    pip install --no-cache-dir -e ".[dev]" || true

# ============================================================
# Stage 3: service  --  runnable image
# ============================================================
FROM python-base AS service

# Copy the built Lean artifacts so lean_env can shell out to lean/lake.
COPY --from=lean-base /root/.elan /root/.elan
COPY --from=lean-base /app/lean /app/lean
ENV PATH="/root/.elan/bin:${PATH}"

COPY config/ config/
COPY data/ data/

# SERVICE_MODULE and SERVICE_PORT are set at run-time.
#   e.g. docker run -e SERVICE_MODULE=services.lean_env.main:app -e SERVICE_PORT=8101
ENV SERVICE_MODULE="services.orchestrator.main:app"
ENV SERVICE_PORT="8100"

EXPOSE ${SERVICE_PORT}

CMD uvicorn "$SERVICE_MODULE" --host 0.0.0.0 --port "$SERVICE_PORT"
