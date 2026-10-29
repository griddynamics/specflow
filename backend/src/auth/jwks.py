"""JWKS signing keys for the OIDC verifier: cached, throttled, never blocking.

``JwksCache`` wraps ``PyJWKClient(jwks_url, lifespan=600, timeout=5)`` (its
HTTP fetch replaced by the hardened ``fetch.fetch_json``: size cap, overall
deadline, no redirects) and keeps its own parsed ``kid -> key`` map:

* a known ``kid`` is served from memory for ``lifespan`` seconds (no fetch);
* an unknown ``kid`` is negative-cached for ``negative_ttl`` (60 s) after a
  refetch that still lacks it: at most one refetch per ``kid`` per 60 s;
* globally, fetches are at least ``MIN_REFETCH_INTERVAL_SECONDS`` apart; after
  a failed fetch the gap doubles from ``FAILURE_BACKOFF_SECONDS`` up to
  ``MAX_FAILURE_BACKOFF_SECONDS`` (reset on success). A token that would need
  a fetch inside that window fails with 401 at once, without waiting;
* fetches are single-flight (waiters reuse the in-flight result) and run on a
  small dedicated executor, bounded by ``timeout`` plus an ``asyncio.wait_for``
  margin, so a hanging endpoint fails with a 401 within about ``timeout``
  seconds and never blocks the event loop.

Without an explicit JWKS URL the ``jwks_uri`` is read once from the issuer's
OIDC discovery document and must be on the issuer's host.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final, override
from urllib.parse import urlsplit

from jwt import PyJWK, PyJWKClient, PyJWKSet, PyJWTError

from auth.errors import invalid_token
from auth.fetch import FetchError, fetch_json
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("auth.jwks")

LIFESPAN_SECONDS: Final = 600
TIMEOUT_SECONDS: Final = 5.0
NEGATIVE_TTL_SECONDS: Final = 60.0
MIN_REFETCH_INTERVAL_SECONDS: Final = 10.0
FAILURE_BACKOFF_SECONDS: Final = 5.0
MAX_FAILURE_BACKOFF_SECONDS: Final = 60.0
MAX_NEGATIVE_KIDS: Final = 1024
DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
_WAIT_MARGIN_SECONDS: Final = 1.0

MAX_FETCH_WORKERS: Final = 2
# Any of these means "JWKS unavailable": transport, deadline, document shape or key material.
_FETCH_FAILURES: Final = (FetchError, PyJWTError, OSError, ValueError, TypeError, KeyError)

# Shared by every cache in the process; fetches end by their own deadline.
_EXECUTOR: Final = ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS, thread_name_prefix="specflow-jwks")


class _BoundedJwkClient(PyJWKClient):
    """``PyJWKClient`` whose HTTP fetch is the hardened ``fetch_json``."""

    @override
    def fetch_data(self) -> Any:
        data = fetch_json(self.uri, timeout=self.timeout)
        if self.jwk_set_cache is not None and isinstance(data, dict):
            self.jwk_set_cache.put(PyJWKSet.from_dict(data))
        return data


class JwksConfigError(ValueError):
    """A ``JwksCache`` was built without a JWKS URL and without an issuer."""


class JwksCache:
    """Signing keys by ``kid``; ``signing_key`` raises a 401 ``AuthError``."""

    def __init__(
        self,
        jwks_url: str | None = None,
        *,
        issuer: str | None = None,
        lifespan: float = LIFESPAN_SECONDS,
        timeout: float = TIMEOUT_SECONDS,
        negative_ttl: float = NEGATIVE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not jwks_url and not issuer:
            raise JwksConfigError("JwksCache needs a JWKS URL or an issuer for discovery")
        self._issuer = issuer
        self._lifespan = lifespan
        self._timeout = timeout
        self._negative_ttl = negative_ttl
        self._clock = clock
        self._client = self._new_client(jwks_url) if jwks_url else None
        self._keys: dict[str, PyJWK] = {}
        self._expires_at = -math.inf
        self._negative: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._generation = 0
        self._last_fetch_ok = False
        self._next_fetch_at = -math.inf
        self._backoff = 0.0

    async def signing_key(self, kid: str) -> PyJWK:
        """The key for ``kid``, fetching only when the cache cannot answer."""

        if self._is_negative(kid):
            raise invalid_token("unknown_kid")
        key = self._cached(kid)
        if key is not None:
            return key
        if not self._lock.locked():
            self._raise_if_throttled()  # fail fast: no waiting, no fetch
        seen = self._generation
        async with self._lock:
            if self._generation == seen:
                self._raise_if_throttled()
                await self._refresh(self._refresh_reason())
            elif not self._last_fetch_ok:
                raise invalid_token("jwks_unavailable")
            key = self._cached(kid)
            if key is None:
                self._remember_unknown(kid)
                raise invalid_token("unknown_kid")
            return key

    def _raise_if_throttled(self) -> None:
        if self._clock() >= self._next_fetch_at:
            return
        if not self._last_fetch_ok:
            raise invalid_token("jwks_unavailable")
        if self._is_fresh():
            raise invalid_token("refetch_throttled")  # not negative-cached
        raise invalid_token("jwks_unavailable")

    def _cached(self, kid: str) -> PyJWK | None:
        return self._keys.get(kid) if self._is_fresh() else None

    def _is_fresh(self) -> bool:
        return self._clock() < self._expires_at

    def _refresh_reason(self) -> str:
        if self._is_fresh():
            return "unknown_kid"
        return "expired" if self._keys else "initial"

    def _is_negative(self, kid: str) -> bool:
        until = self._negative.get(kid)
        if until is None:
            return False
        if self._clock() < until:
            return True
        del self._negative[kid]
        return False

    def _remember_unknown(self, kid: str) -> None:
        now = self._clock()
        if len(self._negative) >= MAX_NEGATIVE_KIDS:
            for stale in [k for k, until in self._negative.items() if until <= now]:
                del self._negative[stale]
            while len(self._negative) >= MAX_NEGATIVE_KIDS:
                del self._negative[next(iter(self._negative))]
        self._negative[kid] = now + self._negative_ttl

    async def _refresh(self, reason: str) -> None:
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        try:
            keys = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, self._fetch_keys),
                timeout=self._fetch_budget(),
            )
        except _FETCH_FAILURES as exc:  # any fetch failure is "JWKS unavailable"
            self._fetch_failed(reason, _failure_code(exc), started)
            raise invalid_token("jwks_unavailable") from None
        self._generation += 1
        self._keys = keys
        self._expires_at = self._clock() + self._lifespan
        self._last_fetch_ok = True
        self._backoff = 0.0
        self._next_fetch_at = self._clock() + MIN_REFETCH_INTERVAL_SECONDS
        log_event(
            logger,
            INFO,
            "auth.jwks.refetch",
            reason=reason,
            outcome="ok",
            key_count=len(keys),
            duration_ms=_elapsed_ms(started),
        )

    def _fetch_failed(self, reason: str, failure: str, started: float) -> None:
        self._generation += 1  # after the attempt: waiters reuse its outcome
        self._last_fetch_ok = False
        self._backoff = min(max(self._backoff * 2, FAILURE_BACKOFF_SECONDS), MAX_FAILURE_BACKOFF_SECONDS)
        self._next_fetch_at = self._clock() + self._backoff
        log_event(
            logger,
            WARNING,
            "auth.jwks.refetch",
            reason=reason,
            outcome="failed",
            failure=failure,
            backoff_s=self._backoff,
            duration_ms=_elapsed_ms(started),
        )

    def _fetch_budget(self) -> float:
        hops = 1 if self._client is not None else 2  # discovery adds one request
        return hops * self._timeout + _WAIT_MARGIN_SECONDS

    def _fetch_keys(self) -> dict[str, PyJWK]:
        """Worker-thread body: (discover once, then) fetch and parse the JWKS."""

        if self._client is None:
            self._client = self._new_client(self._discover_jwks_uri())
        jwk_set = self._client.get_jwk_set(refresh=True)
        return {key.key_id: key for key in jwk_set.keys if key.key_id and key.public_key_use in ("sig", None)}

    def _discover_jwks_uri(self) -> str:
        issuer = self._issuer or ""
        document = fetch_json(issuer.rstrip("/") + DISCOVERY_PATH, timeout=self._timeout)
        jwks_uri = document.get("jwks_uri") if isinstance(document, dict) else None
        if not isinstance(jwks_uri, str) or not _same_origin_host(jwks_uri, issuer):
            raise FetchError("discovery_host")
        return jwks_uri

    def _new_client(self, jwks_url: str) -> PyJWKClient:
        return _BoundedJwkClient(jwks_url, lifespan=self._lifespan, timeout=self._timeout)


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, FetchError):
        return exc.failure
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "invalid_jwks"


def _same_origin_host(candidate: str, issuer: str) -> bool:
    """Same hostname; https issuers also require an https JWKS URL."""

    got, want = urlsplit(candidate), urlsplit(issuer)
    if got.scheme not in ("http", "https") or not got.hostname:
        return False
    if want.scheme == "https" and got.scheme != "https":
        return False
    return got.hostname == want.hostname


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
