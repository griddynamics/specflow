"""Sealed permission constants for API keys."""

from enum import Enum
from typing import FrozenSet


class Permission(str, Enum):
    USER = "user"
    ADMIN = "admin"


VALID_PERMISSIONS: FrozenSet[str] = frozenset(p.value for p in Permission)
