# MCP image: `specflow mcp`, the MCP server as an HTTP router over the API (it calls the API at API_INTERNAL_URL).
# It installs the `mcp` extra only: no database, graph, Temporal, LLM, parser or object-storage library, and no
# migrations (the API image runs them). Port 8002 serves the MCP endpoint plus GET /healthz (liveness)
# and GET /readyz (the API's readiness passed through).
# Build context: backend/ (the last stage is the image; no --target needed):
#   docker build -f backend/docker/mcp/Dockerfile -t <registry>/specflow-mcp:<tag> backend
# The base tag is a literal (no ARG); uv is pinned to an exact version.

# build: the locked runtime dependencies, then the project itself NON-editable, so the
# runtime stage needs only the virtualenv (no src/ tree).
FROM python:3.12.13-slim-bookworm AS build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    SSL_CERT_FILE=/etc/ssl/build-ca.pem \
    PIP_CERT=/etc/ssl/build-ca.pem \
    REQUESTS_CA_BUNDLE=/etc/ssl/build-ca.pem
# The CA bundle of the build stages: the image's own bundle plus, when the build gets the secret extra_ca, the CA of a
# proxy that inspects TLS (`make up EXTRA_CA=<file>`, docs/configuration.md "Behind a TLS-inspecting proxy"). Without
# the secret it is the image's bundle unchanged. The runtime stage starts from the base image again, so the image has
# neither this bundle nor these variables.
RUN --mount=type=secret,id=extra_ca \
    cat /etc/ssl/certs/ca-certificates.crt > /etc/ssl/build-ca.pem \
    && if [ -s /run/secrets/extra_ca ]; then cat /run/secrets/extra_ca >> /etc/ssl/build-ca.pem; fi
RUN pip install uv==0.11.21
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra mcp
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra mcp

# runtime: uid 10001, a HOME dir, the venv only. Logs go to stdout; read-only root filesystem
# is fine. Writes no files.
FROM python:3.12.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/specflow
RUN groupadd --gid 10001 specflow \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/specflow \
        --shell /usr/sbin/nologin specflow
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8002
CMD ["specflow", "mcp"]
