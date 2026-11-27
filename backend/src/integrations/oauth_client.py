"""The part every OAuth integration client (``github.client``, ``box.client``) shares: the app credentials and one
``httpx`` client that ignores the proxy variables and ``.netrc`` (``trust_env=False``), never follows a redirect by
itself, and never shows a credential in its ``repr``. A provider client subclasses it, sets ``TIMEOUT_SECONDS`` and adds
its own requests and errors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ClassVar, override

import httpx
from pydantic import SecretStr


class OAuthHttpClient:
    """The credentials of an OAuth app and its HTTP transport; ``aclose`` ends the connections."""

    TIMEOUT_SECONDS: ClassVar[float] = 30

    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._client_id = client_id
        self._secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(
            timeout=self.TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def aclose(self) -> None:
        await self._http.aclose()
