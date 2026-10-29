"""The package rules equal ``examples/gdrive_explore.py`` and ``contract.json``."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

import gdrive_explore as explore
from drive.categories import (
    CATEGORY_ORDER,
    CLASS_ORDER,
    LABELS,
    categorize,
    processing_class,
)
from drive.links import parse_folder_link
from drive.model import DriveFolder, DriveItem, Listing, ordered, summarize

CONTRACT = json.loads(files("drive").joinpath("fixtures/contract.json").read_text("utf-8"))
MIMES = sorted(
    set(explore.CATEGORIES)
    | {
        "image/png",
        "video/mp4",
        "application/zip",
        "text/plain",
        "",
        explore.GOOGLE + "form",
        explore.GOOGLE + "drawing",
        explore.GOOGLE + "site",
        explore.GOOGLE + "vid",
    }
)


@pytest.mark.parametrize(("link", "accepted"), explore.LINK_CASES)
def test_link_cases_match_explore(link, accepted):
    assert (parse_folder_link(link) is not None) is accepted
    assert parse_folder_link(link) == explore.parse_link(link)


@pytest.mark.parametrize("vector", CONTRACT["link_vectors"], ids=lambda v: v["link"][:50])
def test_contract_link_vectors_match_both(vector):
    assert explore.parse_link(vector["link"]) == vector["id"]
    assert parse_folder_link(vector["link"]) == vector["id"]


@pytest.mark.parametrize("mime", MIMES)
def test_categories_match_explore(mime):
    label, kind = explore.category(mime)
    category, klass = categorize(mime)
    assert LABELS[category] == label
    assert klass.value.replace("_", " ") == kind


def test_contract_categories_in_order():
    assert CONTRACT["categories"] == [
        {
            "category": c.value,
            "label": LABELS[c],
            "processing_class": processing_class(c).value,
        }
        for c in CATEGORY_ORDER
    ]
    assert CONTRACT["class_order"] == [k.value for k in CLASS_ORDER]


def test_summary_bullets_match_explore():
    mimes = (
        [explore.GOOGLE + "document"] * 2
        + ["application/pdf"] * 3
        + ["application/msword", "image/png", "image/gif", "video/mp4"]
        + [explore.GOOGLE + "form", explore.SHORTCUT, "application/zip"]
    )
    tree = {
        "folders": [{"key": "f0", "depth": 0, "parent": None}],
        "items": [{"mimeType": m, "folder": "f0"} for m in mimes],
        "revisited": 0,
    }
    lines = explore.summary_lines("n", tree)
    bullets = [line for line in lines if line.startswith("•")]
    listing = Listing(
        DriveFolder("fake-root0000", "n", in_shared_drive=False),
        1,
        tuple(DriveItem(f"fake-{i:08d}", "x", m, "p", None, None, None, None) for i, m in enumerate(mimes)),
        0,
    )
    flags = {"stored_only": " (stored, not read yet)", "skipped": " (skipped)"}
    mine = [
        f"• {LABELS[c.category]}: {c.count}{flags.get(c.processing_class.value, '')}"
        for c in ordered(summarize(listing))
    ]
    assert mine == bullets
    assert lines[1] == f"We found {len(mimes)} files in 1 folder."
