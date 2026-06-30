"""Tests for brief chat-facing MCP tool messages."""

import json

from services.user_response import brief_sentences, chat_json, chat_payload


def test_brief_sentences_limits_to_two():
    text = "First sentence. Second sentence. Third sentence."
    assert brief_sentences(text) == "First sentence. Second sentence."


def test_brief_sentences_empty_returns_empty():
    assert brief_sentences("") == ""
    assert brief_sentences("   \n\t ") == ""


def test_brief_sentences_collapses_whitespace_single_sentence():
    assert brief_sentences("One   long\nline with no terminator") == (
        "One long line with no terminator"
    )


def test_chat_json_serializes_payload():
    raw = chat_json("Done.", details={"k": "v"}, generation_id="est-1")
    parsed = json.loads(raw)
    assert parsed["message"] == "Done."
    assert parsed["details"] == {"k": "v"}
    assert parsed["generation_id"] == "est-1"


def test_chat_payload_omits_details_when_none():
    payload = chat_payload("Hi.")
    assert "details" not in payload
    assert payload["message"] == "Hi."


def test_chat_payload_separates_message_and_details():
    payload = chat_payload(
        "Short user line.",
        details={"status": "running", "checkpoint": "kb_init_done"},
        generation_id="est-abc",
    )
    assert payload["message"] == "Short user line."
    assert payload["details"]["status"] == "running"
    assert payload["generation_id"] == "est-abc"
