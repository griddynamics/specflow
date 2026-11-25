"""E-mail removal (N5): the original the import stores and the GFM both carry no e-mail address and no user id.

Notion puts ``user://<id>/<e-mail>`` in every user mention, even for a connection without the e-mail capability. The
scan for addresses walks the ``@`` signs with bounded steps, so a hostile page cannot make it quadratic."""

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


def scrub_emails(text: str) -> str:
    """``text`` with every e-mail address replaced by "(e-mail hidden)"."""

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
    """The Markdown with the user id and e-mail removed from every ``user://<id>/<email>`` (``user://`` is left), and
    every e-mail removed from any other text, for the original that the import stores (N5)."""

    return scrub_emails(strip_user_ids(nfm, "user://"))
