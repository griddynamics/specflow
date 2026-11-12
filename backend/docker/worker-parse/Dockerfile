# Worker image for the parse queue: `specflow worker` with WORKER_QUEUES=parse (worker-parse).
# It is the only image that installs the `parse` extra (Docling, torch CPU, OpenCV, PDFium) and
# carries the Docling models, so the api and worker-agents images stay free of them.
# Port 8001 is liveness only (GET /healthz, WORKER_HEALTH_PORT); readiness is the exec
# probe `specflow worker --check`. No migrations here: the API image runs them.
# Build context: backend/ (the last stage is the image; no --target needed):
#   docker build -f backend/docker/worker-parse/Dockerfile -t <registry>/specflow-worker-parse:<tag> backend
# The base tag is a literal (no ARG); uv is pinned to an exact version.
# The build needs the network (PyPI, the PyTorch CPU index, huggingface.co); the container does not:
# the models are in the image and HF_HUB_OFFLINE=1.

# deps: the locked runtime dependencies including the `parse` extra (torch from the CPU index).
FROM python:3.15.0-slim-bookworm AS deps
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system
RUN pip install uv==0.11.21
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra worker --extra parse --extra gcp --extra aws

# models: the Docling models, fetched at build time from Hugging Face at pinned commits
# (docs/file-parsers/engines/docling.md, 'Models'). Layout: the artifacts_path
# folders Docling looks for (<repo with / as -->). Only TableFormer v1 FAST is used (D4); V2 and ACCURATE are not fetched.
#   docling-project/docling-layout-heron  Apache-2.0
#   docling-project/docling-models        CDLA-Permissive-2.0 / Apache-2.0 (tag v2.3.0)
FROM deps AS models
ARG LAYOUT_REVISION=8f39ad3c0b4c58e9c2d2c84a38465abf757272d8
ARG TABLEFORMER_REVISION=fc0f2d45e2218ea24bce5045f58a389aed16dc23
RUN /app/.venv/bin/python - <<'PY'
import os

from huggingface_hub import snapshot_download

for repo, revision, patterns in (
    ("docling-project/docling-layout-heron", os.environ["LAYOUT_REVISION"], ["config.json", "preprocessor_config.json", "model.safetensors"]),
    ("docling-project/docling-models", os.environ["TABLEFORMER_REVISION"], ["model_artifacts/tableformer/fast/*"]),
):
    snapshot_download(repo_id=repo, revision=revision, allow_patterns=patterns, local_dir="/models/" + repo.replace("/", "--"))
PY

# build: the project itself NON-editable, so the runtime stage needs only the virtualenv (no src/ tree).
FROM deps AS build
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra worker --extra parse --extra gcp --extra aws

# runtime: uid 10001, a HOME dir, the venv and the models only. Logs go to stdout; read-only root
# filesystem is fine. worker-parse needs a writable /tmp (the parse writes temp files there).
FROM python:3.15.0-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/specflow \
    HF_HUB_OFFLINE=1 \
    DOCLING_ARTIFACTS_PATH=/opt/docling-models \
    MALLOC_ARENA_MAX=2
RUN groupadd --gid 10001 specflow \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/specflow \
        --shell /usr/sbin/nologin specflow
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=models /models /opt/docling-models
USER 10001:10001
EXPOSE 8001
CMD ["specflow", "worker"]
