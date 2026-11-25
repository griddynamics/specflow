"""E-mail removal (N5): the original the import stores and the GFM both carry no e-mail address and no user id.

Notion puts ``user://<id>/<e-mail>`` in every user mention, even for a connection without the e-mail capability. The
scan for addresses walks the ``@`` signs with bounded steps, so a hostile page cannot make it quadratic."""

from __future__ import annotations

import re
import string
from typing import Final

HIDDEN: Final = "(e-mail hidden)"
_LOCAL: Final = frozenset(string.ascii_letters + string.digits + "._%+-")
_DOMAIN: Final = frozenset(string.ascii_letters + string.digits + ".-")
_LOCAL_MAX: Final = 64
_DOMAIN_MAX: Final = 255
_USER_URL: Final = re.compile(r"""(url\s*=\s*)(["'])user://.*?\2""")


def scrub_emails(text: str) -> str:
    """``text`` with every e-mail address replaced by "(e-mail hidden)"."""

    at = text.find("@")
    if at < 0:
        return text
    parts: list[str] = []
    done = 0
    while at >= 0:
        start = at
        while start > done and at - start < _LOCAL_MAX and text[start - 1] in _LOCAL:
            start -= 1
        stop = at + 1
        while stop < len(text) and stop - at <= _DOMAIN_MAX and text[stop] in _DOMAIN:
            stop += 1
        domain = text[at + 1 : stop].rstrip(".-")
        labels = domain.split(".")
        if start < at and len(labels) > 1 and all(labels):
            parts.append(text[done:start])
            parts.append(HIDDEN)
            done = at + 1 + len(domain)
            at = text.find("@", done)
        else:
            at = text.find("@", at + 1)
    parts.append(text[done:])
    return "".join(parts)


def scrub_original(nfm: str) -> str:
    """The Markdown with the user id and e-mail removed from ``user://<id>/<email>`` mentions (``user://`` is left), and
    every e-mail removed from any other text, for the original that the import stores (N5)."""

    return scrub_emails(_USER_URL.sub(lambda m: f"{m.group(1)}{m.group(2)}user://{m.group(2)}", nfm))
