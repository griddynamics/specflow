"""One depth-first walk over a JSON tree, shared by every oracle that needs one.

Three checks want the same traversal and differ only in what they match: spec
anchors (totality), evasion values (totality), and ``$ref`` targets (contracts).
Written three times, a change to path formatting or a new container type is a
three-place edit. Written once, each caller is a filter.

Both paths a caller might report on are carried, because they are not the same
thing: an evasion is reported at the value's own path, while an unpaid escape
hatch is reported at the path of the element that *holds* the anchor.
"""

from __future__ import annotations

from typing import Any, Iterator, NamedTuple


class Node(NamedTuple):
    """One value in a JSON tree."""

    path: str
    """The value's own JSON path, e.g. ``entities[0].fields[1].name``."""

    owner: str
    """Path of the container holding it, e.g. ``entities[0].fields[1]``."""

    key: str | None
    """The dict key that produced it, or ``None`` for a list element."""

    value: Any

    @property
    def is_leaf(self) -> bool:
        return not isinstance(self.value, (dict, list))


def walk(node: Any, path: str = "", key: str | None = None, owner: str = "") -> Iterator[Node]:
    """Yield ``node`` and every value beneath it, containers included.

    The root is yielded first, so a caller can hand in a bare scalar and still
    have it checked.
    """
    yield Node(path, owner, key, node)
    if isinstance(node, dict):
        for child_key, value in node.items():
            child = f"{path}.{child_key}" if path else child_key
            yield from walk(value, child, child_key, path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk(item, f"{path}[{index}]", None, path)
