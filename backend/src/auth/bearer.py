"""The ``Authorization: Bearer`` parser, free of any web framework.

Shared by the REST dependency and the MCP server (FR-MCP-AUTH-07); importing it
must not load FastAPI.
"""

from __future__ import annotations

from typing import Final

from auth.errors import invalid_token
from auth.verifiers import MAX_TOKEN_BYTES

_TOKEN_CHARS: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.=+/~")


def bearer_token(authorization: str | None) -> str:
    """The token from an ``Authorization: Bearer <token>`` value, else 401."""

    if not authorization:
        raise invalid_token("missing_header")
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > MAX_TOKEN_BYTES or not set(token) <= _TOKEN_CHARS:
        raise invalid_token("malformed_header")
    return token
