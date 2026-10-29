"""AC-6.2 and AC-1.4 (backend part): algorithm pinning and the dev verifier."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from pydantic import SecretStr

from auth.claims import ClaimPolicy
from auth.errors import AuthError
from auth.jwks import JwksCache
from auth.verifiers import (
    DEV_ISSUER,
    DevTokenVerifier,
    OidcJwtVerifier,
    TokenVerifier,
    build_verifier,
)
from specflow_auth_support import (
    DEV_KEY,
    ISSUER,
    POLICY,
    FakeClock,
    StubIssuer,
    auth_settings,
    claims,
    ec_key,
    jwk,
    rsa_key,
    sign,
)

RSA = rsa_key()
EC = ec_key()
DEV_POLICY = ClaimPolicy.build(
    issuer=DEV_ISSUER,
    audience=POLICY.audience,
    client_ids=POLICY.client_ids,
    allowed_email_domains=POLICY.allowed_email_domains,
)


@pytest.fixture
def oidc(stub_issuer: StubIssuer) -> OidcJwtVerifier:
    """Built directly, never through ``load_settings`` (rev 2.1, D10)."""

    stub_issuer.keys = [jwk(RSA, "rsa"), jwk(EC, "ec")]
    return OidcJwtVerifier(POLICY, JwksCache(stub_issuer.jwks_url, clock=FakeClock()))


async def _rejected(verifier: TokenVerifier, token: str) -> AuthError:
    with pytest.raises(AuthError) as caught:
        await verifier.verify(token)
    assert caught.value.status == 401
    return caught.value


def _unsigned(payload: dict[str, object], header: dict[str, object]) -> str:
    def part(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part(header)}.{part(payload)}."


async def test_oidc_accepts_rs256_and_es256(oidc: OidcJwtVerifier) -> None:
    rs = await oidc.verify(sign(claims(), RSA, kid="rsa"))
    es = await oidc.verify(sign(claims(), EC, kid="ec", alg="ES256"))

    assert rs.subject == es.subject == "user-123"


async def test_oidc_rejects_hs256_signed_with_a_dev_style_key(oidc: OidcJwtVerifier, stub_issuer: StubIssuer) -> None:
    token = sign(claims(), DEV_KEY, kid="rsa", alg="HS256")

    assert (await _rejected(oidc, token)).reason == "algorithm"
    assert stub_issuer.jwks_fetches == 0  # rejected before any key lookup


async def test_oidc_rejects_hs256_keyed_with_the_public_key(
    oidc: OidcJwtVerifier,
) -> None:
    pem = RSA.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = {"alg": "HS256", "kid": "rsa", "typ": "JWT"}
    signing_input = _unsigned(claims(), header).rstrip(".")
    mac = hmac.new(pem, signing_input.encode(), hashlib.sha256).digest()
    token = signing_input + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    assert (await _rejected(oidc, token)).reason == "algorithm"


@pytest.mark.parametrize("alg", ["none", "None", "HS384", "HS512", "PS256", "RS512"])
async def test_oidc_rejects_every_algorithm_outside_the_allowlist(oidc: OidcJwtVerifier, alg: str) -> None:
    token = _unsigned(claims(), {"alg": alg, "kid": "rsa"})

    assert (await _rejected(oidc, token)).reason == "algorithm"


async def test_oidc_rejects_a_key_algorithm_mismatch(oidc: OidcJwtVerifier) -> None:
    token = sign(claims(), EC, kid="rsa", alg="ES256")  # ES256 against the RSA key

    assert (await _rejected(oidc, token)).reason in {"malformed", "signature"}


async def test_oidc_rejects_a_bad_signature_and_a_missing_kid(
    oidc: OidcJwtVerifier,
) -> None:
    forged = sign(claims(), rsa_key(), kid="rsa")
    no_kid = sign(claims(), RSA, kid=None)

    assert (await _rejected(oidc, forged)).reason == "signature"
    assert (await _rejected(oidc, no_kid)).reason == "kid_missing"


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "x" * 20000])
async def test_oidc_rejects_malformed_tokens(oidc: OidcJwtVerifier, token: str) -> None:
    assert (await _rejected(oidc, token)).reason == "malformed"


async def test_oidc_applies_the_shared_claim_rules(oidc: OidcJwtVerifier) -> None:
    expired = sign(claims(exp=int(time.time()) - 120), RSA, kid="rsa")
    unverified = sign(claims(email_verified=False), RSA, kid="rsa")

    assert (await _rejected(oidc, expired)).reason == "expired"
    with pytest.raises(AuthError) as caught:
        await oidc.verify(unverified)
    assert (caught.value.status, caught.value.code) == (403, "email_not_verified")


async def test_a_signed_token_with_a_non_finite_exp_is_rejected(
    oidc: OidcJwtVerifier,
) -> None:
    token = sign(claims(exp=float("inf")), RSA, kid="rsa")  # JSON "Infinity"

    assert (await _rejected(oidc, token)).reason == "exp_invalid"


def _dev_claims(**overrides: object) -> dict[str, object]:
    return claims(**{"iss": DEV_ISSUER, "sub": "dev:alice@example.com", **overrides})


async def test_dev_accepts_an_hs256_dev_token() -> None:
    verifier = DevTokenVerifier(DEV_POLICY, SecretStr(DEV_KEY))

    principal = await verifier.verify(sign(_dev_claims(), DEV_KEY, kid=None, alg="HS256"))

    assert (principal.issuer, principal.subject) == (
        DEV_ISSUER,
        "dev:alice@example.com",
    )


async def test_dev_rejects_rs256_from_an_unknown_issuer() -> None:
    verifier = DevTokenVerifier(DEV_POLICY, SecretStr(DEV_KEY))
    token = sign(claims(iss="https://unknown.example/realms/x"), RSA, kid="rsa")

    assert (await _rejected(verifier, token)).reason == "algorithm"


async def test_dev_rejects_a_wrong_key_issuer_audience_and_client() -> None:
    verifier = DevTokenVerifier(DEV_POLICY, SecretStr(DEV_KEY))

    def dev(key: str = DEV_KEY, **overrides: object) -> str:
        return sign(_dev_claims(**overrides), key, kid=None, alg="HS256")

    assert (await _rejected(verifier, dev("w" * 40))).reason == "signature"
    assert (await _rejected(verifier, dev(iss=ISSUER))).reason == "issuer"
    assert (await _rejected(verifier, dev(aud="other"))).reason == "audience"
    assert (await _rejected(verifier, dev(azp="specflow-claude-web"))).reason == "client"


@pytest.mark.parametrize("key", [None, "", "short", "k" * 31])
def test_the_dev_verifier_requires_a_32_byte_key(key: str | None) -> None:
    secret = SecretStr(key) if key is not None else None

    with pytest.raises(ValueError, match="at least 32 bytes"):
        DevTokenVerifier(DEV_POLICY, secret)


def test_build_verifier_refuses_dev_mode_with_a_short_key_without_guards() -> None:
    settings = auth_settings(AUTH_MODE="dev", AUTH_DEV_SIGNING_KEY="k" * 31)

    with pytest.raises(ValueError, match="AUTH_DEV_SIGNING_KEY") as caught:
        build_verifier(settings)
    assert "k" * 31 not in str(caught.value)


def test_build_verifier_picks_the_verifier_for_the_mode(
    stub_issuer: StubIssuer,
) -> None:
    dev = build_verifier(auth_settings(AUTH_MODE="dev", AUTH_DEV_SIGNING_KEY=DEV_KEY))
    oidc = build_verifier(auth_settings(AUTH_JWKS_URL=stub_issuer.jwks_url))
    discovered = build_verifier(auth_settings(OIDC_ISSUER=stub_issuer.issuer))

    assert isinstance(dev, DevTokenVerifier) and isinstance(dev, TokenVerifier)
    assert isinstance(oidc, OidcJwtVerifier) and isinstance(discovered, OidcJwtVerifier)
    assert stub_issuer.jwks_fetches == stub_issuer.discovery_fetches == 0  # lazy


async def test_a_dev_token_never_passes_the_oidc_verifier(
    stub_issuer: StubIssuer,
) -> None:
    stub_issuer.keys = [jwk(RSA, "rsa")]
    oidc = build_verifier(auth_settings(AUTH_JWKS_URL=stub_issuer.jwks_url))
    dev_token = jwt.encode(_dev_claims(iss=ISSUER), DEV_KEY, algorithm="HS256")

    assert (await _rejected(oidc, dev_token)).reason == "algorithm"
