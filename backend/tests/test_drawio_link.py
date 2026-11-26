"""The diagrams.net link parser (``api.imports.drawio_link``): every accepted form, every refusal, the lookalike hosts
and the link that is stored.

Sequence::

    pasted text -> parse_link -> payload (#R) | xml (#P with data) | url (#U, ?url=, bare .drawio) + the link to store
    anything else -> DrawioLinkError(invalid | unsupported(storage) | too_large)
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from api.imports.drawio_link import DIAGRAM_HOSTS, DrawioLinkError, parse_link
from file_parsers.diagrams import MAX_LINK_BYTES

FILE = "https://example.com/files/flow.drawio"


def refused(text: str) -> DrawioLinkError:
    with pytest.raises(DrawioLinkError) as caught:
        parse_link(text)
    return caught.value


@pytest.mark.parametrize("host", sorted(DIAGRAM_HOSTS))
def test_a_payload_link_on_every_diagrams_host_keeps_no_fragment(host) -> None:
    found = parse_link(f"  https://{host}/?lightbox=1#Rjzb%2Fabc  ")

    assert (found.kind, found.value) == ("payload", "jzb%2Fabc")
    assert found.stored == f"https://{host}/?lightbox=1" and "#" not in found.stored


def test_a_url_hash_is_decoded_and_stored_as_scheme_host_path_only() -> None:
    link = f"https://app.diagrams.net/#U{quote(FILE + '?token=s3cret#frag', safe='')}"

    found = parse_link(link)

    assert (found.kind, found.value, found.stored) == ("url", FILE + "?token=s3cret#frag", FILE)


def test_a_raw_url_after_the_u_is_read_too() -> None:
    assert parse_link(f"https://viewer.diagrams.net/#U{FILE}").value == FILE


def test_the_legacy_url_query_is_a_fetch() -> None:
    link = f"https://viewer.diagrams.net/?highlight=0000ff&url={quote(FILE + '?sig=abc', safe='')}"

    found = parse_link(link)

    assert (found.kind, found.value, found.stored) == ("url", FILE + "?sig=abc", FILE)


def test_a_page_hash_with_data_is_inline_and_stored_without_its_fragment() -> None:
    xml = '<mxfile><diagram id="a" name="P">x</diagram></mxfile>'
    link = f"https://app.diagrams.net/?x=1#P{quote(json.dumps({'data': xml, 'id': 'a'}))}"

    found = parse_link(link)

    assert (found.kind, found.value, found.stored) == ("xml", xml, "https://app.diagrams.net/?x=1")


@pytest.mark.parametrize("payload", ["{}", '{"data": ""}', '{"data": 3}', "[1]", "not json", '{"url": "https://a.b"}'])
def test_a_page_hash_without_data_is_invalid(payload) -> None:
    assert refused(f"https://app.diagrams.net/#P{payload}").code == "invalid"


@pytest.mark.parametrize("link", [FILE, "https://app.diagrams.net/files/a.drawio", "https://example.com/A.DRAWIO"])
def test_a_bare_drawio_url_is_a_fetch_of_itself(link) -> None:
    found = parse_link(link + "?key=s3cret")

    assert (found.kind, found.value, found.stored) == ("url", link + "?key=s3cret", link)


@pytest.mark.parametrize(
    ("hash_", "storage"),
    [
        ("G1abcDEF", "Google Drive"),
        ("Howner/repo/main/a.drawio", "GitHub"),
        ("Dfoo", "Dropbox"),
        ("Wbar", "OneDrive"),
        ("Aname", "GitLab"),
        ("Lname.drawio", "browser storage"),
        ("Tcard", "Trello"),
        ("Zunknown", None),
    ],
)
def test_a_storage_hash_is_unsupported_and_names_the_storage(hash_, storage) -> None:
    error = refused(f"https://app.diagrams.net/#{hash_}")

    assert (error.code, error.storage) == ("unsupported", storage)


@pytest.mark.parametrize(
    "link",
    [
        "",
        "   ",
        "flow",
        "http://app.diagrams.net/#Rabc",
        "ftp://app.diagrams.net/#Rabc",
        "//app.diagrams.net/#Rabc",
        "https://user@app.diagrams.net/#Rabc",
        "https://user:pw@app.diagrams.net/#Rabc",
        "https://user:pw@example.com/a.drawio",
        "http://example.com/a.drawio",
        "https://app.diagrams.net/",
        "https://app.diagrams.net/#",
        "https://app.diagrams.net/#R",
        "https://app.diagrams.net/#U",
        "https://app.diagrams.net:8443/#Rabc",
        "https://example.com/a.drawio#frag",
        "https://example.com/page.html",
        "https://app.diagrams.net/#U" + quote("http://example.com/a.drawio", safe=""),
        "https://app.diagrams.net/#U" + quote("https://u:p@example.com/a.drawio", safe=""),
        "https://app.diagrams.net/#U" + quote("file:///etc/passwd", safe=""),
        "https://app.diagrams.net/?url=" + quote("http://example.com/a.drawio", safe=""),
        "https://app.diagrams.net/?url=",
        "https://[::1/#Rabc",
    ],
)
def test_a_link_that_is_not_plain_https_without_credentials_or_not_a_diagram_is_invalid(link) -> None:
    assert refused(link).code == "invalid"


@pytest.mark.parametrize(
    "host",
    [
        "app.diagrams.net.evil.com",
        "evil-app.diagrams.net.evil.com",
        "xapp.diagrams.net",
        "diagrams.net",
        "draw.io",
        "app.diagrams.net@evil.com",
        "evil.com#app.diagrams.net",
        "evil.com?x=app.diagrams.net",
    ],
)
def test_a_lookalike_host_is_not_a_diagrams_host(host) -> None:
    assert refused(f"https://{host}/#Rabc").code == "invalid"
    assert refused(f"https://{host}/?url={quote(FILE, safe='')}").code == "invalid"


def test_the_host_is_matched_case_insensitively() -> None:
    assert parse_link("https://APP.Diagrams.NET/#Rabc").kind == "payload"


def test_a_link_over_the_limit_is_too_large_before_anything_else() -> None:
    assert refused("https://app.diagrams.net/#R" + "a" * MAX_LINK_BYTES).code == "too_large"
    assert refused("x" * (MAX_LINK_BYTES + 1)).code == "too_large"


def test_a_link_at_the_limit_is_read() -> None:
    link = "https://app.diagrams.net/#R" + "a" * (MAX_LINK_BYTES - len("https://app.diagrams.net/#R"))

    assert parse_link(link).kind == "payload"


def test_no_error_carries_the_link() -> None:
    canary = "canary-7f3a"
    for link in (f"https://evil.com/{canary}", f"https://app.diagrams.net/#G{canary}", f"http://x/{canary}"):
        error = refused(link)
        assert canary not in str(error) and canary not in repr(error.args)


def test_a_deeply_nested_page_hash_is_invalid_not_a_crash() -> None:
    nested = "[" * 100_000

    assert refused(f"https://app.diagrams.net/#P{nested}").code == "invalid"


@pytest.mark.parametrize(
    ("link", "name"),
    [
        ("https://app.diagrams.net/?title=Order%20flow.drawio#Rabc", "Order flow"),
        ("https://app.diagrams.net/?title=Map.drawio.svg#Rabc", "Map"),
        ("https://app.diagrams.net/#Rabc", ""),
        ("https://app.diagrams.net/#U" + quote(FILE, safe=""), "flow"),
        ("https://viewer.diagrams.net/?title=Mine.xml&url=" + quote(FILE, safe=""), "Mine"),
        ("https://example.com/files/My%20Plan.drawio", "My Plan"),
    ],
)
def test_the_diagram_name_is_the_title_parameter_else_the_fetched_file_name(link, name) -> None:
    assert parse_link(link).name == name
