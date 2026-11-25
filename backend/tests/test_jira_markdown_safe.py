"""``jira.markdown_safe`` and the two converters that use it: only http, https and mailto links are written, with
spaces and parentheses encoded and ``]`` escaped; a code fence is longer than any backtick run in the code; a
non-numeric heading level or list start falls back to a default; a document nested too deep is one closed error, not a
crash. Pure functions."""

from __future__ import annotations

import pytest

from jira.adf_reader import adf_to_markdown
from jira.markdown_safe import MarkupTooDeepError, fence, link, safe_href
from jira.storage_markdown import to_markdown


@pytest.mark.parametrize(
    ("href", "safe"),
    [
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("http://example.com/wiki/Foo_(bar)", "http://example.com/wiki/Foo_%28bar%29"),
        ("  mailto:ada@example.com ", "mailto:ada@example.com"),
        ("HTTPS://example.com", "HTTPS://example.com"),
        ("https://example.com/a\nb", "https://example.com/a%0Ab"),
        ("javascript:alert(1)", None),
        ("JavaScript:alert(1)", None),
        ("data:text/html;base64,AAAA", None),
        ("file:///etc/passwd", None),
        ("vbscript:x", None),
        ("/relative/path", None),
        ("", None),
        ("http://[::1", None),  # not parseable
    ],
)
def test_only_web_and_mail_addresses_are_kept_and_they_are_encoded(href: str, safe: str | None) -> None:
    assert safe_href(href) == safe


def test_link_text_is_escaped_and_an_unsafe_address_leaves_the_text_alone() -> None:
    assert link("a]b[c", "https://example.com") == "[a\\]b\\[c](https://example.com)"
    assert link("click", "javascript:alert(1)") == "click"
    assert link("", "https://example.com") == ""


@pytest.mark.parametrize(
    ("code", "ticks"),
    [("plain", "```"), ("has ``` inside", "````"), ("a `````` six", "```````"), ("one ` tick", "```")],
)
def test_a_fence_is_longer_than_the_longest_backtick_run_in_the_code(code: str, ticks: str) -> None:
    block = fence(code, "py thon`\n")

    assert block == f"{ticks}python\n{code}\n{ticks}"


def paragraph_with_link(href: str, text: str = "doc") -> dict:
    mark = {"type": "link", "attrs": {"href": href}}
    return {"type": "paragraph", "content": [{"type": "text", "text": text, "marks": [mark]}]}


def adf(*blocks: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def test_adf_links_keep_only_safe_schemes_and_encode_the_address() -> None:
    assert (
        adf_to_markdown(adf(paragraph_with_link("https://example.com/a (b)")))
        == "[doc](https://example.com/a%20%28b%29)"
    )
    assert adf_to_markdown(adf(paragraph_with_link("javascript:alert(1)"))) == "doc"
    assert adf_to_markdown(adf(paragraph_with_link("https://example.com", "a]b"))) == "[a\\]b](https://example.com)"
    card = {"type": "blockCard", "attrs": {"url": "javascript:alert(1)"}}
    assert adf_to_markdown(adf(card)) == "javascript:alert(1)"  # the text, never a link
    ok_card = {"type": "blockCard", "attrs": {"url": "https://example.com/x y"}}
    assert adf_to_markdown(adf(ok_card)) == "[https://example.com/x y](https://example.com/x%20y)"


def test_an_adf_code_block_cannot_be_closed_from_inside() -> None:
    block = {"type": "codeBlock", "attrs": {"language": "py```x"}, "content": [{"type": "text", "text": "a\n```\nb"}]}

    assert adf_to_markdown(adf(block)) == "````pyx\na\n```\nb\n````"


@pytest.mark.parametrize("value", ["abc", None, "", {}, [], "1e3", "NaN"])
def test_a_non_numeric_heading_level_or_list_start_falls_back_to_a_default(value: object) -> None:
    heading = {"type": "heading", "attrs": {"level": value}, "content": [{"type": "text", "text": "T"}]}
    item = {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]}
    ordered = {"type": "orderedList", "attrs": {"order": value}, "content": [item]}

    assert adf_to_markdown(adf(heading, ordered)) == "# T\n\n1. x"


def test_a_numeric_string_level_and_start_are_still_read() -> None:
    heading = {"type": "heading", "attrs": {"level": "3"}, "content": [{"type": "text", "text": "T"}]}
    item = {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]}
    ordered = {"type": "orderedList", "attrs": {"order": "4"}, "content": [item]}

    assert adf_to_markdown(adf(heading, ordered)) == "### T\n\n4. x"


def test_a_document_nested_too_deep_is_one_closed_error() -> None:
    node: dict = {"type": "paragraph", "content": [{"type": "text", "text": "deep"}]}
    for _ in range(400):
        node = {"type": "blockquote", "content": [node]}

    with pytest.raises(MarkupTooDeepError):
        adf_to_markdown(adf(node))


def test_storage_links_keep_only_safe_schemes_and_encode_the_address() -> None:
    assert to_markdown('<p><a href="https://example.com/a (b)">doc</a></p>') == "[doc](https://example.com/a%20%28b%29)"
    assert to_markdown('<p><a href="javascript:alert(1)">doc</a></p>') == "doc"
    assert to_markdown('<p><a href="https://example.com">a]b</a></p>') == "[a\\]b](https://example.com)"
    macro = (
        '<p><ac:link><ri:url ri:value="data:text/html,x" /><ac:plain-text-link-body><![CDATA[docs]]>'
        "</ac:plain-text-link-body></ac:link></p>"
    )
    assert to_markdown(macro) == "docs"
    ok = '<p><ac:link><ri:url ri:value="https://example.com/x y" /></ac:link></p>'
    assert to_markdown(ok) == "[https://example.com/x y](https://example.com/x%20y)"


def test_a_storage_code_macro_and_pre_cannot_be_closed_from_inside() -> None:
    macro = (
        '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[a\n```\nb]]></ac:plain-text-body>'
        "</ac:structured-macro>"
    )

    assert to_markdown(macro) == "````\na\n```\nb\n````"
    assert to_markdown("<pre>x ``` y</pre>") == "````\nx ``` y\n````"


def test_a_page_nested_too_deep_is_one_closed_error() -> None:
    with pytest.raises(MarkupTooDeepError):
        to_markdown("<blockquote>" * 3000 + "deep" + "</blockquote>" * 3000)
