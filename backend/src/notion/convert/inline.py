"""Inline NFM to GFM: ``<b> <i> <s> <u> <code> <span> <mention> <date> <br>``, images and escaped tags in one line.

One left-to-right scan with a small stack, no HTML parser and no nested regular expressions. Backtick code spans are
copied as they are. A tag the converter does not know is made text (``&lt;``), a tag of the NFM vocabulary in the wrong
place is dropped, so no NFM tag survives in the output."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Final

from drive.convert._md import link_target
from notion.convert.result import MarkupTooDeepError

MAX_DEPTH: Final = 32

# Tags whose line-level meaning is handled by ``tree``; found inside a line they are dropped.
BLOCK_TAGS: Final = frozenset(
    {
        "callout",
        "toggle",
        "columns",
        "column",
        "tabs",
        "tab",
        "synced-block",
        "synced-block-reference",
        "quote",
        "bookmark",
        "embed",
        "pdf",
        "file",
        "audio",
        "video",
        "image",
        "page",
        "database",
        "meeting-notes",
        "summary",
        "notes",
        "transcript",
        "custom-block",
        "table",
        "table-of-contents",
        "p",
        "link-preview",
        "empty-block",
        "tr",
        "td",
        "th",
        "colgroup",
        "col",
    }
)
_MARKS: Final = {"b": "**", "strong": "**", "i": "*", "em": "*", "s": "~~", "del": "~~", "strike": "~~"}
_PLAIN: Final = frozenset({"span", "u", "mark", "sup", "sub", "small", "font", "color", "ins"})

ATTR: Final = re.compile(r"""([A-Za-z_][\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_TOKEN: Final = re.compile(
    r"\\<"
    r"|(?P<tick>`+)"
    r"|!\[(?P<alt>[^\[\]\n]*)\]\([^()\n]*\)"
    r"|<[!?]"
    r"|<[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>]*>"
    r"|<(?P<slash>/?)(?P<name>[A-Za-z][\w-]*)(?=[\s/>])(?P<attrs>[^<>]*?)(?P<self>/?)>"
)


def attrs_of(text: str) -> dict[str, str]:
    """The ``name="value"`` pairs of a tag (names lower case, values unescaped)."""

    return {
        m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
        for m in ATTR.finditer(text)
    }


@dataclass
class _Open:
    name: str
    index: int  # where its text starts in ``out``
    mark: str = ""
    tail: str = ""  # what closes it (the link of a page mention)
    url: str = ""


def _date(attrs: dict[str, str]) -> str:
    start, end = attrs.get("start", ""), attrs.get("end", "")
    return f"{start} to {end}" if start and end else start or end


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.out: list[str] = []
        self.stack: list[_Open] = []
        self.dead_ticks: set[int] = set()
        self.code_closed = True  # false once no ``</code>`` is left in the text

    def run(self) -> str:
        text, pos = self.text, 0
        while (m := _TOKEN.search(text, pos)) is not None:
            self.out.append(text[pos : m.start()])
            pos = m.end()
            pos = self._token(m, pos)
        self.out.append(text[pos:])
        while self.stack:
            self._close(self.stack[-1].name)
        return "".join(self.out)

    def _token(self, m: re.Match[str], pos: int) -> int:
        found = m.group(0)
        if found == "\\<":
            self.out.append("&lt;")
        elif m.group("tick"):
            return self._ticks(m, pos)
        elif m.group("alt") is not None:
            self.out.append(f"Image: {m.group('alt').strip()}" if m.group("alt").strip() else "")
        elif found[1] in "!?":
            self.out.append("&lt;" + found[1])
        elif m.group("name") is None:
            # an autolink is kept only when it is a link the converter would write
            self.out.append(found if link_target(found[1:-1]) else "&lt;" + found[1:])
        else:
            return self._tag(m, pos)
        return pos

    def _ticks(self, m: re.Match[str], pos: int) -> int:
        run = m.group("tick")
        close = self._find_run(len(run), pos)
        if close < 0:
            self.out.append(run)
            return pos
        self.out.append(self.text[m.start() : close + len(run)])
        return close + len(run)

    def _find_run(self, size: int, start: int) -> int:
        if size in self.dead_ticks:
            return -1
        text, wanted = self.text, "`" * size
        at = text.find(wanted, start)
        while at >= 0:
            after = at + size
            if (at == start or text[at - 1] != "`") and (after >= len(text) or text[after] != "`"):
                return at
            at = text.find(wanted, at + 1)
        self.dead_ticks.add(size)
        return -1

    def _tag(self, m: re.Match[str], pos: int) -> int:
        name = m.group("name").lower()
        closing, selfclosing = bool(m.group("slash")), bool(m.group("self"))
        attrs = m.group("attrs")
        if name in _MARKS:
            if closing:
                self._close(_MARKS[name])
            elif not selfclosing:
                self._open(_Open(_MARKS[name], len(self.out), mark=_MARKS[name]), unique=True)
        elif name == "code":
            return self._code(pos) if not closing and not selfclosing else pos
        elif name == "mention":
            self._mention(attrs_of(attrs), closing=closing, selfclosing=selfclosing)
        elif name == "date":
            if not closing:
                self.out.append(_date(attrs_of(attrs)))
        elif name == "br":
            self.out.append(" ")
        elif not (name in _PLAIN or name in BLOCK_TAGS):
            self.out.append("&lt;" + m.group(0)[1:])
        return pos

    def _code(self, pos: int) -> int:
        close = self.text.find("</code>", pos) if self.code_closed else -1
        if close < 0:
            self.code_closed = False
            return pos
        body = self.text[pos:close].replace("\\<", "<")
        ticks = max((len(r) for r in re.findall(r"`+", body)), default=0)
        fence = "`" * (ticks + 1)
        pad = " " if body.startswith("`") or body.endswith("`") else ""
        self.out.append(f"{fence}{pad}{body}{pad}{fence}" if body else "")
        return close + len("</code>")

    def _mention(self, attrs: dict[str, str], *, closing: bool, selfclosing: bool) -> None:
        url = attrs.get("url", "")
        user = url.startswith("user://")
        target = None if user else link_target(url)
        if selfclosing:
            self.out.append("a user" if user else f"[{target}]({target})" if target else "")
        elif closing:
            self._close("mention")
        else:
            self._open(
                _Open("mention", len(self.out), "[" if target else "", f"]({target})" if target else "", target or "")
            )

    def _open(self, entry: _Open, *, unique: bool = False) -> None:
        if len(self.stack) >= MAX_DEPTH:
            raise MarkupTooDeepError("inline markup is nested too deeply")
        if unique and any(o.name == entry.name for o in self.stack):
            entry = _Open(entry.name, entry.index)  # the same mark again changes nothing
        self.stack.append(entry)
        self.out.append(entry.mark)

    def _close(self, name: str) -> None:
        """Close the innermost open ``name`` and everything opened after it; a close without an open is dropped."""

        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth].name == name:
                break
        else:
            return
        while len(self.stack) > depth:
            self._wrap(self.stack.pop())

    def _wrap(self, entry: _Open) -> None:
        body = "".join(self.out[entry.index + 1 :])
        del self.out[entry.index :]
        text = body.strip()
        if entry.url and not text:
            text = entry.url
        if not text:
            self.out.append(body)
            return
        lead, trail = body[: len(body) - len(body.lstrip())], body[len(body.rstrip()) :]
        self.out.append(f"{lead}{entry.mark}{text}{entry.tail or entry.mark}{trail}")


def inline(text: str) -> str:
    """One line of NFM as GFM."""

    return _Scanner(text).run() if "<" in text or "`" in text or "![" in text else text
