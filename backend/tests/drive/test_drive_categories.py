"""DATA-GDUI-0001 file categories and processing classes."""

from __future__ import annotations

import pytest

from drive.categories import (
    CATEGORY_ORDER,
    LABELS,
    Category,
    ProcessingClass,
    categorize,
    processing_class,
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
OFFICE = "application/vnd.openxmlformats-officedocument."
DOCX = OFFICE + "wordprocessingml.document"
PPTX = OFFICE + "presentationml.presentation"
XLSX = OFFICE + "spreadsheetml.sheet"


def test_ac1_doc_pdf_docx():
    assert categorize(G + "document") == (
        Category.GOOGLE_DOCS,
        ProcessingClass.PROCESSED,
    )
    assert categorize("application/pdf") == (Category.PDF, ProcessingClass.PROCESSED)
    assert categorize(DOCX) == (Category.WORD, ProcessingClass.PROCESSED)  # the parser table


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
    # processed first (count desc), then stored only (count desc, then A-Z), then skipped (a DOCX is processed)
    assert labels == [
        "PDF",
        "Google Docs",
        "Word documents",
        "Images",
        "Other Google files",
    ]
    assert CategoryCount(Category.PDF, ProcessingClass.PROCESSED, 5) in counts


# Design N1: the class follows the MIME type inside the existing categories.
@pytest.mark.parametrize(
    ("mime", "category", "klass"),
    [
        (DOCX, Category.WORD, ProcessingClass.PROCESSED),
        (PPTX, Category.POWERPOINT, ProcessingClass.PROCESSED),
        (XLSX, Category.EXCEL, ProcessingClass.PROCESSED),
        ("application/pdf", Category.PDF, ProcessingClass.PROCESSED),
        ("text/html", Category.OTHER_FILES, ProcessingClass.PROCESSED),
        ("text/markdown", Category.OTHER_FILES, ProcessingClass.PROCESSED),
        ("text/csv", Category.OTHER_FILES, ProcessingClass.PROCESSED),
        # legacy Office: same category, still stored only
        ("application/msword", Category.WORD, ProcessingClass.STORED_ONLY),
        ("application/vnd.ms-powerpoint", Category.POWERPOINT, ProcessingClass.STORED_ONLY),
        ("application/vnd.ms-excel", Category.EXCEL, ProcessingClass.STORED_ONLY),
        # Drive reports a .md as text/plain: no parser type, stored only (accepted limit)
        ("text/plain", Category.OTHER_FILES, ProcessingClass.STORED_ONLY),
        ("application/octet-stream", Category.OTHER_FILES, ProcessingClass.STORED_ONLY),
        ("application/zip", Category.OTHER_FILES, ProcessingClass.STORED_ONLY),
        ("image/png", Category.IMAGES, ProcessingClass.STORED_ONLY),
        # Google-native files keep their own class (drive-convert)
        (G + "document", Category.GOOGLE_DOCS, ProcessingClass.PROCESSED),
        (G + "form", Category.OTHER_GOOGLE, ProcessingClass.SKIPPED),
        (G + "shortcut", Category.SHORTCUTS, ProcessingClass.SKIPPED),
    ],
)
def test_the_class_follows_the_mime_type_inside_the_same_category(mime, category, klass):
    assert categorize(mime) == (category, klass)


def test_a_file_name_never_makes_a_text_file_processed():
    # AC5: the name says .csv and .html, the Drive type says octet-stream
    assert categorize(item("application/octet-stream", "data.csv").mime_type)[1] is ProcessingClass.STORED_ONLY


def test_the_category_defaults_of_the_contract_do_not_change():
    assert [(c.value, processing_class(c).value) for c in CATEGORY_ORDER] == [
        ("google_docs", "processed"),
        ("google_slides", "processed"),
        ("google_sheets", "processed"),
        ("pdf", "processed"),
        ("word", "stored_only"),
        ("powerpoint", "stored_only"),
        ("excel", "stored_only"),
        ("images", "stored_only"),
        ("videos", "stored_only"),
        ("other_files", "stored_only"),
        ("other_google", "skipped"),
        ("shortcuts", "skipped"),
    ]


def test_one_category_can_hold_two_classes_in_the_summary():
    root = DriveFolder("fake-root0000", "r", in_shared_drive=False)
    items = [item(DOCX)] * 2 + [item("application/msword")] + [item("text/csv")] + [item("application/zip")] * 3
    counts = summarize(Listing(root, 1, tuple(items), 0))
    assert CategoryCount(Category.WORD, ProcessingClass.PROCESSED, 2) in counts
    assert CategoryCount(Category.WORD, ProcessingClass.STORED_ONLY, 1) in counts
    assert CategoryCount(Category.OTHER_FILES, ProcessingClass.PROCESSED, 1) in counts
    assert CategoryCount(Category.OTHER_FILES, ProcessingClass.STORED_ONLY, 3) in counts
