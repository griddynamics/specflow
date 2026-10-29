"""Resolves the `package.module:attribute.path` references `sqlcheck.toml` uses to point
at the project's own objects (its SQLAlchemy metadata, its engine factory), and the
SQLAlchemy type families `schema-parity` compares against."""

from __future__ import annotations

import importlib
from typing import Any

import sqlalchemy.types


def resolve(reference: str) -> Any:
    """`"app.models:Base.metadata"` -> the project object it names.

    The module is imported, then each dotted attribute is read in turn."""
    module_name, sep, attr_path = reference.partition(":")
    if not sep or not module_name or not attr_path:
        msg = f"expected 'package.module:attribute', got {reference!r}"
        raise ValueError(msg)
    target: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        target = getattr(target, part)
    return target


def type_class(name: str) -> type:
    """A SQLAlchemy type class by name (`"String"`), or a project type by reference
    (`"app.models.types:UtcDateTime"`)."""
    found = resolve(name) if ":" in name else getattr(sqlalchemy.types, name, None)
    if not isinstance(found, type):
        msg = f"{name!r} is not a SQLAlchemy type class"
        raise ValueError(msg)
    return found
