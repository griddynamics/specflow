"""The block structure of Notion Markdown: lines in, a tree of NFM tags out.

Line-based, no HTML parser. A tag of the vocabulary at the start or end of a line opens or closes a node; text between
is a line of its node. Fenced code and ``$$`` math are copied as they are (a tag in them is text). Tags may be left open
(they close at the end), a stray close is ignored, and nesting deeper than ``MAX_DEPTH`` raises
``MarkupTooDeepError``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from notion.convert.inline import BLOCK_TAGS, MAX_DEPTH, attrs_of
from notion.convert.result import MarkupTooDeepError

MAX_TABLE_CHARS: Final = 400_000
# Tags that are never a pair: an open tag alone is the whole element.
_SINGLE: Final = frozenset({"table-of-contents", "link-preview", "empty-block", "col"})
_NAMES: Final = "|".join(sorted((t for t in BLOCK_TAGS if t not in {"tr", "td", "th"}), key=len, reverse=True))
_TAG: Final = re.compile(rf"<(?P<slash>/?)(?P<name>{_NAMES})(?=[\s/>])(?P<attrs>[^<>]*?)(?P<self>/?)>")
_FENCE: Final = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(slots=True)
class Raw:
    """A line of text. ``literal`` lines (code, math) are copied as they are."""

    text: str
    literal: bool = False


@dataclass(slots=True)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    indent: str = ""
    body: list[Raw | Node] = field(default_factory=list)


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


class _Parser:
    def __init__(self) -> None:
        self.root = Node("")
        self.stack: list[Node] = [self.root]
        self.fence: tuple[str, int] | None = None
        self.math = False
        self.table: tuple[Node, list[str], int] | None = None  # node, text so far, size

    def feed(self, line: str) -> None:
        if self.table is not None:
            self._table_text(line)
        elif self.fence is not None:
            self._literal(line)
            stripped = line.strip()
            if stripped and set(stripped) == {self.fence[0]} and len(stripped) >= self.fence[1]:
                self.fence = None
        elif self.math:
            self._literal(line)
            self.math = line.strip() != "$$"
        elif (m := _FENCE.match(line)) is not None:
            self.fence = (m.group(1)[0], len(m.group(1)))
            self._literal(line)
        elif line.strip() == "$$":
            self.math = True
            self._literal(line)
        else:
            self._structure(line)

    def finish(self) -> Node:
        if self.table is not None:
            self._table_end()
        return self.root

    def _literal(self, line: str) -> None:
        self.stack[-1].body.append(Raw(line, literal=True))

    def _structure(self, line: str) -> None:
        text = line.strip()
        if not text or not (text[0] == "<" or text.endswith(">")):
            self.stack[-1].body.append(Raw(line))
            return
        indent = indent_of(line)
        pos, end, found = 0, len(text), False
        while (m := _TAG.match(text, pos, end)) is not None:
            found = True
            pos = m.end()
            name = m.group("name")
            if m.group("slash"):
                self._close(name)
            elif name == "table" and not m.group("self"):
                self._table_start(m.group("attrs"), indent)
                self._table_text(text[pos:])
                return
            else:
                self._open(name, m.group("attrs"), indent, m.group("self") or name in _SINGLE)
        closers: list[str] = []
        while end > pos and text[end - 1] == ">" and (at := text.rfind("</", pos, end)) >= 0:
            m = _TAG.fullmatch(text, at, end)
            if m is None or not m.group("slash") or (at > pos and text[at - 1] == "\\"):
                break  # not a closer, or an escaped literal (``\\</callout>``): text, not the end of the block
            closers.append(m.group("name"))
            end = at
            found = True
        if not found:
            self.stack[-1].body.append(Raw(line))
            return
        if text[pos:end].strip():
            self.stack[-1].body.append(Raw(indent + text[pos:end].strip()))
        for name in closers:
            self._close(name)

    def _open(self, name: str, attrs: str, indent: str, single: object) -> None:
        node = Node(name, attrs_of(attrs), indent)
        self.stack[-1].body.append(node)
        if not single:
            if len(self.stack) >= MAX_DEPTH:
                raise MarkupTooDeepError("blocks are nested too deeply")
            self.stack.append(node)

    def _close(self, name: str) -> None:
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == name:
                del self.stack[depth:]
                return

    def _table_start(self, attrs: str, indent: str) -> None:
        node = Node("table", attrs_of(attrs), indent)
        self.stack[-1].body.append(node)
        self.table = (node, [], 0)

    def _table_text(self, text: str) -> None:
        if self.table is None:
            return
        node, parts, size = self.table
        before, closed, after = text.partition("</table>")
        if size < MAX_TABLE_CHARS:
            parts.append(before)
            size += len(before) + 1
        self.table = (node, parts, size)
        if closed:
            self._table_end()
            if after.strip():
                self._structure(node.indent + after.strip())

    def _table_end(self) -> None:
        if self.table is not None:
            node, parts, _ = self.table
            node.body.append(Raw("\n".join(parts), literal=True))
            self.table = None


def parse(text: str) -> Node:
    """The tree of ``text`` (lines split on ``\\n``)."""

    parser = _Parser()
    for line in text.split("\n"):
        parser.feed(line)
    return parser.finish()
