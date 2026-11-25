"""Linear OAuth 2.0 (authorization code with PKCE S256, ``actor=user``): authorize URL, code exchange, refresh with
refresh-token rotation, revoke. STUB of the foundation: stream S1 fills it (plans/LINEAR/LINEAR-PLAN.md section 5).

Intended contract. ``authorize_url`` is ``https://linear.app/oauth/authorize``; ``exchange``, ``refresh`` and ``revoke``
POST to ``GRANT_URL`` and ``/oauth/revoke``. The access token lives 24 h and the refresh token
rotates on every refresh, so a refresh must run under the shared token lock (``integrations.tokens``). No error carries
Linear's text, a code, a ``state`` or a token."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, override

from pydantic import SecretStr

AUTHORIZE_URL: Final = "https://linear.app/oauth/authorize"
GRANT_URL: Final = "https://api.linear.app/oauth/token"
REVOKE_URL: Final = "https://api.linear.app/oauth/revoke"
SCOPES: Final = ("read", "write")


@dataclass(frozen=True)
class TokenGrant:
    """A token response: the pair, its lifetime in seconds and the scopes granted."""

    access_token: SecretStr = field(repr=False)
    refresh_token: SecretStr = field(repr=False)
    expires_in: int
    scope: str


class LinearOAuthClient:
    """The token endpoint client of one OAuth app. STUB: every method raises ``NotImplementedError`` (stream S1)."""

    def __init__(self, client_id: str, client_secret: SecretStr, redirect_uri: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def authorize_url(self, state: str, code_challenge: str) -> str:
        """The consent URL with ``scope=read,write``, ``response_type=code``, PKCE S256 and ``state``."""

        raise NotImplementedError("linear.oauth is filled by stream S1")

    async def exchange(self, code: str, code_verifier: str) -> TokenGrant:
        """Trade an authorization code (and the PKCE verifier) for the token pair."""

        raise NotImplementedError("linear.oauth is filled by stream S1")

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        """Trade a refresh token for a new pair; the old refresh token is dead after this."""

        raise NotImplementedError("linear.oauth is filled by stream S1")

    async def revoke(self, token: SecretStr) -> None:
        """Revoke an access or refresh token (best effort on disconnect)."""

        raise NotImplementedError("linear.oauth is filled by stream S1")

    async def aclose(self) -> None:
        """Nothing is held open yet."""
