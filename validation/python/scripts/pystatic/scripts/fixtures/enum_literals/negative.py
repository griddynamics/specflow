"""Fixture: clean under enum_literals -- either not one of the eight names,
or not a declared enum value, or the real enum member is used."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from pystatic.scripts.fixtures.enum_literals import literal_models
from pystatic.scripts.fixtures.enum_literals.literal_models import (
    DirectImportedLiteralEntry,
    ImportedLiteralEntry as AliasedLiteralEntry,
)


class Account(Protocol):
    status: str


class Person(Protocol):
    name: str


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


@dataclass
class LocalLiteralEntry:
    kind: Literal["ACTIVE"]


def is_active(account: Account) -> bool:
    return account.status == AccountStatus.ACTIVE


def greet(person: Person) -> bool:
    return person.name == "ACTIVE"  # "name" is not one of the eight attribute names


def describe(account: Account) -> bool:
    return account.status == "SOMETHING_UNDECLARED"


def construct_literal_entries() -> None:
    LocalLiteralEntry(kind="ACTIVE")
    DirectImportedLiteralEntry(kind="ACTIVE")
    AliasedLiteralEntry(kind="ADMIN")
    literal_models.ImportedLiteralEntry(kind="ADMIN")
