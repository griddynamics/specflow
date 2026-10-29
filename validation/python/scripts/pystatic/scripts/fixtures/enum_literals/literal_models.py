"""Literal and enum-owned constructor fields for enum_literals fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


@dataclass
class ImportedLiteralEntry:
    kind: Literal["ADMIN"]


@dataclass
class DirectImportedLiteralEntry:
    kind: Literal["ACTIVE"]


class FixtureStatus(StrEnum):
    ACTIVE = "ACTIVE"


class ImportedEnumEntry:
    status: FixtureStatus

    def __init__(self, *, status: str) -> None:
        del status
