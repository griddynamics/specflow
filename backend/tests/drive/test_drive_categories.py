"""DATA-GDUI-0001 file categories and processing classes."""

from __future__ import annotations

import pytest

from drive.categories import (
    CATEGORY_ORDER,
    LABELS,
    Category,
    ProcessingClass,
    categorize,
)
from drive.model import (
    CategoryCount,
    DriveFolder,
    DriveItem,
    Listing,
    ordered,
    summarize,
)

G = "application/vnd.google-apps."
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_ac1_doc_pdf_docx():
    assert categorize(G + "document") == (
        Category.GOOGLE_DOCS,
        ProcessingClass.PROCESSED,
    )
    assert categorize("application/pdf") == (Category.PDF, ProcessingClass.PROCESSED)
    assert categorize(DOCX) == (Category.WORD, ProcessingClass.STORED_ONLY)


def test_ac2_form_and_shortcut():
    assert categorize(G + "form") == (Category.OTHER_GOOGLE, ProcessingClass.SKIPPED)
    assert categorize(G + "shortcut") == (Category.SHORTCUTS, ProcessingClass.SKIPPED)


def test_ac3_unknown_upload_is_other_files():
    assert categorize("application/zip") == (
        Category.OTHER_FILES,
        ProcessingClass.STORED_ONLY,
    )
    assert categorize("") == (Category.OTHER_FILES, ProcessingClass.STORED_ONLY)


@pytest.mark.parametrize(
    ("mime", "category"),
    [
        (G + "presentation", Category.GOOGLE_SLIDES),
        (G + "spreadsheet", Category.GOOGLE_SHEETS),
        ("application/msword", Category.WORD),
        ("application/vnd.ms-powerpoint", Category.POWERPOINT),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            Category.POWERPOINT,
        ),
        ("application/vnd.ms-excel", Category.EXCEL),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            Category.EXCEL,
        ),
        ("image/png", Category.IMAGES),
        ("video/mp4", Category.VIDEOS),
        (G + "drawing", Category.OTHER_GOOGLE),
        (G + "site", Category.OTHER_GOOGLE),
    ],
)
def test_every_category_by_mime(mime, category):
    assert categorize(mime)[0] is category


def test_ac4_folder_is_not_a_category():
    with pytest.raises(ValueError, match="a folder has no file category"):
        categorize(G + "folder")


def item(mime: str, name: str = "x") -> DriveItem:
    return DriveItem("fake-aaaaaa", name, mime, "p", None, None, None, None)


def test_ac5_name_never_decides():
    assert categorize(item(G + "document", "report.docx").mime_type)[0] is Category.GOOGLE_DOCS


def test_every_category_has_one_class_and_label():
    assert len(CATEGORY_ORDER) == 12 == len(LABELS)
    assert {categorize(m)[1] for m in ("application/pdf", "image/gif")} == {
        ProcessingClass.PROCESSED,
        ProcessingClass.STORED_ONLY,
    }


def test_summarize_counts_and_order():
    root = DriveFolder("fake-root0000", "r", in_shared_drive=False)
    items = (
        [item(G + "document")] * 2
        + [item("application/pdf")] * 5
        + [item("image/png")] * 5
        + [item(G + "form")]
        + [item(DOCX)]
    )
    counts = summarize(Listing(root, 1, tuple(items), 0))
    assert sum(c.count for c in counts) == 14
    labels = [LABELS[c.category] for c in ordered(counts)]
    # processed first (count desc), then stored only (count desc, then A-Z), then skipped
    assert labels == [
        "PDF",
        "Google Docs",
        "Images",
        "Word documents",
        "Other Google files",
    ]
    assert CategoryCount(Category.PDF, ProcessingClass.PROCESSED, 5) in counts
