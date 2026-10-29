"""Fixture: violates enum_literals in a Compare, an Assign and a keyword."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pystatic.scripts.fixtures.enum_literals.literal_models import ImportedEnumEntry


@dataclass
class UntypedEntry:
    kind: str


class MismatchedLiteralEntry:
    kind: Literal["INACTIVE"]

    def __init__(self, *, kind: str) -> None:
        del kind


def unresolved_constructor(*, kind: str) -> None:
    del kind


class Stateful(Protocol):
    status: str


def is_active(item: Stateful) -> bool:
    return item.status == "ACTIVE"


def close(session: Stateful) -> None:
    session.status = "ACTIVE"


def make(row: object) -> None:
    del row
    configure(role="ADMIN")


def configure(*, role: str) -> None:
    del role


def construct_non_literal_entries() -> None:
    UntypedEntry(kind="ACTIVE")
    ImportedEnumEntry(status="ACTIVE")
    MismatchedLiteralEntry(kind="ACTIVE")
    unresolved_constructor(kind="ACTIVE")


def assign_literal_variable() -> None:
    status: Literal["ACTIVE"] = "ACTIVE"
    del status


def construct_unresolved_literal(constructor: Callable[..., object]) -> None:
    constructor(kind="ADMIN")
