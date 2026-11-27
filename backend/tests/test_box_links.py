"""``box.links.parse_link``: the links a member may paste (folder, file, shared link on ``app.box.com`` or an enterprise
sub-domain, https only, no credentials) and everything refused as ``box_link_invalid``. Pure: no network."""

from __future__ import annotations

import pytest

from box.codes import BoxCode, BoxError
from box.links import BoxTarget, canonical_link, file_link, parse_link

SHARED = "https://app.box.com/s/sharedlinkcanary9d2"


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("https://app.box.com/folder/123456", BoxTarget("folder", id="123456")),
        ("https://app.box.com/file/987", BoxTarget("file", id="987")),
        ("https://acme.app.box.com/folder/42", BoxTarget("folder", id="42")),
        ("  https://APP.BOX.COM/folder/42/  ", BoxTarget("folder", id="42")),
        ("https://app.box.com/folder/42?s=abc#frag", BoxTarget("folder", id="42")),
        (SHARED, BoxTarget("shared", shared_link=SHARED)),
        (
            "https://acme.app.box.com/v/design-docs",
            BoxTarget("shared", shared_link="https://acme.app.box.com/v/design-docs"),
        ),
        (f"{SHARED}?utm=1#x", BoxTarget("shared", shared_link=SHARED)),
    ],
)
def test_a_folder_file_or_shared_link_is_parsed(link, target) -> None:
    assert parse_link(link) == target


@pytest.mark.parametrize(
    "link",
    [
        "",
        "   ",
        "nonsense",
        "http://app.box.com/folder/1",  # not https
        "https://user:pw@app.box.com/folder/1",  # credentials
        "https://user@app.box.com/folder/1",
        "https://app.box.com.evil.io/folder/1",  # lookalikes
        "https://evilapp.box.com/folder/1x",
        "https://notapp.box.com/folder/1",
        "https://app.box.com@evil.io/folder/1",
        "https://box.com/folder/1",
        "https://a.b.app.box.com/folder/1",  # one sub-domain label only
        "https://app.box.com:8443/folder/1",
        "https://app.box.com/folder/0",  # All Files
        "https://app.box.com/folder/000",
        "https://app.box.com/folder/abc",
        "https://app.box.com/folder/1/extra",
        "https://app.box.com/folder",
        "https://app.box.com/file/",
        "https://app.box.com/notes/1",
        "https://app.box.com/s/",
        "https://app.box.com/s/a b",
        "https://app.box.com/s/a/b",
        "https://app.box.com/v/",
        "https://app.box.com/",
        "ftp://app.box.com/folder/1",
        "https://app.box.com/folder/1\nhttps://app.box.com/folder/2",
        "https://[::1/folder/1",
    ],
)
def test_anything_else_is_box_link_invalid(link) -> None:
    with pytest.raises(BoxError) as error:
        parse_link(link)

    assert error.value.code is BoxCode.LINK_INVALID and str(error.value) == "box_link_invalid"


def test_a_shared_link_never_shows_in_the_repr_of_its_target() -> None:
    target = parse_link(SHARED)

    assert "sharedlinkcanary" not in repr(target) and "sharedlinkcanary" not in str(target)


def test_the_stored_and_link_back_addresses_are_canonical_ids_never_a_shared_link() -> None:
    assert canonical_link("folder", "9") == "https://app.box.com/folder/9"
    assert file_link("7") == "https://app.box.com/file/7"
