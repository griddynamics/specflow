"""The API client of the MCP role: one ``httpx.AsyncClient`` to ``API_INTERNAL_URL`` (architecture.md, Components).

One total timeout per call, no retries, no redirects and no proxy variables of the host: the caller's token goes to
the one configured address and nowhere else. A call never raises for a transport failure; it answers an
``ApiResult`` that names the failure, and ``errors.from_api`` turns that into the tool result.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import Final

import httpx
from starlette.requests import Request

from agent_contracts import USER_ID_HEADER
from settings import McpRoleSettings

API_TIMEOUT_SECONDS: Final = 25  # one total timeout per call, below the ingress timeout of 30 s
READYZ_TIMEOUT_SECONDS: Final = 3  # below the probe timeouts of compose (4 s) and Helm (5 s)
READYZ_PATH: Final = "/readyz"
REQUEST_ID_HEADER: Final = "X-Request-ID"

ApiTransport = httpx.AsyncBaseTransport  # how the requests travel; the seam of the tests

API_UNREACHABLE: Final = "ApiUnreachable"
API_TIMEOUT: Final = "ApiTimeout"
API_PROTOCOL_ERROR: Final = "ApiProtocolError"


@dataclass(frozen=True)
class ApiResult:
    """What one API call answered: the status, the parsed JSON body (``None`` when it is absent or not JSON) and
    the provisioned user id of ``X-SpecFlow-User-Id`` (``None`` when absent or not a UUID); or ``failure``, the
    ``error_type`` of a call that got no answer."""

    status: int | None = None
    body: object = None
    user_id: str | None = None
    failure: str | None = None


def _user_id(value: str | None) -> str | None:
    """The canonical form of a UUID header value, else ``None``: header text never reaches a log as sent."""

    try:
        return str(uuid.UUID(value)) if value else None
    except ValueError:
        return None


def _body(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:  # not JSON (also not UTF-8)
        return None


class ApiClient:
    """``call`` an agent route with the caller's token; ``readyz`` the API's probe without one."""

    def __init__(self, base_url: str, *, transport: ApiTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=API_TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,  # the token and the address are ours: never through HTTP_PROXY of the host
            follow_redirects=False,
        )
        # a jar whose policy refuses every domain: nothing a proxy or balancer sets is stored or replayed
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, route: str, params: Mapping[str, str], authorization: str, request_id: str) -> ApiResult:
        """One ``GET`` on ``route`` with the caller's ``Authorization`` header unchanged and ``request_id``."""

        return await self._get(route, params, {"Authorization": authorization, REQUEST_ID_HEADER: request_id})

    async def readyz(self) -> ApiResult:
        """``GET /readyz``: no token, no query."""

        return await self._get(READYZ_PATH, {}, {}, READYZ_TIMEOUT_SECONDS)

    async def _get(
        self, path: str, params: Mapping[str, str], headers: Mapping[str, str], seconds: float = API_TIMEOUT_SECONDS
    ) -> ApiResult:
        try:
            async with asyncio.timeout(seconds):  # the httpx timeout is per phase; this one is total
                sent = {**headers, "Accept": "application/json"}
                response = await self._client.get(path, params=params, headers=sent)
        except (httpx.TimeoutException, TimeoutError):
            return ApiResult(failure=API_TIMEOUT)
        except httpx.NetworkError:  # connect, read, write or close failed: nobody answered
            return ApiResult(failure=API_UNREACHABLE)
        except httpx.HTTPError:  # a broken exchange (protocol violation, bad encoding, redirect loop)
            return ApiResult(failure=API_PROTOCOL_ERROR)
        return ApiResult(
            status=response.status_code,
            body=_body(response),
            user_id=_user_id(response.headers.get(USER_ID_HEADER)),
        )


def build_api_client(settings: McpRoleSettings, *, transport: ApiTransport | None = None) -> ApiClient:
    """The client of ``API_INTERNAL_URL`` (``transport`` is the seam of the tests)."""

    return ApiClient(settings.api_internal.url, transport=transport)


def request_api_client(request: Request) -> ApiClient:
    """The client the lifespan built (``app.state.api_client``)."""

    client: ApiClient = request.app.state.api_client
    return client
