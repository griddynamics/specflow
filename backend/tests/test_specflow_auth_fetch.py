"""Hardened JWKS/discovery fetch: size cap, overall deadline, no redirects.

All servers are loopback ``StubIssuer``s (127.0.0.1, random port).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from auth import jwks as jwks_module
from auth.errors import AuthError
from auth.fetch import FetchError, fetch_json
from auth.jwks import JwksCache
from logger_harness import events, fields
from specflow_auth_support import (
    FakeClock,
    StubIssuer,
    jwk,
    rsa_key,
)


def _failure(url: str, timeout: float = 2.0) -> tuple[str, float]:
    started = time.monotonic()
    with pytest.raises(FetchError) as caught:
        fetch_json(url, timeout=timeout)
    return caught.value.failure, time.monotonic() - started


@pytest.mark.parametrize("mode", ["huge_declared", "huge_streamed"])
def test_an_oversized_body_fails_closed(stub_issuer: StubIssuer, mode: str) -> None:
    stub_issuer.mode = mode

    failure, elapsed = _failure(stub_issuer.jwks_url)

    assert failure == "too_large" and elapsed < 2.0


def test_a_slow_drip_ends_at_the_overall_deadline(stub_issuer: StubIssuer) -> None:
    stub_issuer.mode = "drip"

    failure, elapsed = _failure(stub_issuer.jwks_url, timeout=1.0)

    assert failure == "timeout"
    assert 0.9 <= elapsed < 1.6


def test_a_hanging_server_ends_at_the_overall_deadline(stub_issuer: StubIssuer) -> None:
    stub_issuer.mode = "hang"

    failure, elapsed = _failure(stub_issuer.jwks_url, timeout=1.0)

    assert failure == "timeout" and elapsed < 1.6


def test_redirects_are_never_followed(stub_issuer: StubIssuer) -> None:
    other = StubIssuer()
    try:
        stub_issuer.mode = "redirect"
        stub_issuer.redirect_to = other.jwks_url

        failure, _ = _failure(stub_issuer.jwks_url)

        assert failure == "redirect_refused"
        assert other.jwks_fetches == 0
    finally:
        other.close()


@pytest.mark.parametrize(
    ("path", "failure"),
    [("/nope", "http_status"), ("/realms/t/.well-known/openid-configuration", None)],
)
def test_status_and_json_checks(stub_issuer: StubIssuer, path: str, failure: str | None) -> None:
    if failure is None:
        assert isinstance(fetch_json(stub_issuer.base + path, timeout=2.0), dict)
    else:
        assert _failure(stub_issuer.base + path)[0] == failure


@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://127.0.0.1/x", "http:///x"])
def test_only_http_urls_are_fetched(url: str) -> None:
    assert _failure(url)[0] == "invalid_url"


async def test_a_discovery_redirect_cannot_bypass_the_host_check(
    stub_issuer: StubIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="auth.jwks")
    other = StubIssuer()
    try:
        stub_issuer.discovery_redirect = other.issuer + "/.well-known/openid-configuration"
        cache = JwksCache(issuer=stub_issuer.issuer, clock=FakeClock())

        with pytest.raises(AuthError) as caught:
            await cache.signing_key("k1")

        assert caught.value.reason == "jwks_unavailable"
        assert other.discovery_fetches == other.jwks_fetches == 0
        [record] = events(caplog, "auth.jwks.refetch")
        assert fields(record)["failure"] == "redirect_refused"
    finally:
        other.close()


async def test_repeated_drips_leave_the_executor_free(
    stub_issuer: StubIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="auth.jwks")
    stub_issuer.mode = "drip"
    clock = FakeClock()
    cache = JwksCache(stub_issuer.jwks_url, timeout=0.5, clock=clock)

    for _ in range(4):
        started = time.monotonic()
        with pytest.raises(AuthError):
            await cache.signing_key("k1")
        assert time.monotonic() - started < 1.0
        clock.advance(61)

    probe = asyncio.get_running_loop().run_in_executor(jwks_module._EXECUTOR, lambda: 1)
    assert await asyncio.wait_for(probe, timeout=1.0) == 1
    workers = [t for t in threading.enumerate() if t.name.startswith("specflow-jwks")]
    assert len(workers) <= 2
    failures = {fields(r)["failure"] for r in events(caplog, "auth.jwks.refetch")}
    assert failures == {"timeout"}


async def test_a_good_key_set_still_loads_through_the_bounded_client(
    stub_issuer: StubIssuer,
) -> None:
    key = rsa_key()
    stub_issuer.keys = [jwk(key, "k1"), {**jwk(key, "enc"), "use": "enc"}]
    cache = JwksCache(stub_issuer.jwks_url, clock=FakeClock())

    assert (await cache.signing_key("k1")).key_id == "k1"
    assert set(cache._keys) == {"k1"}
