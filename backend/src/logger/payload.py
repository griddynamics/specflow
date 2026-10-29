"""Bounded, hashed payload summaries for structured log records.

The payload policy bounds every payload. A payload can be arbitrary, untrusted content (agent tool I/O,
LLM output) of unbounded size, so `summarise_payload` never raises and
never blows up emit latency or memory: only a short preview, the size and a
hash are ever kept -- never the body itself. Full prompts and outputs live
only in Langfuse.

The returned `{prefix}_sha256` key also arms `logger.formatters`' K_sha256
rule: should a record ever carry a raw `{prefix}` field next to it, the
formatter hides that field.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

PREVIEW_CHARS: int = 200


def summarise_payload(value: Any, *, prefix: str = "payload") -> dict[str, Any]:
    """Summarise `value` for structured logging.

    `value is None` returns an empty dict (no keys at all), so callers can
    unconditionally splat the result into `extra=`/a fields mapping
    without adding noise for an absent payload.

    Otherwise returns exactly these keys (never `{prefix}` itself):
    - `{prefix}_preview` -- the first `PREVIEW_CHARS` characters
    - `{prefix}_size` -- the UTF-8 byte length of the *full* stringified
      value
    - `{prefix}_sha256` -- the hex sha256 digest of those same full bytes
    - `{prefix}_truncated` -- whether the full string was longer than the
      preview

    Never raises: any internal failure (an unstringifiable value, an
    encoding edge case) falls back to a safe minimal summary rather than
    propagating into application code.
    """

    if value is None:
        return {}

    try:
        text = _stringify(value)
        encoded = _safe_encode(text)
        return {
            f"{prefix}_preview": text[:PREVIEW_CHARS],
            f"{prefix}_size": len(encoded),
            f"{prefix}_sha256": hashlib.sha256(encoded).hexdigest(),
            f"{prefix}_truncated": len(text) > PREVIEW_CHARS,
        }
    except Exception:
        return _fallback_summary(prefix)


def _stringify(value: Any) -> str:
    """Turn `value` into text, never raising.

    A `str` is used as-is. Anything else is JSON-encoded (`default=str`
    covers non-JSON-native types by stringifying them); if that somehow
    still raises, fall back to `repr`, and if even `repr` raises, to a
    fixed placeholder naming the type.
    """

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return f"<unrepresentable {type(value).__qualname__}>"


def _safe_encode(text: str) -> bytes:
    """UTF-8 bytes of `text`; a lone surrogate (the only way a `str` fails to
    encode as UTF-8) is replaced instead of raising."""

    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="replace")


def _fallback_summary(prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}_preview": "",
        f"{prefix}_size": 0,
        f"{prefix}_sha256": hashlib.sha256(b"").hexdigest(),
        f"{prefix}_truncated": False,
    }
