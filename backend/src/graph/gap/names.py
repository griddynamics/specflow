"""Names and limits of the findings slice of ``graph.gap`` ([gap.md](gap.md))."""

from __future__ import annotations

from typing import Final, Literal

FINDING: Final = "Finding"
EXTERNAL_REF: Final = "ExternalRef"
ABOUT: Final = "ABOUT"
REFERS_TO: Final = "REFERS_TO"
OWNER: Final = "gap"
AGENT: Final = "agent"
FINDING_PREFIX: Final = "FND::"

FindingKind = Literal["gap", "conflict", "question", "note"]
FindingStatus = Literal["open", "resolved", "dismissed"]
RefType = Literal["pr", "commit", "file", "url"]

KINDS: Final[tuple[FindingKind, ...]] = ("gap", "conflict", "question", "note")
REF_TYPES: Final[tuple[RefType, ...]] = ("pr", "commit", "file", "url")
STATUSES: Final[tuple[FindingStatus, ...]] = ("open", "resolved", "dismissed")

# The kind of a blocking finding to the block reason the plan stores ("Blocking link"); ``note`` never blocks.
BLOCK_REASONS: Final[dict[str, str]] = {
    "question": "spec_ambiguous",
    "conflict": "spec_contradiction",
    "gap": "spec_missing",
}

TEXT_LIMIT: Final = 2000
ABOUT_LIMIT: Final = 20
REFS_LIMIT: Final = 10
OPEN_LIMIT: Final = 50
NOTE_LIMIT: Final = 500
AUTHOR_LIMIT: Final = 200
STORY_LIMIT: Final = 200
FILE_LIMIT: Final = 500
URL_LIMIT: Final = 2000
PAGE_LIMITS: Final = (1, 200)
