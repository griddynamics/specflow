"""RED tests for `logger.payload` — LOG-37 and AC-7.

Scenario text: the central-logger test catalog
(row LOG-37: "None -> {}; str; dict; >200 chars; prefix"). There is
no full-payload mode: the result never carries `payload`/`payload_capped`
(AC-7).
Module contract:
the payload policy and the bounded-payloads crash-safety rule.

`src/logger/payload.py` does not exist yet — Wave 3 of the central-logger
plan (`w3-0`) builds it. Every import below is therefore expected to fail
with `ModuleNotFoundError`/`ImportError` today; that is the correct RED
state for this Wave 2 track (`w2-a1`). Do not skip or xfail these tests.

Ambiguity note (flagged in the W2.A1 report): the contract does not spell
out exactly how a non-string `value` (e.g. a `dict`) is turned into text
before it is sized/hashed. The dict-value test below therefore only
checks the documented *shape* (keys, types, a 64-char lowercase hex
sha256) rather than an exact byte-for-byte serialisation, so it cannot
fail for a spec-compliant choice of serialisation once Wave 3 lands.
"""

from __future__ import annotations

import hashlib

import pytest

import logger.payload as payload_module
from logger.payload import PREVIEW_CHARS, summarise_payload

_HEX_DIGITS = frozenset("0123456789abcdef")
_SUMMARY_KEYS = frozenset({"payload_preview", "payload_size", "payload_sha256", "payload_truncated"})


def test_summarise_payload_of_none_is_empty() -> None:
    assert summarise_payload(None) == {}


def test_summarise_payload_short_string_is_not_truncated() -> None:
    value = "hello world"

    result = summarise_payload(value)

    assert result["payload_preview"] == value
    assert result["payload_size"] == len(value.encode("utf-8"))
    assert result["payload_sha256"] == hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert result["payload_truncated"] is False
    assert "payload" not in result
    assert "payload_capped" not in result


def test_summarise_payload_long_string_preview_is_capped_at_200_chars() -> None:
    assert PREVIEW_CHARS == 200
    value = "x" * 250

    result = summarise_payload(value)

    assert result["payload_preview"] == value[:200]
    assert len(result["payload_preview"]) == 200
    assert result["payload_size"] == len(value.encode("utf-8"))
    assert result["payload_sha256"] == hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert result["payload_truncated"] is True


def test_summarise_payload_dict_value_produces_a_well_shaped_summary() -> None:
    # Best-effort interpretation (see module docstring note above): the
    # exact text used to size/hash a non-string value isn't specified, so
    # this only pins down the documented shape of the result.
    value = {"tool": "Read", "args": {"path": "/work/x"}, "n": 3}

    result = summarise_payload(value)

    assert isinstance(result["payload_preview"], str)
    assert len(result["payload_preview"]) <= PREVIEW_CHARS
    assert isinstance(result["payload_size"], int)
    assert result["payload_size"] > 0
    assert isinstance(result["payload_sha256"], str)
    assert len(result["payload_sha256"]) == 64
    assert set(result["payload_sha256"]) <= _HEX_DIGITS
    assert isinstance(result["payload_truncated"], bool)
    assert "payload" not in result


def test_summarise_payload_uses_the_given_prefix() -> None:
    value = "some data"

    result = summarise_payload(value, prefix="data")

    assert set(result) == {"data_preview", "data_size", "data_sha256", "data_truncated"}


def test_summarise_payload_never_raises_on_a_value_whose_str_raises() -> None:
    class Unstringifiable:
        def __str__(self) -> str:
            raise RuntimeError("nope")

        def __repr__(self) -> str:
            raise RuntimeError("nope either")

    result = summarise_payload(Unstringifiable())

    assert isinstance(result, dict)
    assert set(result) <= _SUMMARY_KEYS


# ---------------------------------------------------------------------------
# AC-7 — no full-payload mode: the body itself is never returned.
# ---------------------------------------------------------------------------


def test_summarise_payload_rejects_the_removed_full_keyword() -> None:
    with pytest.raises(TypeError):
        summarise_payload("x", full=True)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("RAW-BODY-" + "x" * 500, id="long-str"),
        pytest.param("short", id="short-str"),
        pytest.param({"prompt": "y" * 500, "n": 1}, id="dict"),
        pytest.param(None, id="none"),
    ],
)
def test_summarise_payload_only_ever_returns_summary_keys(value: object) -> None:
    result = summarise_payload(value)

    assert set(result) <= _SUMMARY_KEYS
    if value is not None:
        assert set(result) == _SUMMARY_KEYS


def test_payload_module_has_no_full_cap() -> None:
    assert not hasattr(payload_module, "FULL_CAP_BYTES")
