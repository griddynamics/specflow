"""``integrations.safe_fetch.fetch_public`` offline: a monkeypatched resolver and ``httpx.MockTransport``, no network.

Sequence::

    url -> https/port/credentials check -> resolve (every address checked) -> GET to the checked IP (Host, SNI)
    3xx -> Location -> the same again (at most 3) | 2xx -> content type -> streamed body under the cap
    every failure -> SafeFetchError(closed code), raised from None
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import socket
import threading
import traceback
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from integrations import safe_fetch
from integrations.safe_fetch import SAFE_FETCH_CODES, SafeFetchError, fetch_public, is_forbidden

HOST = "files.example.com"
URL = f"https://{HOST}/secret-path/a.drawio?token=canary-9c1d"
PUBLIC = "93.184.216.34"
GAIERROR = "gaierror"
Handler = Callable[[httpx.Request], httpx.Response]


def resolving(monkeypatch, table: dict[str, Any] | None = None) -> list[tuple[str, int]]:
    """A resolver answering ``table`` (host -> addresses, or ``GAIERROR``); any other host is ``PUBLIC``."""

    asked: list[tuple[str, int]] = []
    answers = table or {}

    async def resolve(host: str, port: int) -> list[str]:
        asked.append((host, port))
        found = answers.get(host, [PUBLIC])
        if found == GAIERROR:
            raise OSError("no such host")
        return found

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    return asked


async def get(handler: Handler, url: str = URL, *, max_bytes: int = 1000, timeout_s: float = 5) -> bytes:
    return await fetch_public(url, max_bytes=max_bytes, timeout_s=timeout_s, transport=httpx.MockTransport(handler))


async def code_of(handler: Handler, url: str = URL, **kwargs: Any) -> str:
    with pytest.raises(SafeFetchError) as caught:
        await get(handler, url, **kwargs)
    return caught.value.code


def reply(body: bytes = b"", status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    """A response whose body is still a stream, as a real one is (``Response(content=...)`` is already consumed)."""

    return httpx.Response(status, headers=headers or {}, stream=httpx.ByteStream(body))


def ok(body: bytes = b"<mxfile/>", media: str | None = "application/xml") -> Handler:
    headers = {"content-type": media} if media else {}
    return lambda _request: reply(body, headers=headers)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.9.9.9",
        "10.0.0.1",
        "172.16.5.5",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "169.254.1.1",
        "100.64.0.1",
        "100.127.255.254",
        ".".join(["0"] * 4),
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.0.1",
        "198.18.0.1",
        "::1",
        "::",
        "fe80::1",
        "fec0::1",
        "fc00::1",
        "fd00:ec2::254",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a00:1",
        "2002:7f00:1::",
        "2001:db8::1",
    ],
)
def test_every_forbidden_range_is_refused(address) -> None:
    assert is_forbidden(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_a_public_address_is_allowed(address) -> None:
    assert not is_forbidden(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.1.1", "::1", "fd00:ec2::254"])
async def test_a_host_that_resolves_to_a_forbidden_address_is_refused_before_any_request(monkeypatch, address) -> None:
    resolving(monkeypatch, {HOST: [address]})
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return reply()

    assert await code_of(handler) == "blocked_address" and sent == []


async def test_one_private_address_among_public_ones_refuses_the_host(monkeypatch) -> None:
    resolving(monkeypatch, {HOST: [PUBLIC, "10.0.0.5"]})

    assert await code_of(ok()) == "blocked_address"


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "169.254.169.254", "[fd00:ec2::254]", "10.0.0.1"])
async def test_a_literal_forbidden_ip_is_refused(monkeypatch, host) -> None:
    asked = resolving(monkeypatch)

    assert await code_of(ok(), f"https://{host}/a.drawio") == "blocked_address" and asked == []


async def test_the_connection_goes_to_the_checked_ip_with_the_host_header_and_sni(monkeypatch) -> None:
    resolving(monkeypatch, {HOST: ["2606:4700:4700::1111", PUBLIC]})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(b"x", headers={"content-type": "text/plain"})

    assert await get(handler) == b"x"
    (request,) = seen
    assert request.url.host == "2606:4700:4700::1111" and request.url.path == "/secret-path/a.drawio"
    assert request.url.query == b"token=canary-9c1d"
    assert request.headers["host"] == HOST and request.extensions["sni_hostname"] == HOST
    assert "cookie" not in request.headers and "authorization" not in request.headers


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example.com/a.drawio",
        "ftp://files.example.com/a",
        "file:///etc/passwd",
        "//files.example.com/a",
        "files.example.com/a",
        "https://user@files.example.com/a",
        "https://user:pw@files.example.com/a",
        "https://files.example.com:8443/a",
        "https://files.example.com:80/a",
        "https:///a",
        "",
    ],
)
async def test_a_url_that_is_not_plain_https_is_invalid(monkeypatch, url) -> None:
    resolving(monkeypatch)

    assert await code_of(ok(), url) == "invalid_url"


async def test_a_name_that_does_not_resolve_is_unreachable(monkeypatch) -> None:
    resolving(monkeypatch, {HOST: GAIERROR})

    assert await code_of(ok()) == "unreachable"


async def test_a_name_that_resolves_to_nothing_is_unreachable(monkeypatch) -> None:
    resolving(monkeypatch, {HOST: []})

    assert await code_of(ok()) == "unreachable"


async def test_a_redirect_is_followed_and_checked_again(monkeypatch) -> None:
    asked = resolving(monkeypatch)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        if request.headers["host"] == HOST:
            return httpx.Response(302, headers={"location": "https://cdn.example.org/b.drawio"})
        return reply(b"<mxfile/>")

    assert await get(handler) == b"<mxfile/>"
    assert hosts == [HOST, "cdn.example.org"] and [h for h, _ in asked] == [HOST, "cdn.example.org"]


async def test_a_relative_redirect_stays_on_the_host(monkeypatch) -> None:
    resolving(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("a.drawio"):
            return httpx.Response(301, headers={"location": "/other/b.drawio"})
        return reply(b"ok", headers={"content-type": "text/plain"})

    assert await get(handler) == b"ok"


@pytest.mark.parametrize("target", ["127.0.0.1", "10.0.0.8", "169.254.169.254", "::1"])
async def test_a_redirect_to_a_host_that_resolves_private_is_refused(monkeypatch, target) -> None:
    resolving(monkeypatch, {HOST: [PUBLIC], "internal.example.org": [target]})
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "https://internal.example.org/x"})

    assert await code_of(handler) == "blocked_address" and calls == [HOST]


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("http://files.example.com/a", "invalid_url"),
        ("https://127.0.0.1/a", "blocked_address"),
        ("https://[::1]/a", "blocked_address"),
        ("https://u:p@x.example.com/", "invalid_url"),
    ],
)
async def test_a_redirect_to_a_bad_or_literal_private_url_is_refused(monkeypatch, location, code) -> None:
    resolving(monkeypatch)

    assert await code_of(lambda _request: httpx.Response(302, headers={"location": location})) == code


async def test_a_redirect_without_a_location_is_a_bad_status(monkeypatch) -> None:
    resolving(monkeypatch)

    assert await code_of(lambda _request: httpx.Response(302)) == "bad_status"


@pytest.mark.parametrize(("chain", "read"), [(3, True), (4, False)])
async def test_three_redirects_are_followed_and_a_fourth_is_not(monkeypatch, chain, read) -> None:
    resolving(monkeypatch)
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count <= chain:
            return httpx.Response(302, headers={"location": f"https://files.example.com/{count}"})
        return reply(b"done")

    if read:
        assert await get(handler) == b"done"
    else:
        assert await code_of(handler) == "bad_status" and count == 4


async def test_a_redirect_loop_stops(monkeypatch) -> None:
    resolving(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": URL})

    assert await code_of(handler) == "bad_status" and calls == 4


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
async def test_a_status_that_is_not_2xx_is_bad_status(monkeypatch, status) -> None:
    resolving(monkeypatch)

    assert await code_of(lambda _request: httpx.Response(status)) == "bad_status"


@pytest.mark.parametrize(
    "media",
    [
        "application/xml",
        "text/xml; charset=utf-8",
        "text/plain",
        "application/octet-stream",
        "image/svg+xml",
        "image/png",
        "application/vnd.jgraph.mxfile",
        "TEXT/PLAIN",
        None,
    ],
)
async def test_an_allowed_or_missing_content_type_is_read(monkeypatch, media) -> None:
    resolving(monkeypatch)

    assert await get(ok(b"abc", media)) == b"abc"


@pytest.mark.parametrize("media", ["text/html", "application/json", "application/pdf", "image/jpeg", "video/mp4"])
async def test_any_other_content_type_is_bad_type(monkeypatch, media) -> None:
    resolving(monkeypatch)

    assert await code_of(ok(b"abc", media)) == "bad_type"


async def test_a_body_over_the_cap_is_too_large_even_without_a_length(monkeypatch) -> None:
    resolving(monkeypatch)
    pulled = 0

    async def stream() -> Any:
        nonlocal pulled
        for _ in range(1000):
            pulled += 1
            yield b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream(), headers={"content-type": "text/plain"})

    assert await code_of(handler, max_bytes=250) == "too_large"
    assert pulled < 10  # stopped reading at the cap, not after the whole body


async def test_a_declared_length_over_the_cap_is_too_large_and_the_cap_itself_is_read(monkeypatch) -> None:
    resolving(monkeypatch)

    assert await code_of(ok(b"x" * 11), max_bytes=10) == "too_large"
    assert await get(ok(b"x" * 10), max_bytes=10) == b"x" * 10


async def test_the_request_asks_for_an_identity_encoding(monkeypatch) -> None:
    resolving(monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["accept-encoding"])
        return reply(b"abc")

    assert await get(handler) == b"abc" and seen == ["identity"]


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "zstd", "gzip, identity"])
async def test_a_compressed_response_is_refused_before_it_is_read(monkeypatch, encoding) -> None:
    resolving(monkeypatch)
    packed = gzip.compress(b"a" * 100_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return reply(packed, headers={"content-encoding": encoding, "content-type": "text/plain"})

    assert len(packed) < 1000 and await code_of(handler, max_bytes=1000) == "bad_encoding"


@pytest.mark.parametrize("encoding", ["identity", "Identity", ""])
async def test_an_identity_content_encoding_is_read(monkeypatch, encoding) -> None:
    resolving(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return reply(b"abc", headers={"content-encoding": encoding, "content-type": "text/plain"})

    assert await get(handler) == b"abc"


async def test_the_cap_counts_raw_bytes(monkeypatch) -> None:
    resolving(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return reply(b"x" * 11, headers={"content-type": "text/plain"})

    assert await code_of(handler, max_bytes=10) == "too_large"


async def test_at_most_four_fetches_run_at_once(monkeypatch) -> None:
    resolving(monkeypatch)
    running = peak = 0
    release, full = asyncio.Event(), asyncio.Event()

    async def stream() -> Any:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == safe_fetch.MAX_CONCURRENT:
            full.set()
        await release.wait()
        yield b"abc"
        running -= 1

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=_Stream(stream()))

    fetches = asyncio.gather(*(get(handler) for _ in range(12)))
    await full.wait()
    for _ in range(200):  # a fifth fetch, if the gate let it, would be inside by now
        await asyncio.sleep(0)
    held = running
    release.set()

    assert await fetches == [b"abc"] * 12
    assert held == peak == safe_fetch.MAX_CONCURRENT == 4


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: Any) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> Any:
        async for chunk in self._chunks:
            yield chunk


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ConnectTimeout("timed out https://files.example.com/secret"), "timeout"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused for https://files.example.com/secret"), "unreachable"),
        (httpx.ReadError("reset"), "unreachable"),
        (httpx.RemoteProtocolError("bad"), "unreachable"),
        (httpx.TooManyRedirects("loop"), "unreachable"),
        (OSError("Network is unreachable files.example.com"), "unreachable"),
        (ConnectionResetError("reset"), "unreachable"),
    ],
)
async def test_every_transport_error_is_a_closed_code_without_the_url(monkeypatch, error, code) -> None:
    resolving(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(SafeFetchError) as caught:
        await get(handler)

    failure = caught.value
    assert failure.code == code and failure.code in SAFE_FETCH_CODES
    assert failure.__cause__ is None and failure.__suppress_context__
    rendered = "".join(traceback.format_exception(failure))
    for canary in ("canary-9c1d", "secret-path", "files.example.com", "secret"):
        assert canary not in rendered and canary not in str(failure)


async def test_a_refusal_message_is_only_its_code(monkeypatch) -> None:
    resolving(monkeypatch, {HOST: ["10.0.0.1"]})

    with pytest.raises(SafeFetchError) as caught:
        await get(ok())

    assert str(caught.value) == "blocked_address"
    assert "canary-9c1d" not in "".join(traceback.format_exception(caught.value))


async def test_the_overall_deadline_stops_a_slow_resolution(monkeypatch) -> None:
    async def slow(host: str, port: int) -> list[str]:
        await asyncio.Event().wait()  # never set: only the deadline ends it
        return [PUBLIC]

    monkeypatch.setattr(safe_fetch, "_resolve", slow)

    assert await code_of(ok(), timeout_s=0.05) == "timeout"


async def test_a_failed_resolution_does_not_leak_the_host(monkeypatch) -> None:
    resolving(monkeypatch, {HOST: GAIERROR})

    with pytest.raises(SafeFetchError) as caught:
        await get(ok())

    assert HOST not in "".join(traceback.format_exception(caught.value))


async def test_the_resolver_runs_off_the_event_loop_thread(monkeypatch) -> None:
    main = threading.get_ident()
    threads: list[int] = []

    def getaddrinfo(host: str, port: int, family: int, kind: int) -> list[Any]:
        threads.append(threading.get_ident())
        return [(socket.AF_INET, kind, 6, "", (PUBLIC, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    assert await get(ok(b"abc", "text/plain")) == b"abc" and threads and threads[0] != main
