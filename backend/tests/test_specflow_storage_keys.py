"""Key rules (AC-4.4) and the document key builder (C10)."""

from __future__ import annotations

import pytest

from storage.base import InvalidObjectKeyError
from storage.keys import document_original, validate_key


def test_document_original_matches_the_c10_layout():
    assert document_original("p1", "d1") == "projects/p1/documents/d1/original.pdf"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/abs/key",
        "a/../b",
        "..",
        "../x",
        "a/./b",
        "a//b",
        "a/b/",
        "a\\b",
        "a/b\x00c",
        "a/\nb",
        "a" * 1025,
        "é" * 513,
        "a/\ud800b",
    ],
)
def test_unsafe_keys_are_rejected(key):
    with pytest.raises(InvalidObjectKeyError):
        validate_key(key)


@pytest.mark.parametrize("key", ["a", "projects/p/documents/d/original.pdf", "a.b/c-d_e"])
def test_safe_keys_pass_unchanged(key):
    assert validate_key(key) == key


@pytest.mark.parametrize("bad", ["", "a/b", "../x"])
def test_builder_rejects_multi_segment_or_empty_ids(bad):
    with pytest.raises(InvalidObjectKeyError):
        document_original(bad, "d")
    with pytest.raises(InvalidObjectKeyError):
        document_original("p", bad)


def test_key_at_the_1024_byte_limit_is_accepted():
    assert validate_key("a" * 1024) == "a" * 1024
    assert validate_key("é" * 512) == "é" * 512
