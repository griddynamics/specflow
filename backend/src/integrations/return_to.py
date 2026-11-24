"""The ``return_to`` rule of the connect flows: a path of this site (docs/jira-sync/oauth.md "Connect flow").

The web app checks the same rule in its callback route handlers (``isSameSitePath``).
"""

from __future__ import annotations

from typing import Final

RETURN_TO_MAX: Final = 2048


def _dot_segment_or_encoded_separator(value: str) -> bool:
    """Whether the path part of ``value`` has a ``.`` or ``..`` segment (``%2e`` counts as ``.``) or an encoded ``/`` or
    ``\\``: a URL parser resolves ``/.//evil.com`` to ``//evil.com``, another host."""
    path = value.split("?", 1)[0].split("#", 1)[0].lower()
    if "%2f" in path or "%5c" in path:
        return True
    return any(segment.replace("%2e", ".") in {".", ".."} for segment in path.split("/"))


def is_same_site_path(value: str) -> bool:
    """A same-origin path: ``/``, not ``//`` (so no scheme or host); no backslash, which a browser reads as ``/``; ASCII
    and printable with no whitespace; no dot segment or encoded separator, which a URL parser can turn into ``//``."""

    return (
        0 < len(value) <= RETURN_TO_MAX
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and value.isascii()
        and value.isprintable()
        and not any(c.isspace() for c in value)
        and not _dot_segment_or_encoded_separator(value)
    )
