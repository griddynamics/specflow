"""Fetch a public HTTPS URL a user chose (the ``#U`` diagrams.net link), safely.

``https`` only, port 443 only; the host is resolved and every loopback, private, link-local, reserved, multicast,
carrier-grade NAT and metadata address (IPv4, IPv6 and the IPv4 forms embedded in IPv6) is refused when ANY resolved
address is one; the connection goes to the checked IP with the ``Host`` header and the SNI of the host; at most 3
redirects, each resolved and checked again; ``trust_env=False``, no cookies, no auth; the body is streamed and capped at
``max_bytes`` of raw bytes (``Accept-Encoding: identity``; any other ``Content-Encoding`` is refused); at most 4 fetches
run at once in the process. Every failure is a ``SafeFetchError`` with a closed code, raised ``from None``: no message
and no exception context carries the URL.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import weakref
from typing import Final, Literal
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx

SafeFetchCode = Literal[
    "invalid_url", "blocked_address", "unreachable", "timeout", "bad_status", "too_large", "bad_type", "bad_encoding"
]
SAFE_FETCH_CODES: Final = (
    "invalid_url",
    "blocked_address",
    "unreachable",
    "timeout",
    "bad_status",
    "too_large",
    "bad_type",
    "bad_encoding",
)

MAX_REDIRECTS: Final = 3
MAX_CONCURRENT: Final = 4
HTTPS_PORT: Final = 443
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
ALLOWED_TYPES: Final = frozenset(
    {
        "application/xml",
        "text/xml",
        "text/plain",
        "application/octet-stream",
        "image/svg+xml",
        "image/png",
        "application/vnd.jgraph.mxfile",
    }
)
_BLOCKED_NETWORKS: Final = tuple(
    ipaddress.ip_network(net) for net in ("100.64.0.0/10", "169.254.169.254/32", "fd00:ec2::254/128", "64:ff9b::/96")
)
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _gate() -> asyncio.Semaphore:
    """The process-wide limit of ``MAX_CONCURRENT`` public fetches (one semaphore per running event loop)."""

    loop = asyncio.get_running_loop()
    if loop not in _gates:
        _gates[loop] = asyncio.Semaphore(MAX_CONCURRENT)
    return _gates[loop]


class SafeFetchError(Exception):
    """A fetch that stopped with a closed ``code``: ``too_large`` over ``max_bytes``, ``invalid_url`` for a URL that is
    not plain ``https``, ``blocked_address`` for a refused address (also on a redirect), ``bad_type`` for a content type
    that is not text, XML, SVG, PNG or octet-stream, ``bad_encoding`` for a response with a
    ``Content-Encoding`` other than identity, the rest as named. The message never carries the URL."""

    def __init__(self, code: SafeFetchCode) -> None:
        super().__init__(code)
        self.code: SafeFetchCode = code


def _embedded(ip: _Address) -> list[ipaddress.IPv4Address]:
    if isinstance(ip, ipaddress.IPv4Address):
        return []
    found = [ip.ipv4_mapped, ip.sixtofour]
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.teredo:
        found.extend(ip.teredo)
    return [v4 for v4 in found if v4 is not None]


def is_forbidden(ip: _Address) -> bool:
    """Whether a connection to ``ip`` is refused (also when an IPv4 address is embedded in it). An IPv4-mapped address
    reaches its IPv4 address, so that address alone decides (Python versions disagree on its ``is_global``)."""

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_forbidden(ip.ipv4_mapped)
    if any(is_forbidden(v4) for v4 in _embedded(ip)):
        return True
    flagged = (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
        or getattr(ip, "is_site_local", False)
    )
    return flagged or any(ip in net for net in _BLOCKED_NETWORKS if net.version == ip.version)


async def _resolve(host: str, port: int) -> list[str]:
    """The addresses of ``host`` (a thread runs ``getaddrinfo``); ``OSError`` when it does not resolve."""

    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


def _checked(url: str) -> SplitResult:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise SafeFetchError("invalid_url") from None
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise SafeFetchError("invalid_url")
    if port not in (None, HTTPS_PORT):
        raise SafeFetchError("invalid_url")
    return parts


async def _validated_ip(host: str) -> str:
    try:
        literal = ipaddress.ip_address(host)
        found = [str(literal)]
    except ValueError:
        try:
            found = await _resolve(host, HTTPS_PORT)
        except OSError:
            raise SafeFetchError("unreachable") from None
    if not found:
        raise SafeFetchError("unreachable")
    try:
        addresses = [ipaddress.ip_address(item) for item in found]
    except ValueError:
        raise SafeFetchError("unreachable") from None
    if any(is_forbidden(ip) for ip in addresses):
        raise SafeFetchError("blocked_address")
    return found[0]


def _target(parts: SplitResult, ip: str) -> str:
    netloc = f"[{ip}]" if ":" in ip else ip
    return f"https://{netloc}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")


def _media_type(response: httpx.Response) -> str:
    return str(response.headers.get("content-type", "")).split(";", 1)[0].strip().lower()


async def _read(response: httpx.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise SafeFetchError("too_large")
    body = bytearray()
    async for chunk in response.aiter_raw():  # raw bytes: the cap is on what came over the wire
        body += chunk
        if len(body) > max_bytes:
            raise SafeFetchError("too_large")
    return bytes(body)


async def _get(
    url: str, parts: SplitResult, *, max_bytes: int, timeout_s: float, transport: httpx.AsyncBaseTransport | None
) -> tuple[bytes | None, str | None]:
    """One request to the checked IP: ``(body, None)``, or ``(None, location)`` for a redirect."""

    ip = await _validated_ip(parts.hostname or "")
    headers = {"Host": parts.netloc, "Accept": "*/*", "Accept-Encoding": "identity", "User-Agent": "specflow"}
    # a client per hop: no cookie or connection outlives the host it was made for
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=timeout_s, transport=transport, verify=True
    ) as client:
        request = client.build_request(
            "GET", _target(parts, ip), headers=headers, extensions={"sni_hostname": parts.hostname}
        )
        response = await client.send(request, stream=True)
        try:
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise SafeFetchError("bad_status")
                return None, urljoin(url, location)
            if not response.is_success:
                raise SafeFetchError("bad_status")
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                raise SafeFetchError("bad_encoding")
            media = _media_type(response)
            if media and media not in ALLOWED_TYPES:
                raise SafeFetchError("bad_type")
            return await _read(response, max_bytes), None
        finally:
            await response.aclose()


async def _fetch(url: str, *, max_bytes: int, timeout_s: float, transport: httpx.AsyncBaseTransport | None) -> bytes:
    for _hop in range(MAX_REDIRECTS + 1):
        body, location = await _get(url, _checked(url), max_bytes=max_bytes, timeout_s=timeout_s, transport=transport)
        if body is not None:
            return body
        url = location or ""
    raise SafeFetchError("bad_status")


async def fetch_public(
    url: str, *, max_bytes: int, timeout_s: float, transport: httpx.AsyncBaseTransport | None = None
) -> bytes:
    """The body of ``url`` (at most ``max_bytes``) within ``timeout_s`` seconds, or ``SafeFetchError``.

    ``transport`` is a seam for tests (the connection target is still the validated IP)."""

    try:
        async with asyncio.timeout(timeout_s), _gate():
            return await _fetch(url, max_bytes=max_bytes, timeout_s=timeout_s, transport=transport)
    except SafeFetchError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise SafeFetchError("timeout") from None
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ssl.SSLError, ValueError):
        raise SafeFetchError("unreachable") from None
