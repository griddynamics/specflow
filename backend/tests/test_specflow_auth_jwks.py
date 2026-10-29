"""AC-6.3: JWKS cache, unknown-kid throttle, rotation, discovery, hang safety.

All fetches go to the loopback ``StubIssuer`` (127.0.0.1, random port).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time

import pytest

from auth import jwks as jwks_module
from auth.errors import AuthError
from auth.jwks import JwksCache
from auth.verifiers import OidcJwtVerifier
from logger_harness import events, fields
from specflow_auth_support import (
    POLICY,
    FakeClock,
    StubIssuer,
    claims,
    jwk,
    rsa_key,
    sign,
)

TICK_SECONDS = 0.05  # the heartbeat of the loop-liveness probe
KEY = rsa_key()
ROTATED = rsa_key()


def _verifier(stub: StubIssuer, clock: FakeClock, **kwargs: float) -> OidcJwtVerifier:
    stub.keys = [jwk(KEY, "k1")]
    return OidcJwtVerifier(POLICY, JwksCache(stub.jwks_url, clock=clock, **kwargs))


async def test_a_known_kid_is_served_from_cache_for_ten_minutes(
    stub_issuer: StubIssuer,
) -> None:
    clock = FakeClock()
    verifier = _verifier(stub_issuer, clock)
    token = sign(claims(), KEY, kid="k1")

    for _ in range(100):
        await verifier.verify(token)
    clock.advance(599)
    await verifier.verify(token)
    assert stub_issuer.jwks_fetches == 1

    clock.advance(2)  # the 600 s lifespan is over: one scheduled refetch
    await verifier.verify(token)
    assert stub_issuer.jwks_fetches == 2


async def test_100_tokens_with_one_unknown_kid_cause_at_most_one_fetch(
    stub_issuer: StubIssuer,
) -> None:
    clock = FakeClock()
    verifier = _verifier(stub_issuer, clock)
    await verifier.verify(sign(claims(), KEY, kid="k1"))  # warm cache
    clock.advance(10)  # past the global minimum refetch interval
    forged = sign(claims(), rsa_key(), kid="unknown")

    results = await asyncio.gather(*(verifier.verify(forged) for _ in range(100)), return_exceptions=True)
    clock.advance(59)
    for _ in range(100):
        with pytest.raises(AuthError):
            await verifier.verify(forged)

    assert all(isinstance(r, AuthError) and r.status == 401 for r in results)
    assert stub_issuer.jwks_fetches == 1 + 1

    clock.advance(2)  # the 60 s negative entry has expired: one more refetch
    with pytest.raises(AuthError):
        await verifier.verify(forged)
    assert stub_issuer.jwks_fetches == 3


async def test_an_unknown_kid_on_a_cold_cache_fetches_once(
    stub_issuer: StubIssuer,
) -> None:
    verifier = _verifier(stub_issuer, FakeClock())
    forged = sign(claims(), rsa_key(), kid="unknown")

    results = await asyncio.gather(*(verifier.verify(forged) for _ in range(100)), return_exceptions=True)

    assert {getattr(r, "reason", None) for r in results} == {"unknown_kid"}
    assert stub_issuer.jwks_fetches == 1


async def test_a_rotated_key_is_picked_up_with_one_refetch(
    stub_issuer: StubIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="auth.jwks")
    clock = FakeClock()
    verifier = _verifier(stub_issuer, clock)
    await verifier.verify(sign(claims(), KEY, kid="k1"))

    clock.advance(10)  # a new kid after the global interval: one refetch
    stub_issuer.keys = [jwk(ROTATED, "k2")]
    principal = await verifier.verify(sign(claims(), ROTATED, kid="k2"))

    assert principal.subject == "user-123"
    assert stub_issuer.jwks_fetches == 2
    refetches = [fields(r) for r in events(caplog, "auth.jwks.refetch")]
    assert [(f["reason"], f["outcome"]) for f in refetches] == [
        ("initial", "ok"),
        ("unknown_kid", "ok"),
    ]
    assert refetches[1]["key_count"] == 1


async def test_a_hanging_jwks_fails_in_about_5s_without_blocking_the_loop(
    stub_issuer: StubIssuer,
) -> None:
    stub_issuer.mode = "hang"
    verifier = OidcJwtVerifier(POLICY, JwksCache(stub_issuer.jwks_url))
    token = sign(claims(), KEY, kid="k1")
    ticks: list[float] = []

    async def ticker() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(TICK_SECONDS)

    tick_task = asyncio.create_task(ticker())
    started = time.monotonic()
    results = await asyncio.gather(*(verifier.verify(token) for _ in range(10)), return_exceptions=True)
    elapsed = time.monotonic() - started
    tick_task.cancel()

    assert all(isinstance(r, AuthError) and r.status == 401 for r in results)
    assert {r.reason for r in results} == {"jwks_unavailable"}
    assert 4.0 <= elapsed <= 6.5
    assert stub_issuer.jwks_fetches == 1  # waiters share the one failed fetch
    gaps = [b - a for a, b in itertools.pairwise(ticks)]
    assert len(ticks) >= 60 and max(gaps) < 0.5


async def test_jwks_url_is_discovered_from_the_issuer_once(
    stub_issuer: StubIssuer,
) -> None:
    stub_issuer.keys = [jwk(KEY, "k1")]
    clock = FakeClock()
    verifier = OidcJwtVerifier(POLICY, JwksCache(issuer=stub_issuer.issuer, clock=clock))
    token = sign(claims(), KEY, kid="k1")

    await verifier.verify(token)
    clock.advance(601)
    await verifier.verify(token)

    assert (stub_issuer.discovery_fetches, stub_issuer.jwks_fetches) == (1, 2)


async def test_a_discovered_jwks_uri_on_another_host_is_refused(
    stub_issuer: StubIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="auth.jwks")
    stub_issuer.jwks_uri_override = "http://localhost:1/jwks"
    verifier = OidcJwtVerifier(POLICY, JwksCache(issuer=stub_issuer.issuer))

    with pytest.raises(AuthError) as caught:
        await verifier.verify(sign(claims(), KEY, kid="k1"))

    assert caught.value.reason == "jwks_unavailable"
    assert stub_issuer.jwks_fetches == 0
    [record] = events(caplog, "auth.jwks.refetch")
    assert (record.levelno, fields(record)["outcome"]) == (logging.WARNING, "failed")


async def test_the_negative_cache_is_bounded(stub_issuer: StubIssuer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jwks_module, "MAX_NEGATIVE_KIDS", 3)
    clock = FakeClock()
    cache = JwksCache(stub_issuer.jwks_url, clock=clock)
    stub_issuer.keys = [jwk(KEY, "k1")]

    for n in range(5):
        clock.advance(10)
        with pytest.raises(AuthError):
            await cache.signing_key(f"bad-{n}")

    assert len(cache._negative) == 3
    assert list(cache._negative) == ["bad-2", "bad-3", "bad-4"]


async def test_distinct_unknown_kids_share_one_global_refetch_interval(
    stub_issuer: StubIssuer,
) -> None:
    clock = FakeClock()
    verifier = _verifier(stub_issuer, clock)
    await verifier.verify(sign(claims(), KEY, kid="k1"))
    attacker = rsa_key()

    started = time.monotonic()
    for n in range(50):
        error = await _rejected(verifier, sign(claims(), attacker, kid=f"random-{n}"))
        assert error.reason == "refetch_throttled"
    assert time.monotonic() - started < 2.0  # immediate, no waiting
    assert stub_issuer.jwks_fetches == 1

    clock.advance(9.9)
    await _rejected(verifier, sign(claims(), attacker, kid="random-0"))
    assert stub_issuer.jwks_fetches == 1  # throttled kids are not negative-cached
    clock.advance(0.1)
    stub_issuer.keys = [jwk(KEY, "k1"), jwk(ROTATED, "k2")]
    assert await verifier.verify(sign(claims(), ROTATED, kid="k2"))
    assert stub_issuer.jwks_fetches == 2


async def test_failed_fetches_back_off_doubling_to_60s_and_reset_on_success(
    stub_issuer: StubIssuer,
) -> None:
    clock = FakeClock()
    verifier = _verifier(stub_issuer, clock)
    cache = verifier._jwks
    token = sign(claims(), KEY, kid="k1")
    stub_issuer.mode = "redirect"  # fails at once, without network beyond the stub

    expected = [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    for backoff in expected:
        assert (await _rejected(verifier, token)).reason == "jwks_unavailable"
        assert cache._backoff == backoff
        clock.advance(backoff - 0.1)
        fetches = stub_issuer.jwks_fetches
        assert (await _rejected(verifier, token)).reason == "jwks_unavailable"
        assert stub_issuer.jwks_fetches == fetches  # inside the window: no fetch
        clock.advance(0.1)
    assert stub_issuer.jwks_fetches == len(expected)

    stub_issuer.mode = "ok"
    assert await verifier.verify(token)
    assert cache._backoff == 0.0 and stub_issuer.jwks_fetches == len(expected) + 1


async def _rejected(verifier: OidcJwtVerifier, token: str) -> AuthError:
    with pytest.raises(AuthError) as caught:
        await verifier.verify(token)
    assert caught.value.status == 401
    return caught.value


def test_a_cache_needs_a_url_or_an_issuer() -> None:
    with pytest.raises(ValueError, match="needs a JWKS URL"):
        JwksCache()
