"""Confluence storage format (XHTML with ``ac:`` and ``ri:`` elements) to Markdown, for the Confluence importer.

``parse_storage`` builds a small tree (stdlib ``html.parser``: the prefixes need no namespace declaration, CDATA is
turned into text first); ``to_markdown`` renders headings, paragraphs, bold, italic, code, links, nested lists, task
lists, tables, quotes, code macros and the text bodies of panel macros. Links stay text links (``[text](url)``), a link
to another page keeps its title as text. Images, attachments and macros without text are dropped. Nothing here logs.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Final, override

_VOID: Final = frozenset({"br", "hr", "img", "col", "input", "meta"})
_CDATA: Final = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_SPACES: Final = re.compile(r"\s+")
_BREAK: Final = "\x00"  # a line break in inline text, kept apart from the spaces ``_collapse`` folds
_AROUND_BREAK: Final = re.compile(r"\s*\x00\s*")
_HEADINGS: Final = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_CONTAINERS: Final = frozenset(
    {
        "div",
        "section",
        "article",
        "ac:layout",
        "ac:layout-section",
        "ac:layout-cell",
        "ac:rich-text-body",
        "ac:adf-extension",
        "ac:adf-node",
        "ac:adf-content",
        "thead",
        "tbody",
    }
)
_BLOCKS: Final = _CONTAINERS | _HEADINGS | {"p", "ul", "ol", "pre", "blockquote", "table", "hr", "ac:task-list"}
_CODE_MACROS: Final = frozenset({"code", "noformat"})
_DROPPED: Final = frozenset(
    {"ac:image", "ac:parameter", "ac:placeholder", "ri:attachment", "ri:user", "script", "style"}
)
_MARKS: Final = {"strong": "**", "b": "**", "em": "*", "i": "*", "code": "`", "s": "~~", "del": "~~", "strike": "~~"}


class Node:
    """An element of the parsed storage format: lower-cased tag, attributes and children (nodes or text)."""

    __slots__ = ("attrs", "children", "tag")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    def elements(self) -> Iterator[Node]:
        """This node and every element below it, in document order."""

        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.elements()

    def text(self) -> str:
        """All text below this node, as written."""

        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def named(self, tag: str) -> Node | None:
        return next((child for child in self.children if isinstance(child, Node) and child.tag == tag), None)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self._stack = [self.root]

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {name: value or "" for name, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(Node(tag, {name: value or "" for name, value in attrs}))

    @override
    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    @override
    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def parse_storage(storage: str) -> Node:
    """The tree of a storage-format body."""

    builder = _Builder()
    builder.feed(_CDATA.sub(lambda found: html.escape(found.group(1), quote=False), storage))
    builder.close()
    return builder.root


def to_markdown(storage: str) -> str:
    """Markdown of a storage-format body (blocks separated by a blank line, no trailing newline)."""

    return "\n\n".join(_blocks(parse_storage(storage)))


def _blocks(parent: Node) -> list[str]:
    blocks: list[str] = []
    run: list[str] = []

    def flush() -> None:
        text = _collapse("".join(run))
        run.clear()
        if text:
            blocks.append(text)

    for child in parent.children:
        if isinstance(child, str) or not _is_block(child):
            run.append(_inline(child))
            continue
        flush()
        blocks.extend(_block(child))
    flush()
    return blocks


def _is_block(node: Node) -> bool:
    if node.tag == "ac:structured-macro":
        return node.attrs.get("ac:name", "") in _CODE_MACROS or node.named("ac:rich-text-body") is not None
    return node.tag in _BLOCKS


def _block(node: Node) -> list[str]:
    return _BLOCK_RENDERERS.get(node.tag, _blocks)(node)  # a container renders its children


def _heading(node: Node) -> list[str]:
    text = _inline_text(node)
    return [f"{'#' * int(node.tag[1])} {text}"] if text else []


def _paragraph(node: Node) -> list[str]:
    text = _inline_text(node)
    return [text] if text else []


def _bullets(node: Node) -> list[str]:
    lines = _list(node, "")
    return ["\n".join(lines)] if lines else []


def _task_list(node: Node) -> list[str]:
    lines = _tasks(node)
    return ["\n".join(lines)] if lines else []


def _pre(node: Node) -> list[str]:
    return [_fence(node.text(), "")]


def _quote(node: Node) -> list[str]:
    return ["\n".join(f"> {line}" if line else ">" for line in "\n\n".join(_blocks(node)).split("\n"))]


def _rule(_node: Node) -> list[str]:
    return ["---"]


def _macro(node: Node) -> list[str]:
    name = node.attrs.get("ac:name", "")
    params = {
        child.attrs.get("ac:name", ""): child.text().strip()
        for child in node.children
        if isinstance(child, Node) and child.tag == "ac:parameter"
    }
    if name in _CODE_MACROS:
        body = node.named("ac:plain-text-body")
        return [_fence(body.text() if body is not None else "", params.get("language", ""))]
    body = node.named("ac:rich-text-body")
    blocks = _blocks(body) if body is not None else []
    title = params.get("title", "")
    return ([f"**{title}**"] if title else []) + blocks


def _fence(code: str, language: str) -> str:
    return f"```{language}\n{code.strip(chr(10))}\n```"


def _list(node: Node, indent: str) -> list[str]:
    lines: list[str] = []
    number = 0
    for item in node.children:
        if not isinstance(item, Node) or item.tag != "li":
            continue
        number += 1
        marker = f"{number}. " if node.tag == "ol" else "- "
        parts: list[str] = []
        nested: list[str] = []
        for child in item.children:
            if isinstance(child, Node) and child.tag in ("ul", "ol"):
                nested += _list(child, indent + " " * len(marker))
            else:
                parts.append(_inline(child))
        lines.append(f"{indent}{marker}{_collapse(''.join(parts))}".rstrip())
        lines += nested
    return lines


def _tasks(node: Node) -> list[str]:
    lines = []
    for task in node.children:
        if isinstance(task, Node) and task.tag == "ac:task":
            status = task.named("ac:task-status")
            done = status is not None and status.text().strip() == "complete"
            body = task.named("ac:task-body")
            lines.append(f"- [{'x' if done else ' '}] {_inline_text(body) if body is not None else ''}".rstrip())
    return lines


def _table(node: Node) -> list[str]:
    rows = [
        [_cell(cell) for cell in row.children if isinstance(cell, Node) and cell.tag in ("th", "td")]
        for row in node.elements()
        if row.tag == "tr"
    ]
    rows = [row for row in rows if row]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in padded]
    lines.insert(1, "|" + " --- |" * width)
    return ["\n".join(lines)]


def _cell(cell: Node) -> str:
    return " ".join(_blocks(cell)).replace("\n", " ").replace("|", "\\|")


_BLOCK_RENDERERS: Final = {
    **dict.fromkeys(_HEADINGS, _heading),
    "p": _paragraph,
    "ul": _bullets,
    "ol": _bullets,
    "ac:task-list": _task_list,
    "pre": _pre,
    "blockquote": _quote,
    "hr": _rule,
    "table": _table,
    "ac:structured-macro": _macro,
}


def _collapse(text: str) -> str:
    return _AROUND_BREAK.sub("  \n", _SPACES.sub(" ", text)).strip()


def _inline_text(node: Node) -> str:
    return _collapse("".join(_inline(child) for child in node.children))


def _inline(node: Node | str) -> str:
    if isinstance(node, str):
        return _SPACES.sub(" ", node)
    if node.tag in _DROPPED:
        return ""
    if node.tag in _MARKS:
        text = _inline_text(node)
        return f"{_MARKS[node.tag]}{text}{_MARKS[node.tag]}" if text else ""
    if node.tag in _BLOCKS and node.tag != "ac:structured-macro":  # a block inside inline text: its text, spaced
        return " " + " ".join(_blocks(node)) + " "
    return _INLINE_RENDERERS.get(node.tag, _children_inline)(node)


def _children_inline(node: Node) -> str:
    return "".join(_inline(child) for child in node.children)


def _line_break(_node: Node) -> str:
    return _BREAK


def _anchor(node: Node) -> str:
    text, href = _inline_text(node), node.attrs.get("href", "")
    return f"[{text}]({href})" if text and href else text


def _emoticon(node: Node) -> str:
    return node.attrs.get("ac:emoji-fallback", "")


def _time(node: Node) -> str:
    return node.attrs.get("datetime", "")


def _link(node: Node) -> str:
    body = node.named("ac:link-body") or node.named("ac:plain-text-link-body")
    text = _inline_text(body) if body is not None else ""
    page = node.named("ri:page")
    url = node.named("ri:url")
    if url is not None and url.attrs.get("ri:value"):
        return f"[{text or url.attrs['ri:value']}]({url.attrs['ri:value']})"
    if page is not None:
        return text or page.attrs.get("ri:content-title", "")
    return text


def _inline_macro(node: Node) -> str:
    if node.attrs.get("ac:name") == "jira":
        for child in node.children:
            if isinstance(child, Node) and child.tag == "ac:parameter" and child.attrs.get("ac:name") == "key":
                return child.text().strip()
    return ""


_INLINE_RENDERERS: Final = {
    "br": _line_break,
    "a": _anchor,
    "ac:link": _link,
    "ac:structured-macro": _inline_macro,
    "ac:emoticon": _emoticon,
    "time": _time,
}
