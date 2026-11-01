"""ArcadeDB admin client: the small HTTP API calls the adapter needs besides Bolt.

Bolt cannot do four things: report the real server version (``dbms.components()`` answers a fake Neo4j
5.26), say whether a database exists and create one, create vector and fulltext indexes (Cypher
``CREATE VECTOR INDEX`` and ``CREATE FULLTEXT INDEX`` are rejected on 26.10; SQL does it), and report an
index's dimension and similarity. All four go through the HTTP API (port 2480) with the same login.

The client never puts the password into an error or a log line; an error carries a neutral code, the HTTP
status and the server's exception class name only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from graph.core.errors import ConfigurationError, InfrastructureUnavailableError
from graph.core.store import (
    STORE_ACCESS_DENIED,
    STORE_AUTH_REJECTED,
    STORE_DATABASE_NOT_FOUND,
    STORE_UNREACHABLE,
    STORE_VERSION_UNSUPPORTED,
)
from graph.stores.arcadedb.statements import sql as sql_text
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.stores.arcadedb.admin")

BACKEND: Final = "arcadedb"
LOOPBACK: Final = frozenset({"localhost", "::1"})
DEFAULT_ADMIN_PORT: Final = 2480
REQUEST_TIMEOUT_SECONDS: Final = 60.0
MINIMUM_VERSION: Final = (26, 10, 0)
STORE_VERSION_UNKNOWN: Final = "store_version_unknown"
STORE_URI_INVALID: Final = "store_uri_invalid"
SCHEMA_SETUP_FAILED: Final = "schema_setup_failed"
_VERSION: Final = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")
_DATABASE_NAME: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BOLT_SCHEMES: Final = {"bolt": "http", "neo4j": "http", "bolt+s": "https", "neo4j+s": "https"}
_UNVERIFIED_TLS_SCHEMES: Final = frozenset({"bolt+ssc", "neo4j+ssc"})


def admin_url(uri: str, admin_uri: str | None = None) -> str:
    """The base URL of the HTTP API: ``admin_uri`` when given, else the Bolt host on port 2480.

    ``bolt://db:7687`` gives ``http://db:2480``; ``bolt+s://`` and ``neo4j+s://`` give ``https``.
    Raises ``ConfigurationError("store_uri_invalid")`` for a URI that has no host or an unknown scheme."""

    if admin_uri:
        parts = urlsplit(admin_uri)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigurationError(STORE_URI_INVALID, store=BACKEND, setting="admin_uri")
        return admin_uri.rstrip("/")
    parts = urlsplit(uri)
    scheme = _BOLT_SCHEMES.get(parts.scheme)
    if scheme is None and parts.scheme in _UNVERIFIED_TLS_SCHEMES:
        scheme = "https"
    if scheme is None or not parts.hostname:
        raise ConfigurationError(STORE_URI_INVALID, store=BACKEND, setting="uri")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{scheme}://{host}:{DEFAULT_ADMIN_PORT}"


def parse_version(text: str) -> tuple[int, int, int]:
    """``(major, minor, patch)`` of a server version text such as ``26.10.2 (build e85469...)``."""

    found = _VERSION.match(text.strip())
    if found is None:
        raise ConfigurationError(STORE_VERSION_UNKNOWN, store=BACKEND)
    major, minor, patch = found.groups()
    return int(major), int(minor), int(patch or 0)


def check_database_name(name: str) -> str:
    """``name`` when it is a database name safe to put in a URL and a command, else ``ConfigurationError``."""

    if not _DATABASE_NAME.fullmatch(name):
        raise ConfigurationError("store_database_name_invalid", store=BACKEND)
    return name


def _warn_when_clear_text(base_url: str) -> None:
    """Log (event only, no value) that the admin login travels in clear text to a host that is not loopback."""

    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if parts.scheme == "http" and host not in LOOPBACK and not host.startswith("127."):
        log_event(logger, WARNING, "graph.store.admin_clear_text", store=BACKEND)


class AdminClient:
    """The HTTP admin API of one ArcadeDB server, with the login of the store."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool = True,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(user, password.get_secret_value()),
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
            verify=verify,
            trust_env=False,  # the login is HTTP Basic: never through HTTP_PROXY / HTTPS_PROXY of the host
        )
        _warn_when_clear_text(base_url)

    async def close(self) -> None:
        await self._client.aclose()

    async def server_version(self) -> tuple[int, int, int]:
        """The real server version (``GET /api/v1/server``)."""

        body = await self._send("GET", "/api/v1/server")
        return parse_version(str(body.get("version", "")))

    async def require_version(self, minimum: tuple[int, ...] = MINIMUM_VERSION) -> tuple[int, int, int]:
        """The server version; ``ConfigurationError("store_version_unsupported")`` when older than ``minimum``."""

        found = await self.server_version()
        if found < tuple(minimum):
            raise ConfigurationError(
                STORE_VERSION_UNSUPPORTED,
                store=BACKEND,
                found=".".join(map(str, found)),
                minimum=".".join(map(str, minimum)),
            )
        return found

    async def database_exists(self, name: str) -> bool:
        """Whether the server has the database ``name`` (``GET /api/v1/exists/{name}``)."""

        body = await self._send("GET", f"/api/v1/exists/{check_database_name(name)}")
        return bool(body.get("result"))

    async def create_database(self, name: str) -> None:
        """Create the database ``name`` (``create database`` server command); needs a login allowed to."""

        await self._send("POST", "/api/v1/server", {"command": f"create database {check_database_name(name)}"})

    async def ensure_database(self, name: str) -> bool:
        """Create the database ``name`` unless it exists; return whether it was created."""

        if await self.database_exists(name):
            return False
        try:
            await self.create_database(name)
        except ConfigurationError as error:
            if error.code != SCHEMA_SETUP_FAILED or not await self.database_exists(name):
                raise
            return False  # created by a concurrent start
        return True

    async def sql(self, database: str, command: str) -> list[dict[str, Any]]:
        """Run one SQL command in ``database`` and return its result rows."""

        body = await self._send(
            "POST", f"/api/v1/command/{check_database_name(database)}", {"language": "sql", "command": command}
        )
        result = body.get("result", [])
        return [dict(row) for row in result] if isinstance(result, list) else []

    async def types(self, database: str) -> list[dict[str, Any]]:
        """The schema's types with their properties and indexes (``SELECT FROM schema:types``)."""

        return await self.sql(database, sql_text("schema_types"))

    async def _send(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=payload)
        except httpx.HTTPError as error:
            raise InfrastructureUnavailableError(
                STORE_UNREACHABLE, store=BACKEND, error_type=type(error).__name__
            ) from None
        status = response.status_code
        if status == HTTPStatus.UNAUTHORIZED:
            raise ConfigurationError(STORE_AUTH_REJECTED, store=BACKEND)
        if status == HTTPStatus.FORBIDDEN:
            raise ConfigurationError(STORE_ACCESS_DENIED, store=BACKEND)
        if status >= HTTPStatus.BAD_REQUEST:
            if _is_missing_database(response):
                raise ConfigurationError(STORE_DATABASE_NOT_FOUND, store=BACKEND)
            raise ConfigurationError(SCHEMA_SETUP_FAILED, store=BACKEND, status=status, error_type=_exception(response))
        if status == HTTPStatus.NO_CONTENT or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            raise ConfigurationError(SCHEMA_SETUP_FAILED, store=BACKEND, status=status) from None
        return body if isinstance(body, dict) else {}


def _exception(response: httpx.Response) -> str | None:
    """The server's exception class name of an error response (never its message)."""

    try:
        body = response.json()
    except ValueError:
        return None
    name = body.get("exception") if isinstance(body, dict) else None
    return name if isinstance(name, str) and re.fullmatch(r"[\w.$]{1,200}", name) else None


def _is_missing_database(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    detail = body.get("detail") if isinstance(body, dict) else None
    return isinstance(detail, str) and re.search(r"Database '.*' does not exist", detail) is not None
