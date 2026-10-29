"""DATA-GDUI-0002 accepted folder links."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from drive.links import (
    ID_PATTERN,
    canonical_link,
    is_drive_id,
    parse_folder_link,
)

ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


def test_ac1_folder_link_with_query():
    assert parse_folder_link(f"https://drive.google.com/drive/folders/{ID}?usp=sharing") == ID


def test_ac2_user_number_and_trailing_slash():
    assert parse_folder_link(f"https://drive.google.com/drive/u/1/folders/{ID}/") == ID


def test_ac3_nine_characters_rejected():
    assert parse_folder_link("abcdefghi") is None


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/drive/folders/1AbCdEfGhIjKlMnOp",
        "https://docs.google.com/document/d/1abcdefghijk/edit",
        "http://drive.google.com/drive/folders/" + ID,
        "https://DRIVE.google.com/drive/folders/" + ID,
        "https://drive.google.com/drive/folders/" + ID + "#top",
        "https://drive.google.com/drive/folders/" + ID + "/extra",
        "https://drive.google.com/drive/u/x/folders/" + ID,
        "https://drive.google.com/drive/u/\u0661/folders/" + ID,
        "https://drive.google.com/file/d/" + ID + "/view",
        "https://drive.google.com/open?id=short",
        "https://drive.google.com/open?x=" + ID,
        "https://drive.google.com.evil.example/drive/folders/" + ID,
        "drive.google.com/abc",
        "",
    ],
)
def test_ac4_ac5_other_hosts_paths_and_forms_rejected(link):
    assert parse_folder_link(link) is None


def test_ac6_bare_ten_characters_with_spaces():
    assert parse_folder_link("  abcdefghij") == "abcdefghij"


def test_ac7_open_id_form():
    assert parse_folder_link(f"https://drive.google.com/open?id={ID}") == ID


def test_id_with_quote_never_passes():
    assert parse_folder_link("abc'def' or 'x") is None
    assert not is_drive_id("aaaaaaaaaa' or '1'='1")


def test_canonical_link_only_for_valid_ids():
    assert canonical_link(ID) == f"https://drive.google.com/drive/folders/{ID}"
    with pytest.raises(ValueError, match="not a Drive id"):
        canonical_link("short")


def test_contract_vectors_hold():
    contract = json.loads(files("drive").joinpath("fixtures/contract.json").read_text("utf-8"))
    for vector in contract["link_vectors"]:
        assert parse_folder_link(vector["link"]) == vector["id"], vector["link"]


def test_id_pattern_is_the_shared_one():
    assert ID_PATTERN == r"[A-Za-z0-9_-]{10,}"
