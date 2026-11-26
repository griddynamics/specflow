"""E-mail removal (N5): the original the import stores and the GFM both carry no e-mail address and no user id.

Notion puts ``user://<id>/<e-mail>`` in every user mention, even for a connection without the e-mail capability. The
scan for addresses walks the ``@`` signs with bounded steps, so a hostile page cannot make it quadratic. Code spans and
fenced code are left alone."""

from __future__ import annotations

import re
from typing import Final

HIDDEN: Final = "(e-mail hidden)"
_NOT_LOCAL: Final = frozenset('<>()[]{}"`,;:/\\|@')
_LOCAL_MAX: Final = 256
_DOMAIN_MAX: Final = 255
_USER_ID: Final = re.compile(r"""user://[^\s"'<>)\]]*""", re.IGNORECASE)


def _stops(char: str) -> bool:
    return char.isspace() or char in _NOT_LOCAL


def strip_user_ids(text: str, keep: str = "") -> str:
    """``text`` with every ``user://<id>...`` (any case, in any tag, link or code) cut to ``keep``."""

    return _USER_ID.sub(keep, text) if "://" in text else text


def scrub_text(text: str) -> str:
    """``text`` without user ids and e-mail addresses (the final step of every converter output)."""

    return scrub_emails(strip_user_ids(text))


def _prose_emails(text: str) -> str:
    """``scrub_emails`` for text with no code in it."""

    at = text.find("@")
    if at < 0:
        return text
    parts: list[str] = []
    done = 0
    while at >= 0:
        start = at
        while start > done and at - start < _LOCAL_MAX and not _stops(text[start - 1]):
            start -= 1
        stop = at + 1
        while stop < len(text) and stop - at <= _DOMAIN_MAX and (text[stop].isalnum() or text[stop] in ".-"):
            stop += 1
        domain = text[at + 1 : stop].rstrip(".-")
        labels = domain.split(".")
        # an address ends in an alphabetic label (``foo@1.2.3`` is a version, not an address)
        if start < at and len(labels) > 1 and all(labels) and labels[-1].isalpha():
            parts.append(text[done:start])
            parts.append(HIDDEN)
            done = at + 1 + len(domain)
            at = text.find("@", done)
        else:
            at = text.find("@", at + 1)
    parts.append(text[done:])
    return "".join(parts)


def _code_spans(line: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of each inline code span of a line: a run of backticks up to the next run of the same
    length. A run with no closer is text. One scan per line; a length that found no closer is not searched again."""

    spans: list[tuple[int, int]] = []
    unclosed: set[int] = set()
    pos = 0
    while (open_at := line.find("`", pos)) >= 0:
        run_end = open_at
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        size = run_end - open_at
        close = -1
        if size not in unclosed:
            scan = run_end
            while (found := line.find("`", scan)) >= 0:
                end = found
                while end < len(line) and line[end] == "`":
                    end += 1
                if end - found == size:
                    close = end
                    break
                scan = end
            if close < 0:
                unclosed.add(size)
        if close < 0:
            pos = run_end
        else:
            spans.append((open_at, close))
            pos = close
    return spans


def _line_emails(line: str) -> str:
    if "@" not in line:
        return line
    parts: list[str] = []
    done = 0
    for start, end in _code_spans(line):
        parts.append(_prose_emails(line[done:start]))
        parts.append(line[start:end])
        done = end
    parts.append(_prose_emails(line[done:]))
    return "".join(parts)


def scrub_emails(text: str) -> str:
    """``text`` with every e-mail address replaced by "(e-mail hidden)"; code spans and fenced code are left as they
    are (``npm i foo@1.2.3``, ``git@github.com:org/repo``)."""

    if "@" not in text:
        return text
    out: list[str] = []
    fence = ""  # the opening fence (3+ backticks or tildes) while inside fenced code
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        marker = stripped[:3] if stripped[:3] in ("```", "~~~") else ""
        if fence:
            out.append(line)
            if marker == fence and not stripped.lstrip(fence[0]).strip():
                fence = ""
        elif marker:
            fence = marker
            out.append(line)
        else:
            out.append(_line_emails(line))
    return "".join(out)


def scrub_original(nfm: str) -> str:
    """The Markdown with the user id and e-mail removed from every ``user://<id>/<email>`` (``user://`` is left), and
    every e-mail removed from any other text, for the original that the import stores (N5)."""

    return scrub_emails(strip_user_ids(nfm, "user://"))
