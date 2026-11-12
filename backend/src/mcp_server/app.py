"""``create_mcp_app(settings)``: the guard, ``/mcp``, the PRM, the probes and the lifespan (one ``ApiClient``)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Final, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from auth.verifiers import build_verifier
from logger.logger import ERROR, INFO, get_logger, log_event
from mcp_server import guidance
from mcp_server.api_client import ApiTransport, build_api_client, request_api_client
from mcp_server.auth import McpGuard, ProtectedResourceMetadata, metadata_endpoint
from mcp_server.http import McpStarlette, error_response, required
from mcp_server.readiness import readiness_response
from mcp_server.registry import install
from settings import MCP_PATH, PRM_PATH, McpRoleSettings

# Module-level logger first: the central logger owns the root handler before MCPServer
# configures stdlib logging (a no-op then).
logger = get_logger("mcp_server.app")

MAX_REQUEST_BODY_BYTES: Final = 1024 * 1024  # tool arguments are tiny (FR-MCP-OPS-05)
_CODES: Final = {404: "not_found", 405: "method_not_allowed"}


def install_no_tools(server: MCPServer) -> None:
    """A ``tools`` hook that registers nothing (tests of the transport and the guard)."""


async def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _readyz(request: Request) -> JSONResponse:
    return await readiness_response(request_api_client(request))


async def _http_error(_: Request, exc: Exception) -> Response:
    error = cast(HTTPException, exc)
    code = _CODES.get(error.status_code, "request_failed")
    return error_response(error.status_code, code, "The request cannot be served.", dict(error.headers or {}))


async def _unhandled(request: Request, exc: Exception) -> Response:
    log_event(
        logger,
        ERROR,
        "mcp.request.failed",
        method=request.method,
        path=request.url.path,
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return error_response(500, "internal_error", "The request failed.")


def create_mcp_app(
    settings: McpRoleSettings,
    *,
    transport: ApiTransport | None = None,
    tools: Callable[[MCPServer], None] | None = None,
) -> Starlette:
    """Build the app; no connection opens until the first API call or probe.

    ``transport`` is the seam of the tests: the API client sends its requests through it (an in-process API app, a
    mock) instead of the network.

    ``tools`` registers tools on the server before the app is built; the default is the registry (tools and
    prompts) with the deployment's resource URL, which a hook of the tests cannot know before the settings load.
    """

    auth, mcp = settings.auth, settings.mcp
    resource_url = mcp.resource_url  # the one value of the PRM, the instructions and the prompt
    # never auth= or token_verifier=: the guard owns 401 and 403
    server = MCPServer("SpecFlow", instructions=guidance.server_instructions(resource_url))
    (tools or partial(install, resource_url=resource_url))(server)
    server.streamable_http_app(  # called only to create the session manager; its Starlette is unused
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(mcp.transport_hosts),
            allowed_origins=list(mcp.allowed_origins),
        ),
    )

    transports = server.session_manager

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            try:
                api_client = build_api_client(settings, transport=transport)
                stack.push_async_callback(api_client.aclose)
                await stack.enter_async_context(transports.run())  # not `session`: the Cypher semgrep rule
            except Exception as error:
                log_event(logger, ERROR, "mcp.startup.failed", error_type=type(error).__name__, exc_info=True)
                raise
            app.state.api_client = api_client
            log_event(logger, INFO, "mcp.startup.completed", port=mcp.port)
            yield
        log_event(logger, INFO, "mcp.shutdown.completed", port=mcp.port)

    routes = [
        Route(MCP_PATH, StreamableHTTPASGIApp(transports), methods=["POST"]),  # GET/DELETE: 405
        Route("/healthz", _healthz),
        Route("/readyz", _readyz),
    ]
    prm = metadata_endpoint(
        ProtectedResourceMetadata(
            resource=resource_url, authorization_servers=[required(auth.issuer, "the mcp role has an issuer")]
        )
    )
    routes += [Route(f"{PRM_PATH}/mcp", prm), Route(PRM_PATH, prm)]
    app = McpStarlette(
        routes=routes,
        middleware=[
            Middleware(
                McpGuard,
                verifier=build_verifier(auth, audience=mcp.audience, client_ids=mcp.client_ids),
                resource_metadata=mcp.prm_url,
            )
        ],
        exception_handlers={HTTPException: _http_error, Exception: _unhandled},
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False  # "/mcp/" is anything else: 404 JSON, no Host-built redirect
    return app
