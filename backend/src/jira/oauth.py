"""Atlassian OAuth 2.0 (3LO) of the per-user Jira connections (docs/jira-sync/oauth.md): the authorize URL, the
signed ``state``, the token grants, the cloud of the site, the account behind a grant and the refresh-token cipher.

Stand-alone like the rest of ``jira``: the caller passes the client, the key and the site in. No token, code,
``state``, account id or name goes into a log line, an error or a ``repr``; an error is one closed code.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, override
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from integrations.cipher import CipherError
from integrations.cipher import TokenCipher as SharedTokenCipher
from integrations.state import Clock, StateInvalidError
from integrations.state import StateSigner as SharedStateSigner

AUTHORIZE_URL: Final = "https://auth.atlassian.com/authorize"
GRANT_URL: Final = "https://auth.atlassian.com/oauth/token"  # the token endpoint
RESOURCES_URL: Final = "https://api.atlassian.com/oauth/token/accessible-resources"
API_URL: Final = "https://api.atlassian.com/ex/jira/{cloud_id}"
AUDIENCE: Final = "api.atlassian.com"
# The set the site admin approved; changing it makes every user consent again.
SCOPES: Final = (
    "read:jira-work",
    "write:jira-work",
    "read:jira-user",
    "read:board-scope:jira-software",
    "read:sprint:jira-software",
    "write:sprint:jira-software",
    "write:board-scope:jira-software",
    "read:page:confluence",
    "read:hierarchical-content:confluence",
    "read:folder:confluence",
    "read:space:confluence",
    "read:attachment:confluence",
    "offline_access",
)
TIMEOUT_SECONDS: Final = 30
REFRESH_TIMEOUT_SECONDS: Final = 10  # shorter: a refresh holds the row lock and a pass waits on it
INVALID_GRANT: Final = "invalid_grant"
_STATE_CONTEXT: Final = b"specflow atlassian oauth state"


class OAuthCode(StrEnum):
    """The closed outcomes of an OAuth step that is not a success."""

    STATE_INVALID = "atlassian_state_invalid"
    CODE_INVALID = "atlassian_code_invalid"
    SITE_NOT_GRANTED = "atlassian_site_not_granted"
    UNREACHABLE = "atlassian_unreachable"
    GRANT_INVALID = "atlassian_grant_invalid"  # the refresh token was refused (``invalid_grant``): reconnect
    REFUSED = "atlassian_refused"  # any other refusal of the token endpoint (a wrong client secret)


class OAuthError(Exception):
    """An OAuth failure as one closed code; ``str(e)`` is the code."""

    def __init__(self, code: OAuthCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class TokenGrant:
    """What the token endpoint answered; the tokens stay ``SecretStr``."""

    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int
    scope: str


@dataclass(frozen=True)
class AtlassianAccount:
    """The account a grant belongs to (``/myself``); kept out of every log."""

    account_id: str
    name: str

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}(...)"


def api_url(cloud_id: str) -> str:
    """The Jira REST base of one cloud through the Atlassian API gateway (the 3LO calls go there, not to the site)."""

    return API_URL.format(cloud_id=cloud_id)


class StateSigner(SharedStateSigner):
    """The ``state`` of the Atlassian consent (``integrations.state``); a bad one is ``STATE_INVALID``."""

    def __init__(self, token_key: SecretStr, *, clock: Clock = time.time) -> None:
        super().__init__(token_key, _STATE_CONTEXT, clock=clock)

    @override
    def verify(self, state: str, user_id: str, purpose: str = "") -> str:
        try:
            return super().verify(state, user_id, purpose)
        except StateInvalidError:
            raise OAuthError(OAuthCode.STATE_INVALID) from None


class TokenCipher(SharedTokenCipher):
    """Fernet encryption of the refresh tokens at rest (``integrations.cipher``)."""

    @override
    def decrypt(self, ciphertext: bytes) -> SecretStr:
        """The token; a ciphertext of another key (a rotated ``ATLASSIAN_TOKEN_KEY``) is ``GRANT_INVALID``."""

        try:
            return super().decrypt(ciphertext)
        except CipherError:
            raise OAuthError(OAuthCode.GRANT_INVALID) from None


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class AtlassianOAuthClient:
    """The calls to ``auth.atlassian.com`` and ``api.atlassian.com`` of the connect flow and the refresh."""

    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(
            headers={"Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(self, state: str) -> str:
        query = {
            "audience": AUDIENCE,
            "client_id": self._client_id,
            "scope": " ".join(SCOPES),
            "redirect_uri": self._redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def _call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise OAuthError(OAuthCode.UNREACHABLE) from None
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
            raise OAuthError(OAuthCode.UNREACHABLE)
        return response

    async def _grant(self, form: Mapping[str, str], refused: OAuthCode, seconds: float = TIMEOUT_SECONDS) -> TokenGrant:
        body = {"client_id": self._client_id, "client_secret": self._secret.get_secret_value(), **form}
        response = await self._call("POST", GRANT_URL, json=body, timeout=seconds)
        answer = _body(response)
        if not response.is_success:
            # Atlassian answers a dead grant with 403 or 400 and ``{"error": "invalid_grant"}``
            raise OAuthError(refused if answer.get("error") == INVALID_GRANT else OAuthCode.REFUSED)
        access, refresh = answer.get("access_token"), answer.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise OAuthError(OAuthCode.REFUSED)
        expires = answer.get("expires_in")
        return TokenGrant(
            access_token=SecretStr(access),
            refresh_token=SecretStr(refresh),
            expires_in=expires if isinstance(expires, int) and expires > 0 else 0,
            scope=str(answer.get("scope") or ""),
        )

    async def exchange(self, code: str) -> TokenGrant:
        """The grant of an authorization code; a used, expired or unknown code is ``CODE_INVALID``."""

        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri}
        try:
            return await self._grant(form, OAuthCode.CODE_INVALID)
        except OAuthError as error:
            if error.code is OAuthCode.REFUSED:
                raise OAuthError(OAuthCode.CODE_INVALID) from None
            raise

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        """A new access token and the rotated refresh token; a dead refresh token is ``GRANT_INVALID``."""

        form = {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()}
        return await self._grant(form, OAuthCode.GRANT_INVALID, REFRESH_TIMEOUT_SECONDS)

    async def cloud_id(self, access_token: SecretStr, site_url: str) -> str:
        """The id of the granted cloud whose ``url`` is ``site_url``; ``SITE_NOT_GRANTED`` when the grant lacks it."""

        response = await self._call("GET", RESOURCES_URL, headers=_bearer(access_token))
        if not response.is_success:
            raise OAuthError(OAuthCode.CODE_INVALID)
        try:
            resources = response.json()
        except ValueError:
            resources = []
        wanted = site_url.rstrip("/").lower()
        for resource in resources if isinstance(resources, list) else []:
            if (
                isinstance(resource, dict)
                and str(resource.get("url", "")).rstrip("/").lower() == wanted
                and resource.get("id")
            ):
                return str(resource["id"])
        raise OAuthError(OAuthCode.SITE_NOT_GRANTED)

    async def account(self, access_token: SecretStr, cloud_id: str) -> AtlassianAccount:
        """The account of the grant, read from ``/myself`` on the site's cloud."""

        response = await self._call("GET", f"{api_url(cloud_id)}/rest/api/3/myself", headers=_bearer(access_token))
        answer = _body(response)
        if not response.is_success or not answer.get("accountId"):
            raise OAuthError(OAuthCode.SITE_NOT_GRANTED)
        return AtlassianAccount(account_id=str(answer["accountId"]), name=str(answer.get("displayName") or ""))


def _bearer(token: SecretStr) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.get_secret_value()}"}
