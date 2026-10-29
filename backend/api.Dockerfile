# API image. On every start: `specflow migrate` (Alembic upgrade to head; a no-op when
# the schema is current), then the API on port 8000. A failed migration stops the start.
# Build context: backend/ (the last stage is the image; no --target needed):
#   docker build -f backend/api.Dockerfile -t <registry>/specflow-api:<tag> backend
# Run ONE replica: the migration takes no lock.
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

# runtime: uid 10001, a HOME dir, the venv, alembic.ini and migrations/. Writes no files
# (logs go to stdout), so it runs on a read-only root filesystem.
FROM python:3.12.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/specflow \
    ALEMBIC_CONFIG=/app/alembic.ini \
    ORT_DISABLE_TELEMETRY=1
RUN groupadd --gid 10001 specflow \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/specflow \
        --shell /usr/sbin/nologin specflow \
    && install -d -o 10001 -g 10001 /var/lib/specflow/graph   # graph build state (GRAPH_STATE_DIR)
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
USER 10001:10001
EXPOSE 8000
# `exec` makes the API PID 1, so SIGTERM reaches it directly.
CMD ["sh", "-c", "specflow migrate && exec specflow api"]
