"""The GitHub connections (docs/github/oauth.md "Connect flow", "Tokens"): the shared ``state`` and token cache, the
client against a scripted GitHub, and ``GitHubConnections`` over an in-memory store. No token, code, ``state`` or
login may reach a log line or an error."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from db.models import REPOSITORY_FAILURE_CODES
from github.client import REPOSITORY_LIMIT, SIZE_LIMIT_KB, install_url
from github.codes import CLONE_FAILURE_CODES, GitHubCode, GitHubError
from github.connections import GitHubConnections
from github_support import (
    CLIENT_CANARY,
    CODE_CANARY,
    LOGIN,
    T0,
    USER_ID,
    FakeGitHub,
    MemoryStore,
    github_settings,
    repo,
)
from integrations.state import STATE_TTL_SECONDS, StateInvalidError, StateSigner
from integrations.tokens import UserTokens

USER = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)
KEY = SecretStr(Fernet.generate_key().decode())


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(github: FakeGitHub, store: MemoryStore, clock: Clock | None = None) -> GitHubConnections:
    clock = clock or Clock()
    return GitHubConnections(
        github_settings(),
        store=store.transaction,
        client=github.client(),
        now=lambda: T0,
        monotonic=clock,
        clock=clock,
    )


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connected(github: FakeGitHub, store: MemoryStore, clock: Clock | None = None) -> GitHubConnections:
    connections = make(github, store, clock)
    state = state_of(connections.authorize_url(USER, "/projects/p/settings?tab=github"))
    await connections.connect(USER, code=CODE_CANARY, state=state)
    return connections


# The shared state ----------------------------------------------------------------------------------------------------


def test_a_state_holds_its_purpose_and_a_state_of_another_purpose_is_invalid() -> None:
    clock = Clock()
    signer = StateSigner(KEY, b"context a", clock=clock)
    state = signer.sign(str(USER), "/x", "install")

    assert signer.verify(state, str(USER), "install") == "/x"
    for purpose in ("", "authorize"):
        with pytest.raises(StateInvalidError):
            signer.verify(state, str(USER), purpose)
    with pytest.raises(StateInvalidError):
        StateSigner(KEY, b"context b", clock=clock).verify(state, str(USER), "install")
    clock.now += STATE_TTL_SECONDS + 1
    with pytest.raises(StateInvalidError):
        signer.verify(state, str(USER), "install")


async def test_the_token_cache_refreshes_once_for_callers_that_wait_and_never_returns_a_refused_token() -> None:
    clock = Clock()
    tokens = UserTokens(monotonic=clock)
    calls = 0
    release = asyncio.Event()

    class Grant:
        def __init__(self, value: str) -> None:
            self.access_token, self.expires_in = SecretStr(value), 3600

    async def rotate() -> Grant:
        nonlocal calls
        calls += 1
        await release.wait()
        return Grant(f"token-{calls}")

    waiting = [asyncio.ensure_future(tokens.access_token(USER, rotate)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(*waiting) == ["token-1"] * 3
    assert calls == 1
    assert await tokens.refresh(USER, rotate, used="token-1") == "token-2"
    clock.now += 3600
    assert await tokens.access_token(USER, rotate) == "token-3"  # expired 60 s early


# The client ----------------------------------------------------------------------------------------------------------


def test_the_urls_carry_the_client_the_callback_and_the_state() -> None:
    client = FakeGitHub().client()

    query = parse_qs(urlsplit(client.authorize_url("s-1")).query)

    assert query == {
        "client_id": ["Iv23-client-1"],
        "redirect_uri": ["http://localhost:3001/api/integrations/github/callback"],
        "state": ["s-1"],
    }
    assert install_url("specflow-local", "s 2") == "https://github.com/apps/specflow-local/installations/new?state=s+2"


async def test_a_bad_code_a_bad_secret_and_an_outage_are_closed_codes() -> None:
    github = FakeGitHub()
    client = github.client()

    with pytest.raises(GitHubError) as raised:
        await client.exchange("unknown-code")
    assert raised.value.code is GitHubCode.CODE_INVALID
    github.grant_error = "incorrect_client_credentials"
    with pytest.raises(GitHubError) as raised:
        await client.exchange(CODE_CANARY)
    assert raised.value.code is GitHubCode.CODE_INVALID
    github.grant_error = "bad_refresh_token"
    with pytest.raises(GitHubError) as raised:
        await client.refresh(SecretStr("refresh-x"))
    assert raised.value.code is GitHubCode.GRANT_INVALID
    github.grant_error = "incorrect_client_credentials"
    with pytest.raises(GitHubError) as raised:
        await client.refresh(SecretStr("refresh-x"))
    assert raised.value.code is GitHubCode.REFUSED
    github.status = 503
    with pytest.raises(GitHubError) as raised:
        await client.exchange(CODE_CANARY)
    assert raised.value.code is GitHubCode.UNREACHABLE
    assert "canary" not in str(raised.value)


async def test_a_grant_without_a_refresh_token_is_refused() -> None:
    github = FakeGitHub()
    github.with_refresh = False

    with pytest.raises(GitHubError) as raised:
        await github.client().exchange(CODE_CANARY)

    assert raised.value.code is GitHubCode.CODE_INVALID


async def test_the_repositories_of_every_installation_are_listed_once_sorted_and_capped() -> None:
    github = FakeGitHub()
    github.installations[USER_ID] = {
        11: [repo(3, "zeta/b"), repo(1, "Acme/a", size=SIZE_LIMIT_KB + 1)],
        12: [repo(3, "zeta/b"), repo(2, "beta/c", private=False)],
    }
    client = github.client()
    grant = await client.exchange(CODE_CANARY)

    repositories, truncated = await client.repositories(grant.access_token)

    assert [r.full_name for r in repositories] == ["Acme/a", "beta/c", "zeta/b"]
    assert [r.too_large for r in repositories] == [True, False, False]
    assert repositories[1].private is False and truncated is False

    github.installations[USER_ID] = {11: [repo(n, f"o/r{n:04d}") for n in range(1, REPOSITORY_LIMIT + 6)]}
    repositories, truncated = await client.repositories(grant.access_token)
    assert len(repositories) == REPOSITORY_LIMIT and truncated is True


async def test_one_repository_by_id_and_a_missing_one_is_not_accessible() -> None:
    github = FakeGitHub()
    github.installations[USER_ID] = {11: [repo(7, "octo/app")]}
    client = github.client()
    token = (await client.exchange(CODE_CANARY)).access_token

    assert (await client.repository(token, 7)).full_name == "octo/app"
    with pytest.raises(GitHubError) as raised:
        await client.repository(token, 8)
    assert raised.value.code is GitHubCode.REPO_NOT_ACCESSIBLE
    with pytest.raises(GitHubError) as raised:
        await client.repository(SecretStr("dead"), 7)
    assert raised.value.code is GitHubCode.AUTH_FAILED


# The connections -----------------------------------------------------------------------------------------------------


async def test_connect_checks_the_state_stores_the_encrypted_refresh_token_and_logs_ids_only(caplog) -> None:
    github, store = FakeGitHub(), MemoryStore()
    connections = make(github, store)
    state = state_of(connections.authorize_url(USER, "/projects/p/settings?tab=github"))

    with caplog.at_level(logging.DEBUG):
        view, return_to = await connections.connect(USER, code=CODE_CANARY, state=state)

    assert (view.status, view.login, return_to) == ("active", LOGIN, "/projects/p/settings?tab=github")
    row = store.rows[USER]
    assert row.github_user_id == USER_ID and b"refresh-" not in row.refresh_token
    assert store.refresh_expires[USER] is not None
    assert await connections.access_token(USER) == "access-1"  # the exchange's token, no refresh
    assert github.refreshes == 0
    text = caplog.text
    for canary in (CODE_CANARY, state, LOGIN, "access-1", "refresh-1", CLIENT_CANARY):
        assert canary not in text


@pytest.mark.parametrize("case", ["other_user", "install_state", "garbage"])
async def test_a_bad_state_is_refused_before_github_is_called(case: str) -> None:
    github, store = FakeGitHub(), MemoryStore()
    connections = make(github, store)
    state = {
        "other_user": state_of(connections.authorize_url(OTHER, "/x")),
        "install_state": state_of(connections.install_url(USER, "/x")),
        "garbage": "a.b.c",
    }[case]

    with pytest.raises(GitHubError) as raised:
        await connections.connect(USER, code=CODE_CANARY, state=state)

    assert raised.value.code is GitHubCode.STATE_INVALID
    assert github.calls == [] and store.rows == {}


def test_the_setup_check_takes_only_an_install_state_of_the_caller() -> None:
    connections = make(FakeGitHub(), MemoryStore())
    install = state_of(connections.install_url(USER, "/projects/p/settings?tab=github"))

    assert connections.check_setup(USER, install) == "/projects/p/settings?tab=github"
    for user, state in ((OTHER, install), (USER, state_of(connections.authorize_url(USER, "/x")))):
        with pytest.raises(GitHubError) as raised:
            connections.check_setup(user, state)
        assert raised.value.code is GitHubCode.STATE_INVALID


async def test_an_expired_access_token_is_refreshed_and_the_rotated_refresh_token_is_committed_first() -> None:
    github, store, clock = FakeGitHub(), MemoryStore(), Clock()
    connections = await connected(github, store, clock)
    clock.now += 28800

    assert await connections.access_token(USER) == "access-2"
    assert store.events[-3:] == [("lock", USER), ("rotate", USER), ("commit", None)]
    assert github.refresh == {"refresh-2": USER_ID}  # the old refresh token is dead


async def test_a_401_refreshes_once_and_asks_again() -> None:
    github, store = FakeGitHub(), MemoryStore()
    github.installations[USER_ID] = {11: [repo(7, "octo/app")]}
    connections = await connected(github, store)
    github.expire_access()

    repositories, _ = await connections.repositories(USER)

    assert [r.id for r in repositories] == [7] and github.refreshes == 1


async def test_callers_waiting_on_one_refresh_share_it() -> None:
    github, store, clock = FakeGitHub(), MemoryStore(), Clock()
    connections = await connected(github, store, clock)
    clock.now += 28800
    github.gate = asyncio.Event()

    waiting = [asyncio.ensure_future(connections.access_token(USER)) for _ in range(4)]
    await github.entered.wait()
    github.gate.set()

    assert set(await asyncio.gather(*waiting)) == {"access-2"}
    assert github.refreshes == 1


@pytest.mark.parametrize("cause", ["bad_refresh_token", "rotated_key"])
async def test_a_dead_refresh_token_expires_the_connection_once(cause: str, caplog) -> None:
    github, store, clock = FakeGitHub(), MemoryStore(), Clock()
    connections = await connected(github, store, clock)
    clock.now += 28800
    if cause == "bad_refresh_token":
        github.grant_error = "bad_refresh_token"
    else:
        connections = make(github, store, clock)
        connections._cipher = type(connections._cipher)(SecretStr(Fernet.generate_key().decode()))

    with caplog.at_level(logging.INFO), pytest.raises(GitHubError) as raised:
        await connections.access_token(USER)
    assert raised.value.code is GitHubCode.RECONNECT_REQUIRED
    assert store.rows[USER].status == "expired"
    view = await connections.get(USER)
    assert view is not None and view.status == "expired"

    calls = len(github.calls)
    with pytest.raises(GitHubError) as again:
        await connections.access_token(USER)
    assert again.value.code is GitHubCode.RECONNECT_REQUIRED and len(github.calls) == calls
    assert "github.expired" in caplog.text


@pytest.mark.parametrize(("status", "code"), [(503, GitHubCode.UNREACHABLE), (None, GitHubCode.AUTH_FAILED)])
async def test_a_refresh_that_fails_for_another_reason_keeps_the_connection_active(status, code) -> None:
    github, store, clock = FakeGitHub(), MemoryStore(), Clock()
    connections = await connected(github, store, clock)
    clock.now += 28800
    github.status = status
    if status is None:
        github.grant_error = "incorrect_client_credentials"

    with pytest.raises(GitHubError) as raised:
        await connections.access_token(USER)

    assert raised.value.code is code and store.rows[USER].status == "active"


@pytest.mark.parametrize(
    ("change", "code"), [("delete", GitHubCode.NOT_CONNECTED), ("expire", GitHubCode.RECONNECT_REQUIRED)]
)
async def test_a_cached_token_is_not_served_after_another_process_ends_the_connection(change: str, code) -> None:
    github, store, clock = FakeGitHub(), MemoryStore(), Clock()
    worker = await connected(github, store, clock)  # this process holds "access-1" in its cache
    assert await worker.access_token(USER) == "access-1"
    elsewhere = make(github, store, clock)  # another api replica
    if change == "delete":
        await elsewhere.disconnect(USER)
    else:
        await store.expire(USER, now=T0)

    with pytest.raises(GitHubError) as raised:
        await worker.access_token(USER)

    assert raised.value.code is code
    assert worker._tokens._good(USER, used=None) is None  # the cache entry is gone too


async def test_a_reconnect_in_another_process_replaces_the_cached_token() -> None:
    github, store, clock = FakeGitHub(), MemoryStore(), Clock()
    worker = await connected(github, store, clock)
    assert await worker.access_token(USER) == "access-1"
    later = GitHubConnections(
        github_settings(),
        store=store.transaction,
        client=github.client(),
        now=lambda: T0 + timedelta(hours=1),
        monotonic=clock,
        clock=clock,
    )
    github.codes[CODE_CANARY] = USER_ID
    state = state_of(later.authorize_url(USER, "/x"))
    await later.connect(USER, code=CODE_CANARY, state=state)  # a new grant: "access-2" / "refresh-2"

    # Refreshed from the new row, not the old grant's cached "access-1"; then cached again.
    assert await worker.access_token(USER) == "access-3"
    assert github.refreshes == 1
    assert await worker.access_token(USER) == "access-3"


async def test_no_connection_is_not_connected() -> None:
    with pytest.raises(GitHubError) as raised:
        await make(FakeGitHub(), MemoryStore()).access_token(USER)

    assert raised.value.code is GitHubCode.NOT_CONNECTED


async def test_disconnect_deletes_the_row_and_revokes_the_grant_and_works_without_a_connection() -> None:
    github, store = FakeGitHub(), MemoryStore()
    connections = await connected(github, store)

    await connections.disconnect(USER)

    assert store.rows == {} and github.revoked == ["access-1"]
    with pytest.raises(GitHubError) as raised:
        await connections.access_token(USER)
    assert raised.value.code is GitHubCode.NOT_CONNECTED
    await connections.disconnect(USER)  # nothing to delete, nothing to revoke
    assert github.revoked == ["access-1"]


def test_the_failure_codes_of_the_table_follow_the_github_codes() -> None:
    assert REPOSITORY_FAILURE_CODES == CLONE_FAILURE_CODES


def test_nothing_secret_is_in_a_repr() -> None:
    connections = make(FakeGitHub(), MemoryStore())

    text = repr(connections._client) + repr(connections._cipher) + repr(connections._states) + repr(connections._tokens)
    assert CLIENT_CANARY not in text and github_settings().token_key.get_secret_value() not in text
