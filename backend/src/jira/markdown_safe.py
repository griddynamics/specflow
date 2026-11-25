"""What the two converters to Markdown (``jira.adf_reader``, ``jira.storage_markdown``) share to keep the Markdown they
write safe whatever the page says: links with a safe scheme and an encoded address, a code fence longer than any
backtick run in the code, and the one error for a document too deep to convert."""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

SAFE_SCHEMES: Final = frozenset({"http", "https", "mailto"})
_LANGUAGE_OUT: Final = re.compile(r"[^\w+#.-]")
_UNSAFE: Final = re.compile(r"[\x00-\x20\x7f()]")
_BACKTICKS: Final = re.compile(r"`+")


class MarkupTooDeepError(Exception):
    """A page or an issue nests deeper than the converter can walk; its item is an error, the import goes on."""


def safe_href(href: str) -> str | None:
    """``href`` with spaces, parentheses and control characters percent-encoded, or ``None`` unless its scheme is
    ``http``, ``https`` or ``mailto``."""

    href = href.strip()
    try:
        scheme = urlsplit(href).scheme.lower()
    except ValueError:
        return None
    if scheme not in SAFE_SCHEMES:
        return None
    return _UNSAFE.sub(lambda found: f"%{ord(found.group()):02X}", href)


def link(text: str, href: str) -> str:
    """``[text](href)`` when the address is safe, else the text alone; ``]`` and ``[`` in the text are escaped."""

    safe = safe_href(href)
    if safe is None or not text:
        return text
    return f"[{text.replace('[', chr(92) + '[').replace(']', chr(92) + ']')}]({safe})"


def fence(code: str, language: str) -> str:
    """A fenced block whose fence is longer than the longest backtick run in ``code``."""

    longest = max((len(run) for run in _BACKTICKS.findall(code)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{_LANGUAGE_OUT.sub('', language)}\n{code.strip(chr(10))}\n{ticks}"
