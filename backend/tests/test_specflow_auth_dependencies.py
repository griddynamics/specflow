"""``current_principal``, the dev-mode warning, and the no-token-in-logs canary.

A minimal FastAPI app stands in for S7's wiring: the verifier lives at
``app.state.token_verifier`` and an ``AuthError`` handler copies the status,
code and headers. Requests go through ``httpx.ASGITransport`` (no sockets).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from auth.claims import ClaimPolicy, Principal
from auth.dependencies import (
    VERIFIER_STATE_ATTR,
    bearer_token,
    current_principal,
    log_dev_mode_warning,
)
from auth.errors import AuthError
from auth.jwks import JwksCache
from auth.verifiers import (
    DEV_ISSUER,
    DevTokenVerifier,
    OidcJwtVerifier,
    TokenVerifier,
)
from logger_harness import events, fields
from specflow_api_wait_support import wait_until
from specflow_auth_support import (
    DEV_KEY,
    POLICY,
    StubIssuer,
    claims,
    rsa_key,
    sign,
)

DEV_POLICY = ClaimPolicy.build(
    issuer=DEV_ISSUER,
    audience=POLICY.audience,
    client_ids=POLICY.client_ids,
    allowed_email_domains=POLICY.allowed_email_domains,
)
CANARY = "canary7f3a9c"


def _app(verifier: TokenVerifier) -> FastAPI:
    app = FastAPI()
    setattr(app.state, VERIFIER_STATE_ATTR, verifier)

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, error: AuthError) -> JSONResponse:
        body = {"error": {"code": error.code, "message": error.message, "details": {}}}
        return JSONResponse(body, status_code=error.status, headers=error.headers)

    @app.get("/me")
    async def me(principal: Annotated[Principal, Depends(current_principal)]) -> dict[str, str]:
        return {"subject": principal.subject, "email": principal.email}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = _app(DevTokenVerifier(DEV_POLICY, SecretStr(DEV_KEY)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _dev_token(**overrides: object) -> str:
    payload = claims(**{"iss": DEV_ISSUER, "sub": "dev:alice@example.com", **overrides})
    return sign(payload, DEV_KEY, kid=None, alg="HS256")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "missing_header"),
        ("", "missing_header"),
        ("Basic dXNlcjpwYXNz", "malformed_header"),
        ("Bearer", "malformed_header"),
        ("Bearer ", "malformed_header"),
        ("Bearer a b", "malformed_header"),
        ('Bearer a"b', "malformed_header"),
        ("Bearer " + "x" * 20000, "malformed_header"),
    ],
)
def test_bearer_parsing_rejects_missing_and_malformed_headers(value: str | None, reason: str) -> None:
    with pytest.raises(AuthError) as caught:
        bearer_token(value)

    assert (caught.value.status, caught.value.reason) == (401, reason)
    assert caught.value.headers == {"WWW-Authenticate": "Bearer"}


def test_the_bearer_scheme_is_case_insensitive() -> None:
    assert bearer_token("bearer abc.def.ghi") == "abc.def.ghi"


async def test_a_valid_token_yields_the_principal(client: httpx.AsyncClient) -> None:
    response = await client.get("/me", headers={"Authorization": f"Bearer {_dev_token()}"})

    assert response.status_code == 200
    assert response.json() == {
        "subject": "dev:alice@example.com",
        "email": "alice@example.com",
    }


async def test_failures_map_to_401_with_www_authenticate_and_403_without(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="auth.dependencies")
    missing = await client.get("/me")
    bad_domain = await client.get(
        "/me",
        headers={"Authorization": f"Bearer {_dev_token(email='eve@evil.example')}"},
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["error"]["code"] == "invalid_token"
    assert bad_domain.status_code == 403
    assert "www-authenticate" not in bad_domain.headers
    assert bad_domain.json()["error"]["code"] == "domain_not_allowed"
    logged = [fields(r) for r in events(caplog, "auth.token.rejected")]
    assert [(f["reason"], f["status"]) for f in logged] == [
        ("missing_header", 401),
        ("domain_not_allowed", 403),
    ]


async def test_healthz_answers_while_the_jwks_endpoint_hangs(
    stub_issuer: StubIssuer,
) -> None:
    stub_issuer.mode = "hang"
    app = _app(OidcJwtVerifier(POLICY, JwksCache(stub_issuer.jwks_url, timeout=1.0)))
    token = sign(claims(), rsa_key(), kid="k1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        me = asyncio.create_task(http.get("/me", headers={"Authorization": f"Bearer {token}"}))
        await wait_until(lambda: stub_issuer.jwks_fetches == 1)
        started = time.monotonic()
        health = await http.get("/healthz")
        health_seconds = time.monotonic() - started
        assert not me.done()
        rejected = await me

    assert health.status_code == 200 and health_seconds < 0.5
    assert rejected.status_code == 401


def test_dev_mode_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="auth.dependencies")

    log_dev_mode_warning()

    [record] = events(caplog, "auth.mode.dev_enabled")
    assert record.levelno == logging.WARNING


async def test_token_content_never_reaches_logs_or_errors(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    tokens = [
        _dev_token(sub=f"dev:{CANARY}", email=f"{CANARY}@evil.example"),
        _dev_token(email_verified=False, name=CANARY),
        _dev_token(iss=f"https://{CANARY}.example"),
        _dev_token(exp=int(time.time()) - 3600, sub=CANARY),
        sign(claims(sub=CANARY), "w" * 40, kid=CANARY, alg="HS256"),
        sign(claims(sub=CANARY), rsa_key(), kid=CANARY),
        f"{CANARY}.{CANARY}.{CANARY}",
    ]
    verifier = DevTokenVerifier(DEV_POLICY, SecretStr(DEV_KEY))
    errors: list[AuthError] = []
    for token in tokens:
        response = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code in (401, 403)
        assert CANARY not in response.text and token not in response.text
        with pytest.raises(AuthError) as caught:
            await verifier.verify(token)
        errors.append(caught.value)

    captured = capsys.readouterr()
    haystack = "\n".join(
        [caplog.text, captured.out, captured.err]
        + [repr(vars(r)) for r in caplog.records]
        + [f"{e!s} {e!r} {e.__context__!r} {e.__cause__!r}" for e in errors]
    )
    assert CANARY not in haystack
    assert all(e.__context__ is None and e.__cause__ is None for e in errors)
    assert DEV_KEY not in haystack
    for token in tokens:
        assert token not in haystack
