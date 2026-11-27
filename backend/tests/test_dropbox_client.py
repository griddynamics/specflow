"""The shared Dropbox transport (docs/dropbox/oauth.md, DB3): the endpoint allowlist, OAuth calls, the account read,
pacing, the closed refusals, the content stream and its headers. Frozen by W0; the streams add their own files
(``test_dropbox_connections.py``, ``test_dropbox_content.py``)."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr
from tests.dropbox_support import (
    ACCOUNT_ID,
    ACCOUNT_TEAM,
    CALLBACK,
    CLIENT_CANARY,
    CLIENT_ID,
    CODE_CANARY,
    EMAIL_CANARY,
    INVALID_GRANT,
    NAME_CANARY,
    PDF_ENTRY,
    ROOT_NAMESPACE,
    TEAM_ID,
    TEAM_ROOT_NAMESPACE,
    FakeDropbox,
)

from dropbox.client import (
    ALLOWED_URLS,
    SCOPE,
    DropboxApiError,
    DropboxClient,
    WaitBudget,
    api_arg,
)
from dropbox.codes import DropboxCode, DropboxError


async def token_of(client: DropboxClient) -> SecretStr:
    return (await client.exchange(CODE_CANARY)).access_token


def test_the_authorize_url_asks_for_offline_access_and_the_four_read_scopes() -> None:
    url = FakeDropbox().client().authorize_url("state-1")

    assert url.startswith("https://www.dropbox.com/oauth2/authorize?")
    query = httpx.URL(url).params
    assert (query["response_type"], query["token_access_type"], query["state"]) == ("code", "offline", "state-1")
    assert query["scope"] == SCOPE == "account_info.read files.metadata.read files.content.read sharing.read"
    assert query["redirect_uri"] == "http://localhost:3001/api/integrations/dropbox/callback"
    assert CLIENT_CANARY not in url


def test_the_allowlist_is_exactly_the_read_endpoints_of_the_decision() -> None:
    assert {
        "https://api.dropboxapi.com/oauth2/token",
        "https://api.dropboxapi.com/2/auth/token/revoke",
        "https://api.dropboxapi.com/2/users/get_current_account",
        "https://api.dropboxapi.com/2/files/get_metadata",
        "https://api.dropboxapi.com/2/files/list_folder",
        "https://api.dropboxapi.com/2/files/list_folder/continue",
        "https://api.dropboxapi.com/2/sharing/get_shared_link_metadata",
        "https://content.dropboxapi.com/2/files/download",
        "https://content.dropboxapi.com/2/files/export",
    } == ALLOWED_URLS


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://api.dropboxapi.com/2/files/delete_v2"),
        ("POST", "https://api.dropboxapi.com/2/files/upload"),
        ("POST", "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings"),
        ("POST", "https://content.dropboxapi.com/2/files/upload"),
        ("POST", "https://content.dropboxapi.com/2/files/get_metadata"),  # an RPC path on the content host
        ("POST", "https://api.dropboxapi.com/2/files/download"),  # a content path on the api host
        ("POST", "https://notify.dropboxapi.com/2/files/list_folder/longpoll"),
        ("POST", "https://evil.example.test/2/files/list_folder"),
        ("POST", "http://api.dropboxapi.com/2/files/list_folder"),
        ("GET", "https://api.dropboxapi.com/2/files/list_folder"),
        ("PUT", "https://content.dropboxapi.com/2/files/download"),
    ],
)
async def test_any_other_endpoint_host_or_method_raises_before_anything_is_sent(method: str, url: str) -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()

    with pytest.raises(RuntimeError):
        client._build(method, url)
    with pytest.raises(RuntimeError):
        await client.rpc(SecretStr("t"), "files/delete_v2")

    assert dropbox.calls == [] and dropbox.violations == []


async def test_a_full_connect_and_read_calls_only_allowlisted_endpoints() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()
    token = await token_of(client)

    account = await client.get_current_account(token)
    async with client.download(token, {"path": PDF_ENTRY["id"]}) as opened:
        body = b"".join([chunk async for chunk in opened.chunks()])
    revoked = await client.revoke(token)

    assert (account.account_id, account.name, account.team_id, account.root_namespace_id) == (
        ACCOUNT_ID,
        NAME_CANARY,
        None,
        ROOT_NAMESPACE,
    )
    assert body == b"%PDF-1.7 fake" and opened.result["id"] == PDF_ENTRY["id"] and revoked
    assert dropbox.calls == [
        ("api.dropboxapi.com", "/oauth2/token"),
        ("api.dropboxapi.com", "/2/users/get_current_account"),
        ("content.dropboxapi.com", "/2/files/download"),
        ("api.dropboxapi.com", "/2/auth/token/revoke"),
    ]
    assert dropbox.violations == []


async def test_a_team_account_names_its_team_and_root_and_the_email_is_never_read() -> None:
    dropbox = FakeDropbox()
    dropbox.codes[CODE_CANARY] = ACCOUNT_TEAM
    client = dropbox.client()

    account = await client.get_current_account(await token_of(client))

    assert (account.team_id, account.team_name, account.root_namespace_id) == (
        TEAM_ID,
        "Acme Team",
        TEAM_ROOT_NAMESPACE,
    )
    assert EMAIL_CANARY not in repr(account) and EMAIL_CANARY not in str(vars(account))
    assert NAME_CANARY not in repr(account)


async def test_the_code_exchange_keeps_the_refresh_token_and_a_refresh_does_not_rotate_it() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()

    grant = await client.exchange(CODE_CANARY)
    assert grant.refresh_token is not None
    first = await client.refresh(grant.refresh_token)
    second = await client.refresh(grant.refresh_token)  # the same refresh token works again

    assert first.refresh_token is None and second.refresh_token is None
    assert first.access_token.get_secret_value() != second.access_token.get_secret_value()
    assert first.expires_in == 14400 and dropbox.refreshes == 2


async def test_a_dead_code_is_code_invalid_a_dead_refresh_is_grant_invalid_and_errors_carry_no_text() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()

    with pytest.raises(DropboxError) as code:
        await client.exchange("wrong")
    dropbox.grant_error = INVALID_GRANT
    with pytest.raises(DropboxError) as refresh:
        await client.refresh(SecretStr("refresh-x"))
    dropbox.status = 503
    with pytest.raises(DropboxError) as down:
        await client.refresh(SecretStr("refresh-x"))

    assert code.value.code is DropboxCode.CODE_INVALID
    assert refresh.value.code is DropboxCode.GRANT_INVALID
    assert down.value.code is DropboxCode.UNREACHABLE
    assert str(code.value) == "dropbox_code_invalid" and "canary" not in str(refresh.value)


async def test_a_401_expired_access_token_is_access_refused_for_one_refresh() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()
    token = await token_of(client)
    dropbox.expire_access()

    with pytest.raises(DropboxApiError) as refused:
        await client.rpc(token, "users/get_current_account")

    assert refused.value.code is DropboxCode.ACCESS_REFUSED and refused.value.tags == ("expired_access_token",)


async def test_a_429_waits_for_retry_after_and_asks_again() -> None:
    dropbox = FakeDropbox()
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    client = DropboxClient(CLIENT_ID, SecretStr(CLIENT_CANARY), CALLBACK, transport=dropbox.transport(), sleep=sleep)
    token = await token_of(client)
    dropbox.throttle, dropbox.retry_after = 2, "3"

    await client.rpc(token, "users/get_current_account")

    assert waited == [3.0, 3.0]


async def test_a_429_without_retry_after_backs_off_1_2_4_then_is_unreachable() -> None:
    dropbox = FakeDropbox()
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    client = DropboxClient(CLIENT_ID, SecretStr(CLIENT_CANARY), CALLBACK, transport=dropbox.transport(), sleep=sleep)
    token = await token_of(client)
    dropbox.throttle, dropbox.retry_after = 10, None

    with pytest.raises(DropboxError) as raised:
        await client.rpc(token, "users/get_current_account")

    assert waited == [1.0, 2.0, 4.0] and raised.value.code is DropboxCode.UNREACHABLE


async def test_a_wait_that_does_not_fit_the_budget_is_not_made() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()
    token = await token_of(client)
    dropbox.throttle, dropbox.retry_after = 1, "30"

    with pytest.raises(DropboxError) as raised:
        await client.rpc(token, "users/get_current_account", budget=WaitBudget(10))

    assert raised.value.code is DropboxCode.UNREACHABLE


async def test_a_5xx_is_retried_like_a_429() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()
    token = await token_of(client)
    dropbox.status = 503

    with pytest.raises(DropboxError) as raised:
        await client.rpc(token, "users/get_current_account")

    assert raised.value.code is DropboxCode.UNREACHABLE
    assert len([call for call in dropbox.calls if call[1] == "/2/users/get_current_account"]) == 4


async def test_the_409s_and_the_422_invalid_root_are_closed_codes_with_tags_only() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()
    token = await token_of(client)
    dropbox.not_found.add("/gone")
    dropbox.restricted.add("/locked")

    with pytest.raises(DropboxApiError) as gone:
        await client.rpc(token, "files/get_metadata", {"path": "/gone"})
    with pytest.raises(DropboxApiError) as locked:
        await client.rpc(token, "files/get_metadata", {"path": "/locked"})
    dropbox.invalid_root = 1
    with pytest.raises(DropboxApiError) as root:
        await client.rpc(token, "files/get_metadata", {"path": "/gone"}, path_root=ROOT_NAMESPACE)

    assert (gone.value.code, gone.value.tags) == (DropboxCode.NOT_FOUND, ("path", "not_found"))
    assert locked.value.code is DropboxCode.RESTRICTED
    assert root.value.code is DropboxCode.INVALID_ROOT
    assert "/gone" not in str(gone.value) and "/locked" not in repr(locked.value)


async def test_every_call_with_a_root_sends_the_path_root_header() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()
    token = await token_of(client)

    await client.rpc(token, "files/get_metadata", {"path": "/graphico lab"}, path_root=ROOT_NAMESPACE)
    async with client.download(token, {"path": PDF_ENTRY["id"]}, path_root=ROOT_NAMESPACE):
        pass

    assert [json.loads(root) for root in dropbox.path_roots] == [{".tag": "root", "root": ROOT_NAMESPACE}] * 2


def test_the_api_arg_header_escapes_every_non_ascii_character_and_del() -> None:
    value = api_arg({"path": "/Zażółć gęślą \x7f 文件.pdf"})

    assert value.isascii() and "\x7f" not in value
    assert "\\u017c" in value and "\\u007f" in value and "\\u6587" in value
    assert json.loads(value) == {"path": "/Zażółć gęślą \x7f 文件.pdf"}


async def test_a_refused_download_is_a_closed_code_and_the_arg_goes_in_the_header() -> None:
    dropbox = FakeDropbox()
    client = dropbox.client()
    token = await token_of(client)

    with pytest.raises(DropboxApiError) as raised:
        async with client.download(token, {"path": "id:missing"}):
            pass

    assert raised.value.code is DropboxCode.NOT_FOUND
    assert json.loads(dropbox.api_args[-1]) == {"path": "id:missing"}
    assert dropbox.violations == []
