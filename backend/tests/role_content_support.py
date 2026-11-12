"""What each role image holds: entrypoint modules, distributions present and absent (one source).

Read by the docker-gated image test (``test_specflow_images.py``) and by the offline role tests
(``test_specflow_role_imports.py``, ``test_specflow_role_packaging.py``).
"""

from __future__ import annotations

# Role -> (entrypoint modules that import, distributions that must be installed, distributions that must be absent).
# Absence is checked by DISTRIBUTION name, not by module: `google.cloud.storage` ships as tracked `.pyi` stubs in every
# image, and `opentelemetry`, `httpx2`, `anyio` and the module `dotenv` (distribution `python-dotenv`, pulled in by
# pydantic-settings) are legitimately present in mcp and worker-agents. `dotenv` below is the separate PyPI distribution
# of that name, which only the parse extra installs; Docling's distribution is `docling-slim`. `mcp` and `uvicorn` are
# not in the absent sets of the other roles: openai-agents requires `mcp`, which requires `uvicorn`.
ROLE_IMAGE_CONTENT = {
    "api": (
        ("api.server", "api.app", "db.migrate", "health"),
        ("fastapi", "uvicorn", "sqlalchemy", "alembic", "asyncpg", "temporalio", "openai-agents", "numpy", "neo4j"),
        ("anthropic", "claude-agent-sdk", "langfuse", "dotenv", "docling-slim", "docling-core"),
    ),
    "worker-agents": (
        ("temporal.agents.registration", "temporal.worker", "health"),
        ("temporalio", "openai", "openai-agents", "sqlalchemy", "asyncpg", "neo4j", "boto3", "google-cloud-storage"),
        ("anthropic", "claude-agent-sdk", "langfuse", "dotenv", "docling-slim", "docling-core", "fastapi"),
    ),
    "worker-parse": (
        ("temporal.documents.registration", "temporal.worker", "health"),
        ("temporalio", "sqlalchemy", "anthropic", "claude-agent-sdk", "langfuse", "dotenv", "docling-slim", "boto3"),
        ("fastapi", "alembic"),
    ),
    "mcp": (
        ("mcp_server.server", "mcp_server.app"),
        ("mcp", "starlette", "uvicorn", "httpx", "pyjwt", "pydantic", "pydantic-settings", "pyyaml"),
        (
            "temporalio",
            "openai",
            "openai-agents",
            "sqlalchemy",
            "asyncpg",
            "alembic",
            "fastapi",
            "neo4j",
            "boto3",
            "google-cloud-storage",
            "google-api-core",
            "anthropic",
            "claude-agent-sdk",
            "langfuse",
            "docling-slim",
            "docling-core",
            "numpy",
            "google-api-python-client",
        ),
    ),
}
