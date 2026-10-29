# Worker image: `specflow worker`. One image for both workers; the env decides
# the queue: WORKER_QUEUES=parse (worker-parse) or WORKER_QUEUES=agents (worker-agents).
# Port 8001 is liveness only (GET /healthz, WORKER_HEALTH_PORT); readiness is the exec
# probe `specflow worker --check`. No migrations here: the API image runs them.
# Build context: backend/ (the last stage is the image; no --target needed):
#   docker build -f backend/worker.Dockerfile -t <registry>/specflow-worker:<tag> backend
# The base tag is a literal (no ARG); uv is pinned to an exact version.

# build: the locked runtime dependencies, then the project itself NON-editable, so the
# runtime stage needs only the virtualenv (no src/ tree).
FROM python:3.12.13-slim-bookworm AS build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system
RUN pip install uv==0.11.21
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra gcp --extra aws
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra gcp --extra aws

# runtime: uid 10001, a HOME dir, the venv only. Logs go to stdout; read-only root filesystem
# is fine. worker-parse needs a writable /tmp (the PDF parse writes temp files there).
FROM python:3.12.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/specflow \
    ORT_DISABLE_TELEMETRY=1
RUN groupadd --gid 10001 specflow \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/specflow \
        --shell /usr/sbin/nologin specflow
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8001
CMD ["specflow", "worker"]
