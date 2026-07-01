"""Service module for communicating with the SpecFlow backend API."""

import logging
import os
import httpx
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class BackendContractRejection(Exception):
    """A structured contract rejection returned by the backend (HTTP 400 with a
    ``detail`` object carrying a ``code``).

    Carried verbatim so callers can surface the same actionable message + code the
    MCP-side precheck produces — both gates must return the same error shape
    (CLAUDE.md run_generation rejection contract). Without this, a backend-side
    rejection collapses into a generic "couldn't reach the server" message.
    """

    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("error") or detail.get("message") or "Contract rejected.")


def _parse_contract_rejection(response: httpx.Response) -> dict | None:
    """Return the structured rejection detail if the response is one, else None."""
    try:
        body = response.json()
    except Exception:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("code"):
        return detail
    return None


class SpecFlowBackendService:
    """Makes requests to the SpecFlow backend API."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
        self.api_key = os.getenv("SPECFLOW_API_KEY")
        self.user_email = os.getenv("USER_EMAIL")
        logger.info("SpecFlowBackendService initialized with base_url: %s", self.base_url)

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.user_email:
            headers["X-User-Email"] = self.user_email
        return headers

    def _client(
        self,
        timeout_seconds: float,
        connect_timeout_seconds: float = 10.0,
        limits: httpx.Limits | None = None,
    ) -> httpx.AsyncClient:
        timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        if limits is not None:
            return httpx.AsyncClient(timeout=timeout, limits=limits)
        return httpx.AsyncClient(timeout=timeout)

    async def call_backend(
        self,
        endpoint: str,
        method: str = "POST",
        json_data: dict[str, Any] | None = None,
        timeout_seconds: float = 600.0,
        connect_timeout_seconds: float = 10.0,
    ) -> str:
        """Call backend; return response text. Raises on HTTP errors (mirrors upload_file /
        post_form_data) instead of returning a non-JSON error string — callers that blindly
        json.loads() the return value would otherwise fail with a misleading
        "Expecting value" error that hides the actual backend detail.
        """
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()
        async with self._client(timeout_seconds, connect_timeout_seconds) as client:
            m = method.upper()
            if m == "POST":
                response = await client.post(url, json=json_data, headers=headers)
            elif m == "GET":
                response = await client.get(url, headers=headers)
            elif m == "DELETE":
                response = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                rejection = _parse_contract_rejection(e.response)
                if rejection is not None:
                    raise BackendContractRejection(rejection) from e
                detail = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
                logger.error("HTTP error calling backend %s: %s", endpoint, detail)
                raise Exception(f"Backend returned {detail}") from e
            except httpx.HTTPError as e:
                logger.error("HTTP error calling backend %s: %s, type: %s", endpoint, e, type(e).__name__)
                raise Exception(f"Failed to call backend: {e}") from e
            return response.text

    async def call_backend_bytes(
        self,
        endpoint: str,
        method: str = "GET",
        timeout_seconds: float = 600.0,
        connect_timeout_seconds: float = 10.0,
    ) -> bytes:
        """Call backend; return raw response bytes. Raises on HTTP errors."""
        url = f"{self.base_url}{endpoint}"
        async with self._client(timeout_seconds, connect_timeout_seconds) as client:
            if method.upper() != "GET":
                raise ValueError(f"call_backend_bytes: unsupported method {method}")
            response = await client.get(url, headers=self._get_headers())
            response.raise_for_status()
            return response.content

    async def stream_sse(
        self,
        endpoint: str,
        connect_timeout_seconds: float = 10.0,
    ) -> AsyncIterator[str]:
        """Yield SSE ``data:`` payload strings from a long-lived streaming GET.

        Used by the TUI workspace drill-in to live-tail agent messages. The read
        timeout is disabled (``None``) because the stream is intentionally
        long-lived and kept alive by server heartbeats; only the initial connect
        is bounded. Comment lines (``:`` heartbeats) and blank separators are
        skipped — callers receive one string per ``data:`` event. Raises on
        connect / HTTP errors so callers can decide how to surface them.
        """
        url = f"{self.base_url}{endpoint}"
        timeout = httpx.Timeout(None, connect=connect_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url, headers=self._get_headers()) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        yield line[len("data:"):].strip()

    async def upload_file(
        self,
        endpoint: str,
        file_data: bytes,
        filename: str,
        form_data: dict[str, Any],
        timeout_seconds: float = 300.0,
        connect_timeout_seconds: float = 10.0,
    ) -> str:
        """Upload a file via multipart/form-data. Raises on HTTP errors."""
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        url = f"{self.base_url}{endpoint}"
        logger.info("Uploading file to %s (size: %d bytes, timeout: %ss)", url, len(file_data), timeout_seconds)
        try:
            async with self._client(timeout_seconds, connect_timeout_seconds, limits) as client:
                response = await client.post(
                    url,
                    files={"archive": (filename, file_data, "application/gzip")},
                    data=form_data,
                    headers=self._get_headers(),
                )
                logger.info("Response status: %s, headers: %s", response.status_code, dict(response.headers))
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as e:
            rejection = _parse_contract_rejection(e.response)
            if rejection is not None:
                raise BackendContractRejection(rejection) from e
            detail = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
            logger.error("HTTP error uploading file to %s: %s", endpoint, detail)
            raise Exception(f"Backend returned HTTP {e.response.status_code}: {detail}") from e
        except httpx.HTTPError as e:
            logger.error("HTTP error uploading file to %s: %s, type: %s", endpoint, e, type(e).__name__)
            raise Exception(f"Failed to upload file: {e}") from e

    async def post_form_data(
        self,
        endpoint: str,
        form_data: dict[str, Any],
        timeout_seconds: float = 600.0,
        connect_timeout_seconds: float = 10.0,
    ) -> str:
        """POST form data (no file). Raises on HTTP errors."""
        url = f"{self.base_url}{endpoint}"
        logger.info("Posting form data to %s", url)
        try:
            async with self._client(timeout_seconds, connect_timeout_seconds) as client:
                response = await client.post(url, data=form_data, headers=self._get_headers())
                logger.info("Response status: %s", response.status_code)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as e:
            rejection = _parse_contract_rejection(e.response)
            if rejection is not None:
                raise BackendContractRejection(rejection) from e
            detail = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
            logger.error("HTTP error posting form data to %s: %s", endpoint, detail)
            raise Exception(f"Backend returned HTTP {e.response.status_code}: {detail}") from e
        except httpx.HTTPError as e:
            logger.error("HTTP error posting form data to %s: %s, type: %s", endpoint, e, type(e).__name__)
            raise Exception(f"Failed to post form data: {e}") from e


_backend_service = SpecFlowBackendService()
logger.info("Backend service singleton created with URL: %s", _backend_service.base_url)


async def call_backend_endpoint(
    endpoint: str,
    method: str = "POST",
    json_data: dict[str, Any] | None = None,
    timeout_seconds: float = 600.0,
    connect_timeout_seconds: float = 10.0,
) -> str:
    """Call backend; return response text. Use call_backend_endpoint_bytes() for binary downloads."""
    return await _backend_service.call_backend(
        endpoint=endpoint, method=method, json_data=json_data,
        timeout_seconds=timeout_seconds, connect_timeout_seconds=connect_timeout_seconds,
    )


async def call_backend_endpoint_bytes(
    endpoint: str,
    timeout_seconds: float = 600.0,
    connect_timeout_seconds: float = 10.0,
) -> bytes:
    """Call backend; return raw bytes for binary downloads. Raises on HTTP errors."""
    return await _backend_service.call_backend_bytes(
        endpoint=endpoint, timeout_seconds=timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
    )


async def stream_backend_sse(
    endpoint: str,
    connect_timeout_seconds: float = 10.0,
) -> AsyncIterator[str]:
    """Yield SSE ``data:`` payload strings from a streaming backend endpoint."""
    async for data in _backend_service.stream_sse(
        endpoint=endpoint, connect_timeout_seconds=connect_timeout_seconds
    ):
        yield data


async def upload_file_to_backend(
    endpoint: str,
    file_data: bytes,
    filename: str,
    form_data: dict[str, Any],
    timeout_seconds: float = 300.0,
    connect_timeout_seconds: float = 10.0,
) -> str:
    """Upload a file via multipart/form-data to the backend."""
    return await _backend_service.upload_file(
        endpoint=endpoint, file_data=file_data, filename=filename, form_data=form_data,
        timeout_seconds=timeout_seconds, connect_timeout_seconds=connect_timeout_seconds,
    )


async def post_form_data_to_backend(
    endpoint: str,
    form_data: dict[str, Any],
    timeout_seconds: float = 600.0,
    connect_timeout_seconds: float = 10.0,
) -> str:
    """POST form data to the backend."""
    return await _backend_service.post_form_data(
        endpoint=endpoint, form_data=form_data,
        timeout_seconds=timeout_seconds, connect_timeout_seconds=connect_timeout_seconds,
    )
