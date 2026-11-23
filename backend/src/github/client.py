"""The calls to ``github.com`` (OAuth web flow of the GitHub App) and ``api.github.com`` (REST as the user).

A user access token can reach only what both the user and the app installation can reach (docs/github/oauth.md
"Access"). Every refusal is one closed ``GitHubCode``; no token, code, ``state``, login or GitHub text goes into a log
line, an error or a ``repr``. The clients ignore the proxy variables and ``.netrc`` (``trust_env=False``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, override
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from github.codes import GitHubCode, GitHubError
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("github.client")

AUTHORIZE_URL: Final = "https://github.com/login/oauth/authorize"
GRANT_URL: Final = "https://github.com/login/oauth/access_token"  # the token endpoint
API_URL: Final = "https://api.github.com"
INSTALL_URL: Final = "https://github.com/apps/{slug}/installations/new"
MANAGE_URL: Final = "https://github.com/settings/installations"
API_VERSION: Final = "2022-11-28"
TIMEOUT_SECONDS: Final = 30
REFRESH_TIMEOUT_SECONDS: Final = 10  # shorter: a refresh holds the row lock and a clone waits on it
PAGE_SIZE: Final = 100
REPOSITORY_LIMIT: Final = 1000  # the most repositories one list answers (``truncated`` beyond)
SIZE_LIMIT_KB: Final = 1024 * 1024  # 1 GiB as GitHub's ``size`` (KiB) of a repository
CODE_REFUSED: Final = "bad_verification_code"  # GitHub's ``error`` for a used, expired or unknown code
REFRESH_REFUSED: Final = "bad_refresh_token"  # GitHub's ``error`` for a dead refresh token
_FORBIDDEN_OR_MISSING: Final = frozenset({httpx.codes.FORBIDDEN, httpx.codes.NOT_FOUND})


@dataclass(frozen=True)
class TokenGrant:
    """What the token endpoint answered; the tokens stay ``SecretStr``. ``refresh_expires_in`` 0 = not told."""

    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int
    refresh_expires_in: int


@dataclass(frozen=True)
class GitHubUser:
    """The account of a token (``GET /user``); kept out of every log."""

    id: int
    login: str

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}(...)"


@dataclass(frozen=True)
class Repository:
    """A repository the token can read."""

    id: int
    full_name: str
    html_url: str
    private: bool
    default_branch: str | None
    size_kb: int

    @property
    def too_large(self) -> bool:
        return self.size_kb > SIZE_LIMIT_KB


def install_url(slug: str, state: str) -> str:
    """The page where the user installs the app and picks the repositories; GitHub hands ``state`` to the setup URL."""

    return f"{INSTALL_URL.format(slug=slug)}?{urlencode({'state': state})}"


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _positive(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _repository(item: Any) -> Repository | None:
    if not isinstance(item, dict):
        return None
    repo_id, full_name, html_url = item.get("id"), item.get("full_name"), item.get("html_url")
    if not isinstance(repo_id, int) or not isinstance(full_name, str) or not isinstance(html_url, str):
        return None
    branch = item.get("default_branch")
    return Repository(
        id=repo_id,
        full_name=full_name,
        html_url=html_url,
        private=item.get("private") is True,
        default_branch=branch if isinstance(branch, str) and branch else None,
        size_kb=_positive(item.get("size")),
    )


def _bearer(token: SecretStr) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


class GitHubClient:
    """The OAuth web flow, the refresh, and the REST reads of the integration."""

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
        query = {"client_id": self._client_id, "redirect_uri": self._redirect_uri, "state": state}
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def _call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise GitHubError(GitHubCode.UNREACHABLE) from None
        limited = response.status_code == httpx.codes.FORBIDDEN and response.headers.get("x-ratelimit-remaining") == "0"
        if limited or response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
            raise GitHubError(GitHubCode.UNREACHABLE)
        return response

    # OAuth ---------------------------------------------------------------------------------------------------------

    async def _grant(self, form: Mapping[str, str], dead: str, seconds: float) -> TokenGrant:
        """A token call. GitHub answers a refusal with 200 and ``{"error": ...}``: ``dead`` (the code or the refresh
        token is unusable) is ``GRANT_INVALID``; any other refusal (a wrong secret) is ``REFUSED``."""

        body = {"client_id": self._client_id, "client_secret": self._secret.get_secret_value(), **form}
        response = await self._call(
            "POST", GRANT_URL, data=body, headers={"Accept": "application/json"}, timeout=seconds
        )
        answer = _body(response)
        if not response.is_success or "error" in answer:
            raise GitHubError(GitHubCode.GRANT_INVALID if answer.get("error") == dead else GitHubCode.REFUSED)
        access, refresh = answer.get("access_token"), answer.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise GitHubError(GitHubCode.REFUSED)
        if not isinstance(refresh, str) or not refresh:
            # The app does not expire user tokens: no refresh token to keep (docs/configuration.md "Register").
            log_event(logger, WARNING, "github.oauth.no_refresh_token")
            raise GitHubError(GitHubCode.REFUSED)
        return TokenGrant(
            access_token=SecretStr(access),
            refresh_token=SecretStr(refresh),
            expires_in=_positive(answer.get("expires_in")),
            refresh_expires_in=_positive(answer.get("refresh_token_expires_in")),
        )

    async def exchange(self, code: str) -> TokenGrant:
        """The grant of an authorization code; any refusal (a used, expired or unknown code) is ``CODE_INVALID``."""

        form = {"code": code, "redirect_uri": self._redirect_uri}
        try:
            return await self._grant(form, CODE_REFUSED, TIMEOUT_SECONDS)
        except GitHubError as error:
            if error.code in (GitHubCode.GRANT_INVALID, GitHubCode.REFUSED):
                raise GitHubError(GitHubCode.CODE_INVALID) from None
            raise

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        """A new access token and the rotated refresh token; a dead refresh token is ``GRANT_INVALID``."""

        form = {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()}
        return await self._grant(form, REFRESH_REFUSED, REFRESH_TIMEOUT_SECONDS)

    async def revoke(self, access_token: SecretStr) -> bool:
        """Best effort: remove the user's grant of the app on GitHub (``DELETE /applications/{client_id}/grant``).
        True when GitHub confirmed it; never raises."""

        try:
            response = await self._call(
                "DELETE",
                f"{API_URL}/applications/{self._client_id}/grant",
                auth=(self._client_id, self._secret.get_secret_value()),
                json={"access_token": access_token.get_secret_value()},
                headers=self._api_headers(),
            )
        except GitHubError:
            return False
        return response.status_code == httpx.codes.NO_CONTENT

    # REST ----------------------------------------------------------------------------------------------------------

    @staticmethod
    def _api_headers(token: SecretStr | None = None) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        return headers | (_bearer(token) if token is not None else {})

    async def _get(self, token: SecretStr, path: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        response = await self._call("GET", f"{API_URL}{path}", params=params, headers=self._api_headers(token))
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise GitHubError(GitHubCode.AUTH_FAILED)  # the caller refreshes once and asks again
        return response

    async def user(self, token: SecretStr) -> GitHubUser:
        response = await self._get(token, "/user")
        answer = _body(response)
        user_id, login = answer.get("id"), answer.get("login")
        if not response.is_success or not isinstance(user_id, int) or not isinstance(login, str) or not login:
            raise GitHubError(GitHubCode.REFUSED)
        return GitHubUser(id=user_id, login=login)

    async def _pages(self, token: SecretStr, path: str, key: str, limit: int) -> tuple[list[Any], bool]:
        """Up to ``limit`` items of a paginated list (``key`` of each page); true when more were there."""

        items: list[Any] = []
        page = 1
        while True:
            response = await self._get(token, path, {"per_page": PAGE_SIZE, "page": page})
            answer = _body(response)
            batch = answer.get(key)
            if not response.is_success or not isinstance(batch, list):
                raise GitHubError(GitHubCode.REFUSED)
            items.extend(batch)
            total = answer.get("total_count")
            if len(items) > limit:
                return items[:limit], True
            if len(batch) < PAGE_SIZE or (isinstance(total, int) and len(items) >= total):
                return items, False
            if len(items) == limit:
                return items, True
            page += 1

    async def repositories(self, token: SecretStr) -> tuple[list[Repository], bool]:
        """Every repository the token reaches through the app's installations (deduplicated, by full name), at most
        ``REPOSITORY_LIMIT``; true when the list was cut."""

        installations, truncated = await self._pages(token, "/user/installations", "installations", REPOSITORY_LIMIT)
        found: dict[int, Repository] = {}
        for installation in installations:
            installation_id = installation.get("id") if isinstance(installation, dict) else None
            if not isinstance(installation_id, int):
                continue
            room = REPOSITORY_LIMIT - len(found)
            path = f"/user/installations/{installation_id}/repositories"
            items, cut = await self._pages(token, path, "repositories", room)
            truncated = truncated or cut
            for item in items:
                repository = _repository(item)
                if repository is not None:
                    found.setdefault(repository.id, repository)
            if len(found) >= REPOSITORY_LIMIT:
                truncated = truncated or installation is not installations[-1]
                break
        return sorted(found.values(), key=lambda r: r.full_name.lower()), truncated

    async def repository(self, token: SecretStr, repository_id: int) -> Repository:
        """One repository by id; one the token cannot read (or that is gone) is ``REPO_NOT_ACCESSIBLE``."""

        response = await self._get(token, f"/repositories/{repository_id}")
        if response.status_code in _FORBIDDEN_OR_MISSING:
            raise GitHubError(GitHubCode.REPO_NOT_ACCESSIBLE)
        repository = _repository(_body(response)) if response.is_success else None
        if repository is None:
            raise GitHubError(GitHubCode.REFUSED)
        return repository
