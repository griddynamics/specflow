"""FR-MCP-AUTH-07: the verifier hands the signed JOSE header to claim validation (rfc9068)."""

from __future__ import annotations

import base64
import json

import jwt
import pytest

from auth.claims import ClaimPolicy
from auth.errors import AuthError
from auth.jwks import JwksCache
from auth.verifiers import OidcJwtVerifier
from specflow_auth_support import (
    AUDIENCE,
    CLIENT_ID,
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


@pytest.fixture
def oidc(stub_issuer: StubIssuer) -> OidcJwtVerifier:
    stub_issuer.keys = [jwk(RSA, "rsa")]
    return OidcJwtVerifier(_policy(ISSUER), JwksCache(stub_issuer.jwks_url, clock=FakeClock()))


@pytest.mark.parametrize("typ", ["at+jwt", "application/at+jwt"])
async def test_the_oidc_verifier_passes_the_header_typ(oidc: OidcJwtVerifier, typ: str) -> None:
    good = jwt.encode(claims(typ=None), RSA, algorithm="RS256", headers={"kid": "rsa", "typ": typ})
    plain = jwt.encode(claims(typ=None), RSA, algorithm="RS256", headers={"kid": "rsa"})
    other = jwt.encode(claims(typ=None), RSA, algorithm="RS256", headers={"kid": "rsa", "typ": "JWT"})

    assert (await oidc.verify(good)).subject == "user-123"
    for refused in (plain, other):
        with pytest.raises(AuthError) as raised:
            await oidc.verify(refused)
        assert raised.value.reason == "typ"


async def test_the_header_cannot_be_forged_without_the_signature(oidc: OidcJwtVerifier) -> None:
    token = jwt.encode(claims(typ=None), RSA, algorithm="RS256", headers={"kid": "rsa", "typ": "JWT"})
    _, body, signature = token.split(".")
    header = {"alg": "RS256", "kid": "rsa", "typ": "at+jwt"}
    forged_header = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=")
    forged = f"{forged_header.decode()}.{body}.{signature}"

    with pytest.raises(AuthError) as raised:
        await oidc.verify(forged)
    assert raised.value.reason == "signature"
