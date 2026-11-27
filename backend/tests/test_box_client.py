"""The shared Box transport (docs/box/oauth.md): the read-only guard, OAuth calls, throttling and the download redirect.
Frozen by W0; the streams add their own files (``test_box_connections.py``, ``test_box_content.py``)."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr
from tests.box_support import (
    CLIENT_CANARY,
    CODE_CANARY,
    ENTERPRISE_ID,
    SHARED_LINK_CANARY,
    USER_ID,
    USERS_ME_PERSONAL,
    FakeBox,
)

from box.client import SCOPE, BoxClient, WaitBudget
from box.codes import BoxCode, BoxError


async def token_of(box: FakeBox, client: BoxClient) -> SecretStr:
    return (await client.exchange(CODE_CANARY)).access_token


def test_the_authorize_url_asks_for_one_scope_and_carries_the_state() -> None:
    url = FakeBox().client().authorize_url("state-1")

    assert url.startswith("https://account.box.com/api/oauth2/authorize?")
    query = httpx.URL(url).params
    assert (
        query["response_type"] == "code" and query["state"] == "state-1" and query["scope"] == SCOPE == "root_readwrite"
    )
    assert query["redirect_uri"] == "http://localhost:3001/api/integrations/box/callback"
    assert CLIENT_CANARY not in url


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_the_client_refuses_any_write_towards_the_rest_api(method: str) -> None:
    box = FakeBox()
    client = box.client()

    with pytest.raises(RuntimeError):
        await client._once(method, "https://api.box.com/2.0/folders")
    with pytest.raises(RuntimeError):
        await client._once("POST", "https://api.box.com/2.0/files/1/content")

    assert box.calls == [] and box.violations == []


async def test_a_full_connect_and_read_sends_only_gets_and_the_two_oauth_posts() -> None:
    box = FakeBox()
    client = box.client()
    token = await token_of(box, client)

    user = await client.user(token)
    await client.revoke(token)

    assert (user.id, user.enterprise_id, user.enterprise_name) == (USER_ID, ENTERPRISE_ID, "Acme Corp")
    assert box.calls == [("POST", "/oauth2/token"), ("GET", "/2.0/users/me"), ("POST", "/oauth2/revoke")]
    assert box.violations == []


async def test_a_personal_account_has_no_enterprise() -> None:
    box = FakeBox()
    box.codes[CODE_CANARY] = USERS_ME_PERSONAL
    client = box.client()

    user = await client.user(await token_of(box, client))

    assert (user.enterprise_id, user.enterprise_name) == (None, None) and "Pat" not in repr(user)


async def test_a_dead_code_is_code_invalid_and_a_dead_refresh_token_is_grant_invalid() -> None:
    box = FakeBox()
    client = box.client()

    with pytest.raises(BoxError) as code:
        await client.exchange("wrong")
    with pytest.raises(BoxError) as refresh:
        await client.refresh(SecretStr("refresh-unknown"))

    assert code.value.code is BoxCode.CODE_INVALID and refresh.value.code is BoxCode.GRANT_INVALID
    assert str(refresh.value) == "box_grant_invalid"


async def test_a_token_call_that_box_refuses_for_another_reason_is_refused_and_a_5xx_is_unreachable() -> None:
    box = FakeBox()
    client = box.client()
    box.grant_error = {"error": "invalid_client", "error_description": "canary text"}
    with pytest.raises(BoxError) as refused:
        await client.refresh(SecretStr("refresh-1"))
    box.status = 503
    with pytest.raises(BoxError) as down:
        await client.refresh(SecretStr("refresh-1"))

    assert refused.value.code is BoxCode.REFUSED and down.value.code is BoxCode.UNREACHABLE
    assert "canary" not in str(refused.value)


async def test_the_refresh_rotates_and_the_old_refresh_token_dies() -> None:
    box = FakeBox()
    client = box.client()
    first = await client.exchange(CODE_CANARY)

    second = await client.refresh(first.refresh_token)
    with pytest.raises(BoxError) as again:
        await client.refresh(first.refresh_token)

    assert second.refresh_token != first.refresh_token and again.value.code is BoxCode.GRANT_INVALID


async def test_a_401_is_token_refused() -> None:
    box = FakeBox()
    client = box.client()
    token = await token_of(box, client)
    box.expire_access()

    with pytest.raises(BoxError) as error:
        await client.get(token, "/users/me")

    assert error.value.code is BoxCode.ACCESS_REFUSED


async def test_a_429_waits_for_retry_after_and_asks_again() -> None:
    box = FakeBox()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    client = BoxClient("box-client-1", SecretStr(CLIENT_CANARY), "http://x/cb", transport=box.transport(), sleep=sleep)
    token = await token_of(box, client)
    box.throttle = 2

    response = await client.get(token, "/users/me")

    assert response.status_code == 200 and waits == [1.0, 1.0]


async def test_throttling_past_the_attempts_or_the_wait_budget_is_unreachable() -> None:
    box = FakeBox()
    client = box.client()
    token = await token_of(box, client)
    box.throttle = 100
    with pytest.raises(BoxError) as attempts:
        await client.get(token, "/users/me")
    with pytest.raises(BoxError) as budget:
        await client.get(token, "/users/me", budget=WaitBudget(0.5))

    assert attempts.value.code is BoxCode.UNREACHABLE and budget.value.code is BoxCode.UNREACHABLE


async def test_a_shared_link_goes_in_the_boxapi_header_only() -> None:
    box = FakeBox()
    client = box.client()
    token = await token_of(box, client)

    response = await client.get(token, "/shared_items", shared_link=SHARED_LINK_CANARY)

    assert response.status_code == 200
    assert box.shared_headers == [f"shared_link={SHARED_LINK_CANARY}"]
    assert SHARED_LINK_CANARY not in repr(client)


async def test_a_download_follows_the_redirect_to_the_download_host_without_the_token() -> None:
    box = FakeBox()
    client = box.client()
    token = await token_of(box, client)

    async with client.stream(token, "/files/3001/content") as response:
        data = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert data == b"%PDF-1.7 fake" and box.violations == []
    assert [path for _, path in box.calls][-2:] == ["/2.0/files/3001/content", "/d/1/signed/3001"]


async def test_a_download_that_is_not_ready_waits_and_a_missing_file_is_no_access() -> None:
    box = FakeBox()
    client = box.client()
    token = await token_of(box, client)
    box.not_ready = 1
    async with client.stream(token, "/files/3001/content") as response:
        assert response.status_code == 200
    with pytest.raises(BoxError) as missing:
        async with client.stream(token, "/files/9999/content"):
            pass

    assert missing.value.code is BoxCode.NO_ACCESS


async def test_a_redirect_to_a_foreign_host_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example.test/steal"})

    client = BoxClient("id", SecretStr("secret"), "http://x/cb", transport=httpx.MockTransport(handler))

    with pytest.raises(BoxError) as error:
        async with client.stream(SecretStr("t"), "/files/1/content"):
            pass

    assert error.value.code is BoxCode.REFUSED
