"""Confluence links (``jira.confluence_links``): the page or space a pasted link points to on the configured site, the
refused ones with their closed code, and the pages and Jira issues a page body links to. Pure functions."""

from __future__ import annotations

import pytest

from jira.confluence_links import (
    LinkCode,
    LinkError,
    PageTarget,
    SpaceTarget,
    TitledPage,
    parse_link,
    references,
)

HOST = "example.atlassian.net"
BASE = f"https://{HOST}/wiki"


@pytest.mark.parametrize(
    ("link", "target"),
    [
        (f"{BASE}/spaces/ORD/pages/123456/Order+limits", PageTarget("123456")),
        (f"  {BASE}/spaces/ORD/pages/123456  ", PageTarget("123456")),
        (f"{BASE}/spaces/ORD/pages/123456/Order%20limits?focusedCommentId=9#top", PageTarget("123456")),
        (f"{BASE}/pages/viewpage.action?pageId=777", PageTarget("777")),
        ("https://EXAMPLE.atlassian.net/wiki/spaces/ORD/pages/5/T", PageTarget("5")),
        (f"{BASE}/spaces/ORD/overview", SpaceTarget("ORD")),
        (f"{BASE}/spaces/ORD", SpaceTarget("ORD")),
        (f"{BASE}/spaces/ORD/pages", SpaceTarget("ORD")),
        (f"{BASE}/spaces/~712020ab/overview", SpaceTarget("~712020ab")),
    ],
)
def test_a_page_or_space_link_of_the_site_is_understood(link: str, target: PageTarget | SpaceTarget) -> None:
    assert parse_link(link, HOST) == target


@pytest.mark.parametrize(
    ("link", "code"),
    [
        (f"{BASE}/x/AbCdEf", LinkCode.SHORT),
        ("https://other.atlassian.net/wiki/spaces/ORD/pages/5/T", LinkCode.SITE_MISMATCH),
        ("not a link", LinkCode.INVALID),
        (f"http://{HOST}/wiki/spaces/ORD/pages/5/T", LinkCode.INVALID),
        (f"{BASE}/display/ORD/Old+style", LinkCode.INVALID),
        (f"{BASE}/pages/viewpage.action?pageId=abc", LinkCode.INVALID),
        (f"{BASE}/pages/viewpage.action", LinkCode.INVALID),
        (f"{BASE}/spaces/ORD/pages/notanumber/T", LinkCode.INVALID),
        (f"https://{HOST}/browse/PAY-1", LinkCode.INVALID),
        (f"{BASE}/spaces/bad key/overview", LinkCode.INVALID),
    ],
)
def test_any_other_link_is_refused_with_its_closed_code(link: str, code: LinkCode) -> None:
    with pytest.raises(LinkError) as caught:
        parse_link(link, HOST)

    assert caught.value.code is code and str(caught.value) == code.value


def test_a_short_link_is_refused_even_when_its_id_would_decode() -> None:
    """No tiny-id algorithm is documented or provable offline, and the v1 call that resolves one needs a scope the site
    admin did not grant (AC17): ``confluence_short_link_unsupported``, never a guessed page id."""

    with pytest.raises(LinkError) as caught:
        parse_link(f"{BASE}/x/AQBr", HOST)

    assert caught.value.code is LinkCode.SHORT


BODY = f"""
<p><a href="{BASE}/spaces/ORD/pages/42/Refunds">Refunds</a> and again
<a href="{BASE}/spaces/ORD/pages/42/Refunds">Refunds</a>, <a href="{BASE}/pages/viewpage.action?pageId=43">old</a>,
<a href="https://other.atlassian.net/wiki/spaces/X/pages/9/Y">other site</a>, <a href="https://example.org/">web</a>,
<a href="https://{HOST}/browse/PAY-7">ticket</a>, <a href="https://{HOST}/browse/PAY-7">ticket again</a>,
<a href="https://{HOST}/wiki/spaces/ORD/overview">space</a>, <a>nothing</a></p>
<ac:link><ri:page ri:content-title="Refund policy" ri:space-key="ORD" /></ac:link>
<ac:link><ri:page ri:content-title="Refund policy" ri:space-key="ORD" /></ac:link>
<ac:link><ri:page ri:content-title="Same space page" /></ac:link>
<ac:link><ri:page /></ac:link>
<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PAY-8</ac:parameter>
<ac:parameter ac:name="server">x</ac:parameter></ac:structured-macro>
<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">not a key</ac:parameter></ac:structured-macro>
<ac:structured-macro ac:name="jira"><ac:parameter ac:name="columns">key</ac:parameter></ac:structured-macro>
<ac:structured-macro ac:name="info"><ac:rich-text-body>text</ac:rich-text-body></ac:structured-macro>
"""


def test_the_references_of_a_page_are_its_same_site_pages_titles_and_issues_once_each() -> None:
    found = references(BODY, HOST)

    assert found.page_ids == ["42", "43"]
    assert found.titled == [TitledPage("Refund policy", "ORD"), TitledPage("Same space page", None)]
    assert found.issue_keys == ["PAY-7", "PAY-8"]


def test_a_body_without_links_has_no_references() -> None:
    found = references("<p>plain</p>", HOST)

    assert (found.page_ids, found.titled, found.issue_keys) == ([], [], [])
