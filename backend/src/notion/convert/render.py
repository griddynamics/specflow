"""The tree of ``tree`` as GFM, and the sub-pages and databases it names (docs/notion/discovery.md "Conversion test").

Every NFM element has one rule: a callout is a blockquote, a toggle a heading or a bold line, tabs and columns are
flattened, a styled table is a GFM table, meeting notes keep the summary and the notes (not the transcript), a custom
block is dropped, a link block is a link, an image is only named. Nothing is fetched."""

from __future__ import annotations

import re
from typing import Final

from drive.convert._md import clean, link_target, table_md
from notion.convert.inline import inline
from notion.convert.result import ConvertedPage
from notion.convert.scrub import scrub_text
from notion.convert.tree import Node, Raw, indent_of, parse
from notion.links import dashed, parse_link
from notion.model import DatabaseRef, PageRef
from notion.reasons import NotionError

MAX_CHARS: Final = 2_000_000
TRUNCATED: Final = "[Content truncated]"
_ICON_MAX: Final = 8  # an emoji; a longer icon is a custom emoji or a URL, which is dropped
_DROPPED: Final = frozenset(
    {"table-of-contents", "link-preview", "empty-block", "custom-block", "transcript", "col", "colgroup"}
)
_LINKS: Final = frozenset({"bookmark", "embed", "pdf", "file", "audio", "video"})
_TITLED: Final = frozenset({"toggle", "tab", "meeting-notes"})
_HEADING: Final = re.compile(r"h([1-6])")
_BLOCK_START: Final = re.compile(r"(#{1,6} |[-*+] |\d+[.)] |>|```|~~~|\||---)")
_COLLECTION: Final = re.compile(r"collection://([0-9a-fA-F-]+)")
_HEX32: Final = re.compile(r"[0-9a-fA-F]{32}")
_CELL_TAG: Final = re.compile(r"<(/?)(tr|td|th)(?=[\s/>])[^<>]*?(/?)>", re.IGNORECASE)


class _Out:
    """Lines with single blank lines between blocks; ``item`` lines (sub-pages) stay together."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.after_item = False

    def blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def _settle(self) -> None:
        if self.after_item:
            self.blank()
            self.after_item = False

    def line(self, text: str) -> None:
        if not text.strip():
            self.blank()
            return
        self._settle()
        self.lines.append(text)

    def literal(self, text: str) -> None:
        self._settle()
        self.lines.append(text)

    def block(self, lines: list[str]) -> None:
        if lines:
            self._settle()
            self.blank()
            self.lines.extend(lines)
            self.blank()

    def item(self, text: str) -> None:
        if not self.after_item:
            self.blank()
        self.lines.append(text)
        self.after_item = True

    def finish(self) -> list[str]:
        lines = self.lines
        while lines and not lines[-1].strip():
            lines.pop()
        start = 0
        while start < len(lines) and not lines[start].strip():
            start += 1
        return lines[start:]


def _common(items: list[Raw | Node]) -> str:
    """The indentation shared by the non-blank lines and nodes of a body."""

    seen = [
        it.indent if isinstance(it, Node) else indent_of(it.text)
        for it in items
        if isinstance(it, Node) or it.text.strip()
    ]
    prefix = seen[0] if seen else ""
    for other in seen[1:]:
        while not other.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def _strip(text: str, dedent: str) -> str:
    return text[len(dedent) :] if text.startswith(dedent) else text.lstrip(" \t")


def _caption(node: Node) -> str:
    return " ".join(inline(p) for it in node.body if isinstance(it, Raw) and (p := it.text.strip()))


def _split_title(body: list[Raw | Node]) -> tuple[str, list[Raw | Node]]:
    """The first line of a body (its title) and the items after it; no title when a node comes first."""

    for index, item in enumerate(body):
        if isinstance(item, Node):
            return "", body
        if item.text.strip():
            return inline(item.text.strip()), body[index + 1 :]
    return "", []


def _quote(lines: list[str]) -> list[str]:
    return [f"> {line}" if line.strip() else ">" for line in lines]


class _Renderer:
    def __init__(self) -> None:
        self.pages: list[PageRef] = []
        self.databases: list[DatabaseRef] = []

    def items(self, body: list[Raw | Node], dedent: str = "") -> list[str]:
        out = _Out()
        for item in body:
            if isinstance(item, Raw):
                text = _strip(item.text, dedent)
                if item.literal:
                    out.literal(text)
                else:
                    out.line(text[: len(text) - len(text.lstrip())] + inline(text.lstrip()).lstrip())
            else:
                rel = item.indent[len(dedent) :] if item.indent.startswith(dedent) else ""
                self._node(item, out, rel)
        return out.finish()

    def _node(self, node: Node, out: _Out, rel: str) -> None:
        tag = node.tag
        if tag in _DROPPED:
            return
        if tag in ("page", "database"):
            out.item(rel + self._ref(node))
            return
        if tag == "table":
            lines = self._table(node)
        elif tag in _LINKS:
            lines = self._link(node)
        elif tag == "image":
            caption = _caption(node)
            lines = [f"Image: {caption}"] if caption else []
        elif tag in ("callout", "quote"):
            lines = self._callout(node)
        elif tag in _TITLED:
            lines = self._titled(node)
        else:
            lines = self.items(node.body, _common(node.body))  # columns, tabs, synced blocks: their content
        out.block([rel + line if line.strip() else line for line in lines])

    def _callout(self, node: Node) -> list[str]:
        lines = self.items(node.body, _common(node.body))
        icon = node.attrs.get("icon", "") if node.tag == "callout" else ""
        if not icon or len(icon) > _ICON_MAX or ":" in icon or "/" in icon:
            return _quote(lines)
        if lines and not _BLOCK_START.match(lines[0]):
            lines[0] = f"{icon} {lines[0]}"
            return _quote(lines)
        return _quote([icon, "", *lines])

    def _titled(self, node: Node) -> list[str]:
        title, rest = _split_title(node.body)
        out = _Out()
        if title:
            level = _HEADING.fullmatch(node.attrs.get("heading", ""))
            if node.tag == "tab":
                out.line(f"#### {title}")
            elif node.tag == "meeting-notes":
                out.line(f"**{title}**")
            else:
                out.line(f"{'#' * int(level.group(1))} {title}" if level else f"**{title}**")
        out.block(self.items(rest, _common(rest)))
        return out.finish()

    def _link(self, node: Node) -> list[str]:
        source = next((node.attrs[k] for k in ("source", "src", "url") if node.attrs.get(k)), "")
        caption = _caption(node)
        target = link_target(source)
        if target is None:
            return [caption] if caption else []
        return [f"[{caption or target}]({target})"]

    def _ref(self, node: Node) -> str:
        url, caption = node.attrs.get("url", ""), _caption(node)
        title = caption or "Untitled"
        try:
            page_id = parse_link(url).id
        except NotionError:
            page_id = None
        if node.tag == "page":
            if page_id:
                self.pages.append(PageRef(page_id, title, url))
            kind = "page"
        else:
            source = _COLLECTION.fullmatch(node.attrs.get("data-source-url", ""))
            plain = source.group(1).replace("-", "") if source else ""
            if page_id:
                self.databases.append(DatabaseRef(title, url, dashed(plain) if _HEX32.fullmatch(plain) else None))
            kind = "database"
        target = link_target(url)
        return f"- Sub-{kind}: [{title}]({target})" if target else f"- Sub-{kind}: {title}"

    def _table(self, node: Node) -> list[str]:
        """A styled ``<table>`` as GFM; the first row is the header (GFM has no table without one)."""

        text = node.body[0].text if node.body and isinstance(node.body[0], Raw) else ""
        rows = _table_rows(text)
        return table_md(rows).split("\n") if rows else []


def _table_rows(text: str) -> list[list[str]]:
    """The rows of ``<tr>`` and ``<td>``/``<th>`` tags in ``text`` (cell text as GFM); other tags are cell text."""

    rows: list[list[str]] = []
    cell: list[str] | None = None
    pos = 0
    for m in _CELL_TAG.finditer(text):
        if cell is not None:
            cell.append(text[pos : m.start()])
            rows[-1].append(inline(" ".join("".join(cell).split())))
            cell = None
        pos = m.end()
        if m.group(2).lower() == "tr":
            if not m.group(1):
                rows.append([])
        elif not m.group(1):
            if not rows:
                rows.append([])
            cell = []
            if m.group(3):
                rows[-1].append("")
                cell = None
    if cell is not None:
        cell.append(text[pos:])
        rows[-1].append(inline(" ".join("".join(cell).split())))
    return [row for row in rows if row]


def to_markdown(nfm: str) -> ConvertedPage:
    """Convert the Markdown of ``GET /v1/pages/{id}/markdown`` (v2)."""

    text = clean(nfm).replace("\r\n", "\n").replace("\r", "\n")
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[: max(text.rfind("\n", 0, MAX_CHARS), 0) or MAX_CHARS]
    renderer = _Renderer()
    lines = renderer.items(parse(text).body)
    if truncated:
        lines += ["", TRUNCATED]
    markdown = scrub_text("\n".join(lines)).strip("\n")
    return ConvertedPage(f"{markdown}\n" if markdown else "", tuple(renderer.pages), tuple(renderer.databases))
