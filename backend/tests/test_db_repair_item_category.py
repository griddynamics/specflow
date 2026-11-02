"""The backfill (repair step B2) names the same file category as ``db.batches.item_category``.

Uploads may be DOCX, PPTX, XLSX, HTML, Markdown or CSV, so a legacy-day batch item of such a
document must not be filed as ``other_files`` when a new upload of it is ``word``, ``powerpoint`` or ``excel``.
"""

from __future__ import annotations

import re

import pytest

from db.batches import item_category
from db.source_repair import B2_ITEMS
from file_parsers.formats import FORMATS

EQUALS = re.compile(r"WHEN d\.media_type = '([^']+)' THEN '([^']+)'")
SUFFIX = re.compile(r"WHEN d\.media_type LIKE '%([^']+)' THEN '([^']+)'")
ELSE = re.compile(r"ELSE '([^']+)'")


def sql_category(media_type: str) -> str:
    """What the CASE of the statement answers for ``media_type`` (``=`` and ``LIKE '%suffix'`` arms, in order)."""

    arms = sorted(
        [(m.start(), m.group(1), m.group(2), False) for m in EQUALS.finditer(B2_ITEMS)]
        + [(m.start(), m.group(1), m.group(2), True) for m in SUFFIX.finditer(B2_ITEMS)]
    )
    for _, text, category, is_suffix in arms:
        if media_type.endswith(text) if is_suffix else media_type == text:
            return category
    return ELSE.search(B2_ITEMS).group(1)


@pytest.mark.parametrize("spec", FORMATS, ids=[s.format.value for s in FORMATS])
def test_every_accepted_media_type_gets_the_batch_category(spec):
    assert sql_category(spec.media_type) == item_category(spec.media_type)


def test_an_unknown_media_type_is_other_files_in_both():
    assert sql_category("application/zip") == item_category("application/zip") == "other_files"
    assert sql_category("application/msword") == item_category("application/msword") == "other_files"


def test_the_backfilled_item_is_processed_and_pending():
    assert "'processed'" in B2_ITEMS and "'pending'" in B2_ITEMS
