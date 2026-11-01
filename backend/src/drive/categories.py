"""File categories and processing classes (DATA-GDUI-0001).

A category is chosen by the Drive ``mimeType`` only, never by the file name
(DATA-GDUI-0001.AC5). Folders belong to no category.

The processing class follows the MIME type too (design N1, no new category): a file whose
type is in the parser table (``file_parsers.formats``: PDF, OOXML Word/PowerPoint/Excel, HTML,
Markdown, CSV) is ``processed``; legacy ``.doc/.ppt/.xls`` and every other type keep the class of
their category. A ``.md`` that Drive reports as ``text/plain`` has no parser type and stays
``stored_only`` (accepted limit).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from file_parsers.formats import format_for_media_type

GOOGLE: Final = "application/vnd.google-apps."
FOLDER_MIME: Final = GOOGLE + "folder"
SHORTCUT_MIME: Final = GOOGLE + "shortcut"


class Category(StrEnum):
    GOOGLE_DOCS = "google_docs"
    GOOGLE_SLIDES = "google_slides"
    GOOGLE_SHEETS = "google_sheets"
    PDF = "pdf"
    WORD = "word"
    POWERPOINT = "powerpoint"
    EXCEL = "excel"
    IMAGES = "images"
    VIDEOS = "videos"
    OTHER_FILES = "other_files"
    OTHER_GOOGLE = "other_google"
    SHORTCUTS = "shortcuts"


class ProcessingClass(StrEnum):
    PROCESSED = "processed"
    STORED_ONLY = "stored_only"
    SKIPPED = "skipped"


# Table order of DATA-GDUI-0001; the UI uses the same order (contract.json).
CATEGORY_ORDER: Final[tuple[Category, ...]] = tuple(Category)
CLASS_ORDER: Final[tuple[ProcessingClass, ...]] = tuple(ProcessingClass)

LABELS: Final[Mapping[Category, str]] = {
    Category.GOOGLE_DOCS: "Google Docs",
    Category.GOOGLE_SLIDES: "Google Slides",
    Category.GOOGLE_SHEETS: "Google Sheets",
    Category.PDF: "PDF",
    Category.WORD: "Word documents",
    Category.POWERPOINT: "PowerPoint files",
    Category.EXCEL: "Excel files",
    Category.IMAGES: "Images",
    Category.VIDEOS: "Videos",
    Category.OTHER_FILES: "Other files",
    Category.OTHER_GOOGLE: "Other Google files",
    Category.SHORTCUTS: "Shortcuts",
}

_CLASS_OF: Final[Mapping[Category, ProcessingClass]] = {
    Category.GOOGLE_DOCS: ProcessingClass.PROCESSED,
    Category.GOOGLE_SLIDES: ProcessingClass.PROCESSED,
    Category.GOOGLE_SHEETS: ProcessingClass.PROCESSED,
    Category.PDF: ProcessingClass.PROCESSED,
    Category.WORD: ProcessingClass.STORED_ONLY,
    Category.POWERPOINT: ProcessingClass.STORED_ONLY,
    Category.EXCEL: ProcessingClass.STORED_ONLY,
    Category.IMAGES: ProcessingClass.STORED_ONLY,
    Category.VIDEOS: ProcessingClass.STORED_ONLY,
    Category.OTHER_FILES: ProcessingClass.STORED_ONLY,
    Category.OTHER_GOOGLE: ProcessingClass.SKIPPED,
    Category.SHORTCUTS: ProcessingClass.SKIPPED,
}

_OFFICE = "application/vnd.openxmlformats-officedocument."
_BY_MIME: Final[Mapping[str, Category]] = {
    GOOGLE + "document": Category.GOOGLE_DOCS,
    GOOGLE + "presentation": Category.GOOGLE_SLIDES,
    GOOGLE + "spreadsheet": Category.GOOGLE_SHEETS,
    "application/pdf": Category.PDF,
    "application/msword": Category.WORD,
    _OFFICE + "wordprocessingml.document": Category.WORD,
    "application/vnd.ms-powerpoint": Category.POWERPOINT,
    _OFFICE + "presentationml.presentation": Category.POWERPOINT,
    "application/vnd.ms-excel": Category.EXCEL,
    _OFFICE + "spreadsheetml.sheet": Category.EXCEL,
    SHORTCUT_MIME: Category.SHORTCUTS,
}


#: Categories whose class depends on the MIME type (the parser table decides).
_BY_PARSER_TABLE: Final = frozenset({Category.WORD, Category.POWERPOINT, Category.EXCEL, Category.OTHER_FILES})


def processing_class(category: Category) -> ProcessingClass:
    """The default class of a category (the contract); ``categorize`` refines it by MIME type."""

    return _CLASS_OF[category]


def _class_of(category: Category, mime_type: str) -> ProcessingClass:
    if category in _BY_PARSER_TABLE and format_for_media_type(mime_type) is not None:
        return ProcessingClass.PROCESSED
    return _CLASS_OF[category]


def categorize(mime_type: str) -> tuple[Category, ProcessingClass]:
    """Category and processing class of one non-folder item, by mimeType only."""

    if mime_type == FOLDER_MIME:
        raise ValueError("a folder has no file category")
    category = _BY_MIME.get(mime_type)
    if category is None:
        if mime_type.startswith("image/"):
            category = Category.IMAGES
        elif mime_type.startswith("video/"):
            category = Category.VIDEOS
        elif mime_type.startswith(GOOGLE):
            category = Category.OTHER_GOOGLE
        else:
            category = Category.OTHER_FILES
    return category, _class_of(category, mime_type)
