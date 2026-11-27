"""``dropbox.links.parse_link``: the links a member may paste (shared links ``/scl/fo``, ``/scl/fi``, legacy ``/s`` and
``/sh``, and the ``/home/<path>`` web path, on ``www.dropbox.com`` or ``dropbox.com``, https only, no credentials) and
everything refused as ``dropbox_link_invalid``. Pure: no network."""

from __future__ import annotations

import pytest

from dropbox.codes import DropboxCode, DropboxError
from dropbox.links import DropboxTarget, canonical_link, parse_link

FOLDER = "https://www.dropbox.com/scl/fo/abc123/AAAkey"
FILE = "https://www.dropbox.com/scl/fi/xyz789/Spec.pdf"


@pytest.mark.parametrize(
    ("link", "target"),
    [
        (f"{FOLDER}?rlkey=k1&st=s1&dl=0", DropboxTarget("shared", shared_link=f"{FOLDER}?rlkey=k1")),
        (f"{FILE}?rlkey=k2&dl=1", DropboxTarget("shared", shared_link=f"{FILE}?rlkey=k2")),
        (f"{FOLDER}?rlkey=k1#frag", DropboxTarget("shared", shared_link=f"{FOLDER}?rlkey=k1")),
        (
            "https://dropbox.com/scl/fo/abc123/AAAkey?rlkey=k1",
            DropboxTarget("shared", shared_link=f"{FOLDER}?rlkey=k1"),
        ),
        (
            "  HTTPS://WWW.DROPBOX.COM/scl/fo/abc123/AAAkey?rlkey=k1  ",
            DropboxTarget("shared", shared_link=f"{FOLDER}?rlkey=k1"),
        ),
        (
            "https://www.dropbox.com/s/legacykey1/notes.txt?dl=0",
            DropboxTarget("shared", shared_link="https://www.dropbox.com/s/legacykey1/notes.txt"),
        ),
        (
            "https://www.dropbox.com/sh/legacykey2/AAAid?dl=0",
            DropboxTarget("shared", shared_link="https://www.dropbox.com/sh/legacykey2/AAAid"),
        ),
        (f"{FOLDER}", DropboxTarget("shared", shared_link=FOLDER)),
        ("https://www.dropbox.com/home/Graphico%20Lab", DropboxTarget("path", path="/Graphico Lab")),
        ("https://www.dropbox.com/home/Graphico%20Lab/Sub/", DropboxTarget("path", path="/Graphico Lab/Sub")),
        ("https://dropbox.com/home/a/b?preview=x.pdf", DropboxTarget("path", path="/a/b/x.pdf")),  # the file
        (
            "https://www.dropbox.com/home/Graphico%20Lab?preview=Q3+plan%20%C5%BC.pdf",
            DropboxTarget("path", path="/Graphico Lab/Q3 plan ż.pdf"),
        ),
        ("https://www.dropbox.com/home/a?preview=&dl=0", DropboxTarget("path", path="/a")),  # an empty preview
        ("https://www.dropbox.com/home/Zażółć%20gęślą", DropboxTarget("path", path="/Zażółć gęślą")),
    ],
)
def test_a_shared_link_or_a_home_path_is_parsed(link, target) -> None:
    assert parse_link(link) == target


@pytest.mark.parametrize(
    "link",
    [
        "",
        "   ",
        "nonsense",
        "http://www.dropbox.com/scl/fo/abc/AAA?rlkey=k",  # not https
        "https://user:pw@www.dropbox.com/scl/fo/abc/AAA",  # credentials
        "https://user@www.dropbox.com/home/a",
        "https://www.dropbox.com@evil.io/home/a",
        "https://www.dropbox.com.evil.io/home/a",  # lookalikes
        "https://evildropbox.com/home/a",
        "https://notwww.dropbox.com/home/a",
        "https://dl.dropboxusercontent.com/scl/fo/abc/AAA",
        "https://www.dropbox.com:8443/home/a",
        "https://www.dropbox.com/home",  # the Dropbox root
        "https://www.dropbox.com/home/",
        "https://www.dropbox.com/home//",
        "https://www.dropbox.com/",
        "https://www.dropbox.com/scl/fo",  # incomplete shared links
        "https://www.dropbox.com/scl/fo/abc",
        "https://www.dropbox.com/scl/xx/abc/AAA",
        "https://www.dropbox.com/scl/fo/a b/AAA",
        "https://www.dropbox.com/s",
        "https://www.dropbox.com/sh/",
        "https://www.dropbox.com/s/a%20b/name",
        "https://www.dropbox.com/paper/d/abc",  # legacy Paper
        "https://www.dropbox.com/work/x",
        "ftp://www.dropbox.com/home/a",
        "https://www.dropbox.com/home/a\nhttps://www.dropbox.com/home/b",
        "https://www.dropbox.com/home/Graphico Lab",  # a raw space is not a pasted address
        "https://www.dropbox.com/home/a\u0000b",
        "https://[::1/home/a",
        "https://www.dropbox.com/home/" + "a" * 2000,
        "https://www.dropbox.com/home/a?preview=..",  # path traversal
        "https://www.dropbox.com/home/a?preview=.",
        "https://www.dropbox.com/home/a?preview=../b.pdf",
        "https://www.dropbox.com/home/a?preview=..%2Fb.pdf",
        "https://www.dropbox.com/home/a?preview=sub/b.pdf",
        "https://www.dropbox.com/home/a/../b",
        "https://www.dropbox.com/home/a/%2E%2E/b",
        "https://www.dropbox.com/home/a%2F..%2Fb",
        "https://www.dropbox.com/home?preview=..",
    ],
)
def test_anything_else_is_dropbox_link_invalid(link) -> None:
    with pytest.raises(DropboxError) as error:
        parse_link(link)

    assert error.value.code is DropboxCode.LINK_INVALID and str(error.value) == "dropbox_link_invalid"


def test_a_shared_link_and_a_path_never_show_in_the_repr_of_their_target() -> None:
    shared = parse_link(f"{FOLDER}?rlkey=rlkeysecret1")
    path = parse_link("https://www.dropbox.com/home/Secret%20Folder")

    for target in (shared, path):
        assert "rlkeysecret" not in repr(target) and "AAAkey" not in repr(target) and "Secret" not in repr(target)
        assert "rlkeysecret" not in str(target) and "Secret" not in str(target)


def test_the_stored_address_is_the_home_path_url_encoded_never_a_shared_link() -> None:
    assert canonical_link("/Graphico Lab/Sub") == "https://www.dropbox.com/home/Graphico%20Lab/Sub"
    assert canonical_link("/Zażółć") == "https://www.dropbox.com/home/Za%C5%BC%C3%B3%C5%82%C4%87"
    assert parse_link(canonical_link("/Graphico Lab/Sub")) == DropboxTarget("path", path="/Graphico Lab/Sub")
