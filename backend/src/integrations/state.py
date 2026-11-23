"""The signed OAuth ``state`` (docs/jira-sync/oauth.md "Connect flow", docs/github/oauth.md "Connect flow").

``state`` = ``nonce.payload.signature``: the payload holds the user id, ``return_to``, the expiry and, when given, a
``purpose``; the signature is an HMAC-SHA256 of ``nonce.payload`` with a key derived from the token key and a context
of the integration (no table). Never logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from typing import Any, Final, override

from pydantic import SecretStr

STATE_TTL_SECONDS: Final = 600
_NONCE_BYTES: Final = 16
_STATE_PARTS: Final = 3

Clock = Callable[[], float]


class StateInvalidError(Exception):
    """The state is forged, expired, of another user or of another purpose."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class StateSigner:
    """Signs and checks ``state`` for one integration (``context``) with a key derived from its token key."""

    def __init__(self, token_key: SecretStr, context: bytes, *, clock: Clock = time.time) -> None:
        self._key = hmac.new(token_key.get_secret_value().encode("ascii"), context, hashlib.sha256).digest()
        self._clock = clock

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def _signature(self, signed: str) -> str:
        return _b64(hmac.new(self._key, signed.encode("ascii"), hashlib.sha256).digest())

    def sign(self, user_id: str, return_to: str, purpose: str = "") -> str:
        content: dict[str, Any] = {"u": user_id, "r": return_to, "e": int(self._clock()) + STATE_TTL_SECONDS}
        if purpose:
            content["p"] = purpose
        signed = f"{_b64(secrets.token_bytes(_NONCE_BYTES))}.{_b64(json.dumps(content).encode())}"
        return f"{signed}.{self._signature(signed)}"

    def verify(self, state: str, user_id: str, purpose: str = "") -> str:
        """The ``return_to`` of an unexpired state this signer made for ``user_id`` and ``purpose``; else
        ``StateInvalidError``."""

        parts = state.split(".")
        if len(parts) != _STATE_PARTS or not state.isascii():
            raise StateInvalidError
        signed = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(self._signature(signed), parts[2]):
            raise StateInvalidError
        try:
            payload = json.loads(_unb64(parts[1]))
        except (ValueError, binascii.Error):
            raise StateInvalidError from None
        if not isinstance(payload, dict) or payload.get("u") != user_id or not isinstance(payload.get("r"), str):
            raise StateInvalidError
        if payload.get("p", "") != purpose:
            raise StateInvalidError
        expiry = payload.get("e")
        if not isinstance(expiry, int) or expiry < self._clock():
            raise StateInvalidError
        return str(payload["r"])
