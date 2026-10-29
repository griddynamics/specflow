"""Recorded folder listings (DATA-GDUI-0003) turned into Drive-shaped data.

A recording has no ids or versions; they are made here: folder
``fake-{rec}-{key}`` (the root ``f0`` is just ``fake-{rec}``, the pasteable id),
item ``fake-{rec}-i{index}``, version 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from drive.model import DriveFolder, DriveItem


def default_fixtures() -> Traversable:
    return files("drive").joinpath("fixtures")


def as_traversable(source: Traversable | Path | None) -> Traversable:
    return default_fixtures() if source is None else source


def recording_names(fixtures: Traversable) -> list[str]:
    folder = fixtures.joinpath("recordings")
    if not folder.is_dir():
        return []
    return sorted(e.name.removesuffix(".json") for e in folder.iterdir() if e.name.endswith(".json"))


@dataclass(frozen=True)
class RecordedTree:
    name: str
    folders: dict[str, DriveFolder]  # by folder id
    children: dict[str, list[str]]  # folder id -> subfolder ids
    items: dict[str, list[DriveItem]]  # folder id -> items in it
    by_id: dict[str, DriveItem]
    root_id: str


def folder_id(rec: str, key: str) -> str:
    return f"fake-{rec}" if key == "f0" else f"fake-{rec}-{key}"


def load(fixtures: Traversable, name: str) -> RecordedTree:
    data: dict[str, Any] = json.loads(fixtures.joinpath("recordings").joinpath(f"{name}.json").read_text("utf-8"))
    folders: dict[str, DriveFolder] = {}
    children: dict[str, list[str]] = {}
    items: dict[str, list[DriveItem]] = {}
    by_key: dict[str, str] = {}
    for f in data["folders"]:
        fid = folder_id(name, f["key"])
        by_key[f["key"]] = fid
        folders[fid] = DriveFolder(id=fid, name=f["name"], in_shared_drive=False)
        children[fid] = []
        items[fid] = []
    for f in data["folders"]:
        if f["parent"] is not None:
            children[by_key[f["parent"]]].append(by_key[f["key"]])
    by_id: dict[str, DriveItem] = {}
    for index, i in enumerate(data["items"]):
        parent = by_key[i["folder"]]
        item = DriveItem(
            id=f"fake-{name}-i{index}",
            name=i["name"],
            mime_type=i["mimeType"],
            parent_id=parent,
            size=i.get("size"),
            modified_time=i.get("modifiedTime"),
            version=1,
            md5=None,
        )
        items[parent].append(item)
        by_id[item.id] = item
    return RecordedTree(name, folders, children, items, by_id, by_key["f0"])


def find_recording(fixtures: Traversable, some_id: str) -> str | None:
    """The recording a fake folder or item id belongs to, if any."""

    for rec in recording_names(fixtures):
        base = f"fake-{rec}"
        if some_id == base or some_id.startswith(base + "-"):
            return rec
    return None


__all__ = [
    "RecordedTree",
    "as_traversable",
    "default_fixtures",
    "find_recording",
    "folder_id",
    "load",
    "recording_names",
]
