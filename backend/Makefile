# Relative to backend/. The root Makefile passes an absolute path down.
ENV_FILE ?= ../.env.local

.PHONY: setup lint unit-test check \
	db-upgrade db-revision api worker temporal-check image-test rss-test

setup:
	uv sync --extra gcp --extra aws

lint:
	uv run ruff check src examples tests migrations

unit-test:
	uv run pytest

check: lint unit-test

db-upgrade:
	uv run --env-file $(ENV_FILE) alembic upgrade head

db-revision:
	@test -n "$(MSG)" || { echo 'MSG is required: make db-revision MSG="describe the change"' >&2; exit 1; }
	uv run --env-file $(ENV_FILE) alembic revision -m "$(MSG)" $(ALEMBIC_ARGS)

# Host processes (settings from the env file: localhost values).
api:
	uv run --env-file $(ENV_FILE) specflow api

worker:
	uv run --env-file $(ENV_FILE) specflow worker

# Reaches TEMPORAL_ADDRESS in TEMPORAL_NAMESPACE and nothing else; exit 0 = reachable.
define TEMPORAL_CHECK
import asyncio, os, sys
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

address = os.environ.get("TEMPORAL_ADDRESS", "")
namespace = os.environ.get("TEMPORAL_NAMESPACE", "")
if not address or not namespace:
    sys.exit("TEMPORAL_ADDRESS and TEMPORAL_NAMESPACE must be set")

async def probe():
    client = await Client.connect(address, namespace=namespace)
    await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=namespace))

try:
    asyncio.run(asyncio.wait_for(probe(), 10))
except Exception as exc:
    sys.exit("temporal unreachable: " + type(exc).__name__)
endef
export TEMPORAL_CHECK

temporal-check:
	uv run --env-file $(ENV_FILE) python -c "$$TEMPORAL_CHECK"

# Image tests need docker and the built images (root `make images` builds them first).
image-test:
	SPECFLOW_TEST_IMAGES=1 uv run pytest tests/test_specflow_images.py -q -rs

# AC-4.2 on Linux: the upload memory test in a throwaway Python container (no image of
# ours). backend/ is mounted read-only; the virtualenv and caches stay in the container.
rss-test:
	docker run --rm -v "$(CURDIR):/src:ro" -w /src \
		-e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uv-cache \
		-e PYTHONDONTWRITEBYTECODE=1 -e SPECFLOW_IMAGE_RSS=1 \
		python:3.12.13-slim-bookworm sh -c \
		"pip install -q uv==0.11.21 && uv sync -q --frozen --no-editable --extra gcp --extra aws && uv run --no-sync pytest -p no:cacheprovider -q -rs tests/test_specflow_images.py -k rss"
