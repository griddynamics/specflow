"""FR-MCP-AUTH-07: both verifiers hand the signed JOSE header to claim validation (rfc9068)."""

from __future__ import annotations

import base64
import json

import jwt
import pytest
from pydantic import SecretStr

from auth.claims import ClaimPolicy
from auth.errors import AuthError
from auth.jwks import JwksCache
from auth.verifiers import DEV_ISSUER, DevTokenVerifier, OidcJwtVerifier
from specflow_auth_support import (
    AUDIENCE,
    CLIENT_ID,
    DEV_KEY,
    DOMAIN,
    ISSUER,
    FakeClock,
    StubIssuer,
    claims,
    jwk,
    rsa_key,
)

RSA = rsa_key()


def _policy(issuer: str) -> ClaimPolicy:
    return ClaimPolicy.build(
        issuer=issuer,
        audience=AUDIENCE,
        client_ids=[CLIENT_ID],
        allowed_email_domains=[DOMAIN],
        typ_rule="rfc9068",
    )


def _hs256(payload: dict, headers: dict | None) -> str:
    return jwt.encode(payload, DEV_KEY, algorithm="HS256", headers=headers)


@pytest.fixture
def oidc(stub_issuer: StubIssuer) -> OidcJwtVerifier:
    stub_issuer.keys = [jwk(RSA, "rsa")]
    return OidcJwtVerifier(_policy(ISSUER), JwksCache(stub_issuer.jwks_url, clock=FakeClock()))


async def test_the_oidc_verifier_passes_the_header_typ(oidc: OidcJwtVerifier) -> None:
    good = jwt.encode(
        claims(typ=None),
        RSA,
        algorithm="RS256",
        headers={"kid": "rsa", "typ": "at+jwt"},
    )
    plain = jwt.encode(claims(typ=None), RSA, algorithm="RS256", headers={"kid": "rsa"})

    assert (await oidc.verify(good)).subject == "user-123"
    with pytest.raises(AuthError) as raised:
        await oidc.verify(plain)
    assert raised.value.reason == "typ"


@pytest.mark.parametrize("typ", ["at+jwt", "application/at+jwt"])
async def test_the_dev_verifier_passes_the_header_typ(typ: str) -> None:
    verifier = DevTokenVerifier(_policy(DEV_ISSUER), SecretStr(DEV_KEY))
    payload = claims(iss=DEV_ISSUER, typ=None)

    assert (await verifier.verify(_hs256(payload, {"typ": typ}))).subject == "user-123"
    with pytest.raises(AuthError) as raised:
        await verifier.verify(_hs256(payload, {"typ": "JWT"}))
    assert raised.value.reason == "typ"


async def test_the_header_cannot_be_forged_without_the_signature() -> None:
    verifier = DevTokenVerifier(_policy(DEV_ISSUER), SecretStr(DEV_KEY))
    token = _hs256(claims(iss=DEV_ISSUER, typ=None), {"typ": "JWT"})
    _, body, signature = token.split(".")
    forged_header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "at+jwt"}).encode()).rstrip(b"=")
    forged = f"{forged_header.decode()}.{body}.{signature}"

    with pytest.raises(AuthError) as raised:
        await verifier.verify(forged)
    assert raised.value.reason == "signature"
