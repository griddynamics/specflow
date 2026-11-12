"""Argument checks of the lifecycle and story calls; each raises ``ConfigurationError`` before any query, except
``check_done``, which needs the claimed tasks of the locked plan and so runs before the first write."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Final, get_args
from urllib.parse import urlsplit

from graph.core.errors import ConfigurationError
from graph.plan.names import BlockReason, TaskProgress

COMMIT_PATTERN: Final = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
NOTE_LIMIT: Final = 500
CLAIMANT_LIMIT: Final = 200
LABEL_LIMIT: Final = 80
BRANCH_LIMIT: Final = 200
URL_LIMIT: Final = 2000
STORY_BLOCK_REASONS: Final[tuple[BlockReason, ...]] = ("spec_ambiguous", "spec_contradiction", "spec_missing")
_URL_SCHEMES: Final = frozenset({"http", "https"})


def check_claimant(value: str) -> None:
    """A user id or claimant: 1-200 printable characters (``invalid_claimant``)."""

    if not value or len(value) > CLAIMANT_LIMIT or not value.isprintable():
        raise ConfigurationError("invalid_claimant")


def note_text(note: str) -> str | None:
    """A note of at most 500 characters; an empty one is none (``invalid_note``)."""

    if len(note) > NOTE_LIMIT:
        raise ConfigurationError("invalid_note")
    return note or None


def check_note(note: str | None) -> None:
    if note is not None:
        note_text(note)


def commit_text(commit: str) -> str:
    """A 40 or 64 hex commit in lower case (``invalid_commit``)."""

    if not COMMIT_PATTERN.fullmatch(commit):
        raise ConfigurationError("invalid_commit")
    return commit.lower()


def check_printable(value: str | None, limit: int) -> None:
    """A display field of 1 to ``limit`` printable characters (``invalid_field``); none passes."""

    if value is not None and (not value or len(value) > limit or not value.isprintable()):
        raise ConfigurationError("invalid_field")


def check_label(label: str | None) -> None:
    check_printable(label, LABEL_LIMIT)


def check_branch(branch: str | None) -> None:
    check_printable(branch, BRANCH_LIMIT)


def check_pr_url(url: str | None) -> None:
    """An ``http`` or ``https`` URL with a host, without user info, of at most 2000 characters (``invalid_field``)."""

    if url is None:
        return
    check_printable(url, URL_LIMIT)
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as error:
        raise ConfigurationError("invalid_field") from error
    if parts.scheme not in _URL_SCHEMES or not host or "@" in parts.netloc or " " in url:
        raise ConfigurationError("invalid_field")


def check_progress(progress: str) -> None:
    if progress not in get_args(TaskProgress):
        raise ConfigurationError("invalid_field")


def check_story_reason(reason: str) -> None:
    """A reason a story claim may be blocked for (``invalid_reason``)."""

    if reason not in STORY_BLOCK_REASONS:
        raise ConfigurationError("invalid_reason")


def check_done(done: Collection[str] | None, claimed: Collection[str]) -> None:
    """``done`` names claimed tasks only (``invalid_done``)."""

    if done is not None and not set(done) <= set(claimed):
        raise ConfigurationError("invalid_done")
