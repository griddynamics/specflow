"""``create_mcp_app(settings)``: the guard, ``/mcp``, the PRM, the probes and the lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
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

from api.mcp.auth import McpGuard, ProtectedResourceMetadata, metadata_endpoint
from api.mcp.http import CheckBody, McpStarlette, ReadyzBody, error_response, required
from api.mcp.registry import install
from api.mcp.runtime import RuntimeFactory, build_runtime, request_runtime
from auth.verifiers import build_verifier
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from settings import MCP_PATH, PRM_PATH, RoleSettings

# Module-level logger first: the central logger owns the root handler before MCPServer
# configures stdlib logging (a no-op then).
logger = get_logger("api.mcp.app")

MAX_REQUEST_BODY_BYTES: Final = 1024 * 1024  # tool arguments are tiny (FR-MCP-OPS-05)
_CODES: Final = {404: "not_found", 405: "method_not_allowed"}


def install_no_tools(server: MCPServer) -> None:
    """A ``tools`` hook that registers nothing (tests of the transport and the guard)."""


async def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _readyz(request: Request) -> JSONResponse:
    report = await request_runtime(request).readiness.snapshot()
    body = ReadyzBody(
        status="ready" if report.all_ok else "not_ready",
        checks={name: CheckBody(ok=r.ok, detail=r.detail, duration_ms=r.duration_ms) for name, r in report.items()},
    )
    return JSONResponse(body.model_dump(), status_code=200 if report.all_ok else 503)


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
    settings: RoleSettings,
    *,
    runtime_factory: RuntimeFactory = build_runtime,
    tools: Callable[[MCPServer], None] = install,
) -> Starlette:
    """Build the app; no connection opens until the lifespan starts.

    ``tools`` registers tools on the server before the app is built; the default is the tool registry.
    """

    auth = required(settings.auth, "the mcp role requires auth settings")
    mcp = required(settings.mcp, "the mcp role requires mcp settings")
    dev = auth.mode == "dev"  # dev mode: no PRM route, a challenge without resource_metadata
    server = MCPServer("SpecFlow")  # never auth= or token_verifier=: the guard owns 401 and 403
    tools(server)
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
                runtime = await runtime_factory(settings)
                stack.push_async_callback(runtime.aclose)
                await stack.enter_async_context(transports.run())  # not `session`: the Cypher semgrep rule
            except Exception as error:
                log_event(logger, ERROR, "mcp.startup.failed", error_type=type(error).__name__, exc_info=True)
                raise
            app.state.runtime = runtime
            if dev:
                log_event(logger, WARNING, "mcp.auth.dev_mode", "AUTH_MODE=dev: locally signed dev tokens are accepted")
            log_event(logger, INFO, "mcp.startup.completed", port=mcp.port, auth_mode=auth.mode)
            yield
        log_event(logger, INFO, "mcp.shutdown.completed", port=mcp.port)

    routes = [
        Route(MCP_PATH, StreamableHTTPASGIApp(transports), methods=["POST"]),  # GET/DELETE: 405
        Route("/healthz", _healthz),
        Route("/readyz", _readyz),
    ]
    if not dev:  # dev mode has no PRM (404) and a challenge without resource_metadata
        prm = metadata_endpoint(
            ProtectedResourceMetadata(
                resource=mcp.resource_url, authorization_servers=[required(auth.issuer, "oidc mode has an issuer")]
            )
        )
        routes += [Route(f"{PRM_PATH}/mcp", prm), Route(PRM_PATH, prm)]
    app = McpStarlette(
        routes=routes,
        middleware=[
            Middleware(
                McpGuard,
                verifier=build_verifier(auth, audience=mcp.audience, client_ids=mcp.client_ids),
                resource_metadata=None if dev else mcp.prm_url,
            )
        ],
        exception_handlers={HTTPException: _http_error, Exception: _unhandled},
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False  # "/mcp/" is anything else: 404 JSON, no Host-built redirect
    return app
