"""Linear OAuth 2.0 (authorization code with PKCE S256, ``actor=user``): authorize URL, code exchange, refresh with
refresh-token rotation, revoke, and the viewer read that names the member and the workspace of a new token.

The access token lives about 24 h and the refresh token rotates on every refresh (the caller commits the new one before
it uses the new access token; a lost refresh answer may be retried with the same refresh token for 30 minutes). Every
failure is one ``linear.errors`` class: a dead grant (``invalid_grant``) is ``LinearUnauthorizedError``, a refused app
credential ``LinearForbiddenError``, any other refusal ``LinearInvalidError``, a network failure, a timeout, a 429 or
a 5xx ``LinearUnavailableError``. No error, log line or ``repr`` carries Linear's text, a code, a ``state`` or a token.
The client ignores the proxy variables and ``.netrc`` (``trust_env=False``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, override
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from linear.errors import (
    LinearForbiddenError,
    LinearInvalidError,
    LinearUnauthorizedError,
    LinearUnavailableError,
)
from linear.models import Organization, Viewer

AUTHORIZE_URL: Final = "https://linear.app/oauth/authorize"
GRANT_URL: Final = "https://api.linear.app/oauth/token"
REVOKE_URL: Final = "https://api.linear.app/oauth/revoke"
GRAPHQL_URL: Final = "https://api.linear.app/graphql"
SCOPES: Final = ("read", "write")
TIMEOUT_SECONDS: Final = 15
_VIEWER_QUERY: Final = "query { viewer { id name organization { id name urlKey } } }"
_DEAD_GRANT: Final = "invalid_grant"
_REFUSED_CLIENT: Final = frozenset({"invalid_client", "unauthorized_client"})
_AUTH_STATUSES: Final = (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN)


@dataclass(frozen=True)
class TokenGrant:
    """A token response: the pair, its lifetime in seconds and the scopes granted (``read,write``)."""

    access_token: SecretStr = field(repr=False)
    refresh_token: SecretStr = field(repr=False)
    expires_in: int
    scope: str


def _answer(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _scope_of(value: Any) -> str:
    """Linear answers the scopes as a list or as one text separated by spaces or commas."""

    parts = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    return ",".join(str(part) for part in parts)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class LinearOAuthClient:
    """The token endpoint client of one OAuth app."""

    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=transport, trust_env=False, follow_redirects=False
        )

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def authorize_url(self, state: str, code_challenge: str) -> str:
        """The consent URL: ``scope=read,write``, ``response_type=code``, PKCE S256, ``actor=user`` and ``state``."""

        query = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "actor": "user",
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.post(url, **kwargs)
        except httpx.HTTPError:
            raise LinearUnavailableError from None
        if response.is_server_error or response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise LinearUnavailableError(http_status=response.status_code)
        return response

    async def _grant(self, form: dict[str, str]) -> TokenGrant:
        body = {"client_id": self._client_id, "client_secret": self._client_secret.get_secret_value(), **form}
        response = await self._post(GRANT_URL, data=body, headers={"Accept": "application/json"})
        answer = _answer(response)
        if not response.is_success:
            error = answer.get("error")
            if error == _DEAD_GRANT:
                raise LinearUnauthorizedError(http_status=response.status_code)
            if error in _REFUSED_CLIENT or response.status_code in _AUTH_STATUSES:
                raise LinearForbiddenError(http_status=response.status_code)
            raise LinearInvalidError(http_status=response.status_code)
        access, refresh = _text(answer.get("access_token")), _text(answer.get("refresh_token"))
        expires_in = answer.get("expires_in")
        if access is None or refresh is None or not isinstance(expires_in, int) or expires_in <= 0:
            raise LinearInvalidError(http_status=response.status_code)  # no refresh token: the app does not rotate
        return TokenGrant(SecretStr(access), SecretStr(refresh), expires_in, _scope_of(answer.get("scope")))

    async def exchange(self, code: str, code_verifier: str) -> TokenGrant:
        """Trade an authorization code (and the PKCE verifier) for the token pair."""

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "code_verifier": code_verifier,
        }
        return await self._grant(form)

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        """Trade a refresh token for a new pair; the old refresh token is dead after this."""

        return await self._grant({"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()})

    async def revoke(self, token: SecretStr, *, hint: str = "access_token") -> None:
        """Revoke an access or refresh token (``hint`` is Linear's ``token_type_hint``); raises ``LinearError`` when
        Linear refuses (the caller treats a disconnect as best effort)."""

        form = {"token": token.get_secret_value(), "token_type_hint": hint}
        response = await self._post(REVOKE_URL, data=form, headers={"Accept": "application/json"})
        if not response.is_success:
            raise LinearInvalidError(http_status=response.status_code)

    async def viewer(self, access_token: SecretStr) -> Viewer:
        """The member and the workspace a fresh access token belongs to."""

        response = await self._post(
            GRAPHQL_URL,
            json={"query": _VIEWER_QUERY},
            headers={"Authorization": f"Bearer {access_token.get_secret_value()}"},
        )
        if response.status_code in _AUTH_STATUSES:
            raise LinearUnauthorizedError(http_status=response.status_code)
        data = _answer(response).get("data")
        viewer = data.get("viewer") if isinstance(data, dict) else None
        organization = viewer.get("organization") if isinstance(viewer, dict) else None
        if not isinstance(viewer, dict) or not isinstance(organization, dict):
            raise LinearUnauthorizedError(http_status=response.status_code)
        user_id, name = _text(viewer.get("id")), _text(viewer.get("name"))
        org_id, org_name, url_key = (_text(organization.get(key)) for key in ("id", "name", "urlKey"))
        if user_id is None or name is None or org_id is None or org_name is None or url_key is None:
            raise LinearInvalidError(http_status=response.status_code)
        return Viewer(user_id, name, Organization(org_id, org_name, url_key))

    async def aclose(self) -> None:
        await self._http.aclose()
