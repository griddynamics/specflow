"""The bundle's conventions, read from the `[docs-bundle]` configuration table.

Every key is optional; `pystatic.example.toml` documents each one with a neutral example.
An empty list or string disables the rule that reads it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pystatic import config

SECTION = "docs-bundle"

DEFAULT_FIELDS = ("type", "title", "description", "status", "tags")
DEFAULT_STATUSES = ("draft", "stable", "deprecated")
_STATUSES = frozenset(DEFAULT_STATUSES)
DEFAULT_ID_PATTERN = r"^`?([A-Z]{1,6}-[A-Z0-9]+(?:-[A-Z0-9]+)*)`?$"
#: Markers of work in progress; each is reported once per file.
DEFAULT_EPHEMERAL: tuple[tuple[str, str], ...] = (
    ("status glyph", "[✅⚠❌]"),
    ("revised marker", "↺"),
    ("code citation", r"\b[\w.-]+(?:/[\w.-]+)*\.(?:py|pyi|tsx?|jsx?|mjs|cjs|go|rs|java|kt):\d+"),
    ("section-number reference", r"(?<![\w/])§\s?\d"),
    ("not-implemented tag", r"\[NOT IMPLEMENTED"),
    ("revision note", r"\brevision \d"),
    ("rev note", r"\brev\.? ?\d+\b"),
)


@dataclass(frozen=True)
class Folder:
    path: str
    title: str
    types: frozenset[str] = frozenset()
    order: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class NumberedRecords:
    folder: str
    filename: re.Pattern[str]
    heading: re.Pattern[str]


@dataclass(frozen=True)
class IdPlacement:
    pattern: re.Pattern[str]
    forbidden_folders: tuple[str, ...]
    allowed_files: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EphemeralExempt:
    file: str
    after: re.Pattern[str]


@dataclass(frozen=True)
class Bundle:
    root: str = "docs"
    index_name: str = "index.md"
    root_title: str = "Documentation"
    root_preamble: tuple[str, ...] = ()
    fields: tuple[str, ...] = DEFAULT_FIELDS
    optional_fields: frozenset[str] = frozenset()
    types: frozenset[str] = frozenset()
    statuses: frozenset[str] = _STATUSES
    tag_folder: str = ""
    extra_tags: frozenset[str] = frozenset()
    reading_order_heading: str = ""
    id_pattern: re.Pattern[str] | None = field(
        default_factory=lambda: re.compile(DEFAULT_ID_PATTERN)
    )
    ascii_headings: bool = True
    forbid_numbered_headings: bool = True
    forbidden_heading_chars: tuple[str, ...] = ("§",)
    folders: tuple[Folder, ...] = ()
    numbered_records: tuple[NumberedRecords, ...] = ()
    id_placement: tuple[IdPlacement, ...] = ()
    ephemeral: tuple[tuple[str, re.Pattern[str]], ...] = field(
        default_factory=lambda: tuple((n, re.compile(p)) for n, p in DEFAULT_EPHEMERAL)
    )
    ephemeral_exempt: tuple[EphemeralExempt, ...] = ()

    def folder_types(self) -> dict[str, frozenset[str]]:
        return {f.path: f.types for f in self.folders if f.types}

    def docs_dir(self, target: Path) -> Path:
        return target / self.root


def _str(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        msg = f"[{SECTION}] `{key}` must be a string"
        raise config.ConfigError(msg)
    return value


def _bool(table: dict[str, Any], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        msg = f"[{SECTION}] `{key}` must be true or false"
        raise config.ConfigError(msg)
    return value


def _tables(table: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        msg = f"[{SECTION}] `{key}` must be an array of tables ([[{SECTION}.{key}]])"
        raise config.ConfigError(msg)
    return value


def _folder(raw: dict[str, Any]) -> Folder:
    return Folder(
        path=_str(raw, "path", ""),
        title=_str(raw, "title", ""),
        types=frozenset(config.string_list(raw, "types", [])),
        order=tuple(config.string_list(raw, "order", [])),
        summary=_str(raw, "summary", ""),
    )


def _ephemeral(table: dict[str, Any]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    if "ephemeral" not in table:
        return tuple((n, re.compile(p)) for n, p in DEFAULT_EPHEMERAL)
    return tuple(
        (_str(raw, "name", "marker"), re.compile(_str(raw, "pattern", "")))
        for raw in _tables(table, "ephemeral")
    )


def from_table(table: dict[str, Any]) -> Bundle:
    id_pattern = _str(table, "id-pattern", DEFAULT_ID_PATTERN)
    return Bundle(
        root=_str(table, "root", "docs"),
        index_name=_str(table, "index-name", "index.md"),
        root_title=_str(table, "root-title", "Documentation"),
        root_preamble=tuple(config.string_list(table, "root-preamble", [])),
        fields=tuple(config.string_list(table, "fields", list(DEFAULT_FIELDS))),
        optional_fields=frozenset(config.string_list(table, "optional-fields", [])),
        types=frozenset(config.string_list(table, "types", [])),
        statuses=frozenset(config.string_list(table, "statuses", list(DEFAULT_STATUSES))),
        tag_folder=_str(table, "tag-folder", ""),
        extra_tags=frozenset(config.string_list(table, "extra-tags", [])),
        reading_order_heading=_str(table, "reading-order-heading", ""),
        id_pattern=re.compile(id_pattern) if id_pattern else None,
        ascii_headings=_bool(table, "ascii-headings", default=True),
        forbid_numbered_headings=_bool(table, "forbid-numbered-headings", default=True),
        forbidden_heading_chars=tuple(config.string_list(table, "forbidden-heading-chars", ["§"])),
        folders=tuple(_folder(raw) for raw in _tables(table, "folders")),
        numbered_records=tuple(
            NumberedRecords(
                folder=_str(raw, "folder", ""),
                filename=re.compile(_str(raw, "filename", r".*")),
                heading=re.compile(_str(raw, "heading", r"^(.+)$")),
            )
            for raw in _tables(table, "numbered-records")
        ),
        id_placement=tuple(
            IdPlacement(
                pattern=re.compile(_str(raw, "pattern", "")),
                forbidden_folders=tuple(config.string_list(raw, "forbidden-folders", [])),
                allowed_files=frozenset(config.string_list(raw, "allowed-files", [])),
            )
            for raw in _tables(table, "id-placement")
        ),
        ephemeral=_ephemeral(table),
        ephemeral_exempt=tuple(
            EphemeralExempt(
                file=_str(raw, "file", ""), after=re.compile(_str(raw, "after", ""), re.MULTILINE)
            )
            for raw in _tables(table, "ephemeral-exempt")
        ),
    )


def load() -> Bundle:
    table = config.section(SECTION)
    if not isinstance(table, dict):
        msg = f"[{SECTION}] must be a table"
        raise config.ConfigError(msg)
    return from_table(table)
