"""Zero-argument app factory for archkit: the API app on fixed, offline settings.

``api.app.create_app`` takes a ``RoleSettings``; archkit calls its factory with no
arguments. The settings are built from this literal mapping, never from the process
environment or ``.env.local``, and no connection is opened (the lifespan is not entered).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Final

from fastapi import FastAPI

from api.app import create_app as create_api_app
from api.errors import required
from auth.dependencies import AGENT_VERIFIER_STATE_ATTR, VERIFIER_STATE_ATTR
from auth.verifiers import build_verifier
from settings import load_settings

ENVIRONMENT: Final = {
    "APP_ENV": "local",
    "DATABASE_HOST": "localhost",
    "DATABASE_PORT": "5432",
    "DATABASE_NAME": "archkit",
    "DATABASE_USER": "archkit",
    "DATABASE_PASSWORD": "archkit-hermetic-value",
    "DATABASE_AGENT_USER": "archkit_agent",
    "STORAGE_BUCKET": "archkit",
    "STORAGE_PREFIX": "archkit/",
    "GOOGLE_CLOUD_PROJECT": "archkit",
    "TEMPORAL_ADDRESS": "localhost:7233",
    "TEMPORAL_NAMESPACE": "archkit",
    "GRAPH_STORE_URI": "bolt://localhost:7687",
    "GRAPH_STORE_USER": "neo4j",
    "GRAPH_STORE_PASSWORD": "archkit-hermetic-value",
    "AGENT_MODEL": "archkit/model",
    "OIDC_ISSUER": "https://idp.archkit.invalid/realms/archkit",
    "AUTH_JWKS_URL": "https://idp.archkit.invalid/realms/archkit/certs",
    "AUTH_ALLOWED_EMAIL_DOMAINS": "example.com",
    "MCP_RESOURCE_URL": "http://localhost:8002/mcp",  # the api role with MCP settings: the agent routes are on
}


def create_app() -> FastAPI:
    """The API app as ``specflow api`` builds it, on the hermetic settings above.

    The lifespan is not entered, so the state it would set is stubbed: the token verifier
    and the agent verifier (JWKS caches fetch lazily, so nothing is fetched here) and an empty runtime,
    enough for requests that fail authentication before any store is used.
    """
    settings = load_settings("api", ENVIRONMENT)
    app = create_api_app(settings)
    setattr(
        app.state, VERIFIER_STATE_ATTR, build_verifier(required(settings.auth, "the api role requires auth settings"))
    )
    mcp = required(settings.mcp, "the archkit api role loads MCP settings")
    setattr(
        app.state,
        AGENT_VERIFIER_STATE_ATTR,
        build_verifier(
            required(settings.auth, "the api role requires auth settings"),
            audience=mcp.audience,
            client_ids=mcp.client_ids,
        ),
    )
    app.state.runtime = SimpleNamespace()
    return app
