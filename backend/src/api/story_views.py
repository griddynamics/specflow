"""The contract views of the people tools and the story writes (D25): ``graph.plan`` rows in, ``agent_contracts``
models out. Holders are user ids in the library and names here (``agent_views.Holders``); no id, email or claim token
leaves in a view except the caller's own token in ``claim_view``. Titles and finding text are data."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from agent_contracts import (
    MAX_ALTERNATIVES,
    MAX_BLOCKERS,
    MAX_WAITS_ON,
    QUESTION_CHARS,
    AreaInProgress,
    AreaItem,
    AreaLink,
    ErrorDetails,
    FindingRef,
    StoryClaim,
    StoryHeader,
    StoryItem,
    StoryLink,
    StoryPreview,
    StoryRefItem,
    StoryRelease,
    StoryTaskItem,
)
from api.agent_views import Holders, finding_items
from api.errors import invalid_cursor
from graph.gap import Finding
from graph.plan.names import StoryView
from graph.plan.story_board import STATE_ORDER
from graph.plan.story_findings import StoryFindings
from graph.plan.story_models import AreaSummary, StoryBlocker, StoryRow
from graph.plan.story_models import StoryClaim as LibraryClaim
from graph.plan.story_models import StoryPreview as Preview
from graph.plan.story_models import StoryRelease as LibraryRelease

AREA_HOLDERS: Final = 3
CURSOR_VERSION: Final = 1


def _cut(text: str, limit: int = QUESTION_CHARS) -> str:
    return text[:limit]


# --- list_areas -----------------------------------------------------------------------------------------------


def area_holder_ids(summaries: Iterable[AreaSummary], rows: Iterable[StoryRow]) -> dict[str, list[str]]:
    """Per area, its holders' user ids: distinct, oldest claim first, at most 3."""

    claimed: dict[str, dict[str, float]] = {}
    for row in rows:
        if row.claimed_by is not None and row.claimed_at is not None:
            seen = claimed.setdefault(row.area_id, {})
            stamp = row.claimed_at.timestamp()
            seen[row.claimed_by] = min(stamp, seen.get(row.claimed_by, stamp))
    return {
        summary.area_id: sorted(claimed.get(summary.area_id, {}), key=lambda who: (claimed[summary.area_id][who], who))[
            :AREA_HOLDERS
        ]
        for summary in summaries
    }


def area_items(
    summaries: Sequence[AreaSummary], holder_ids: Mapping[str, Sequence[str]], holders: Holders
) -> list[AreaItem]:
    items = []
    for summary in summaries:
        shown = [holder for who in holder_ids.get(summary.area_id, ()) if (holder := holders.of(who)) is not None]
        count = summary.stories
        items.append(
            AreaItem(
                area_id=summary.area_id,
                title=summary.title,
                kind=summary.kind,
                ready=count["ready"],
                in_progress=AreaInProgress(count=count["in_progress"], holders=shown),
                waiting=count["waiting"],
                blocked=count["blocked"],
                empty=count["empty"],
                done=count["completed"],
                open_tasks=summary.open_tasks,
            )
        )
    return items


# --- list_stories ---------------------------------------------------------------------------------------------


def ordered_rows(rows: Sequence[StoryRow], view: StoryView, caller: str) -> list[StoryRow]:
    """The view's order of tools.md: ``open`` ready first (the library's order), the other states by ``story_id``;
    ``mine`` the held stories, then the blocked ones, each by ``story_id``; ``done`` as the library has it."""

    if view == "done":
        return list(rows)
    if view == "mine":
        return sorted(rows, key=lambda row: (row.claimed_by != caller, row.story_id))
    ready = [row for row in rows if row.state == "ready"]
    rest = sorted(
        (row for row in rows if row.state != "ready"), key=lambda row: (STATE_ORDER.index(row.state), row.story_id)
    )
    return [*ready, *rest]


def story_row_user_ids(rows: Iterable[StoryRow]) -> list[str | None]:
    return [who for row in rows for who in (row.claimed_by, row.completed_by)]


def story_items(
    rows: Sequence[StoryRow], first_rank: int, findings: Mapping[str, StoryFindings], holders: Holders
) -> list[StoryItem]:
    """``rows`` as items ranked from ``first_rank`` (the position in the view, continuing across pages)."""

    items = []
    for rank, row in enumerate(rows, start=first_rank):
        found = findings.get(row.story_id)
        done = row.state == "completed"
        items.append(
            StoryItem(
                rank=rank,
                story_id=row.story_id,
                title=row.title,
                area=AreaLink(area_id=row.area_id, title=row.area_title),
                kind=row.kind,
                state=row.state,
                holder=holders.of(row.claimed_by, row.agent_label),
                waits_on=[StoryLink(story_id=item.story_id, title=item.title) for item in row.waits_on[:MAX_WAITS_ON]],
                waits_on_total=len(row.waits_on),
                cycle_with=list(row.cycle_with),
                tasks=row.tasks,
                unblocks=row.unblocks,
                findings=found.count if found else 0,
                question=_cut(found.question.text) if found and found.question else None,
                pr_url=row.pr_url if done else None,
                merge_commit=row.commit if done else None,
                completed_by=holders.of(row.completed_by) if done else None,
                completed_at=row.completed_at if done else None,
            )
        )
    return items


def encode_story_cursor(project_id: str, area_id: str | None, view: str, after: int) -> str:
    """Opaque cursor: base64url without padding of the project, area, view and the rank the page ended at."""

    raw = json.dumps(
        {"v": CURSOR_VERSION, "project": project_id, "area": area_id, "view": view, "after": after},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def decode_story_cursor(cursor: str, project_id: str, area_id: str | None, view: str) -> int:
    """The rank the cursor holds, else ``invalid_cursor`` (not canonical, or of another project, area or view)."""

    try:
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        after = data["after"]
        canonical = isinstance(after, int) and not isinstance(after, bool) and after >= 0
        canonical = canonical and encode_story_cursor(project_id, area_id, view, after) == cursor
    except (ValueError, TypeError, KeyError, binascii.Error):  # a lone surrogate fails the re-encode
        raise invalid_cursor() from None
    if not canonical:
        raise invalid_cursor()
    return int(after)


# --- preview_story --------------------------------------------------------------------------------------------


def blocker_item(blocker: StoryBlocker, holders: Holders) -> StoryRefItem:
    return StoryRefItem(
        story_id=blocker.story_id, title=blocker.title, state=blocker.state, holder=holders.of(blocker.claimed_by)
    )


def preview_user_ids(preview: Preview) -> list[str | None]:
    return [
        preview.claimed_by,
        *(blocker.claimed_by for blocker in preview.blockers),
        *(finding.created_by for finding in preview.findings),
    ]


def preview_view(project_id: str, preview: Preview, holders: Holders) -> StoryPreview:
    return StoryPreview(
        project_id=project_id,
        story=StoryHeader(
            story_id=preview.story_id,
            title=preview.title,
            kind=preview.kind,
            area_id=preview.area_id,
            summary=preview.summary,
            release=preview.release,
            state=preview.state,
            holder=holders.of(preview.claimed_by, preview.agent_label),
            since=preview.claimed_at if preview.claimed_by else None,
            branch=preview.branch,
            note=preview.note,
        ),
        story_ids=list(preview.unit),
        items=[
            StoryTaskItem(
                task_id=task.task_id,
                title=task.title,
                status=task.status,
                ready=task.ready,
                complexity=task.complexity,
                progress=task.progress,
            )
            for task in preview.tasks
        ],
        blockers=[blocker_item(blocker, holders) for blocker in preview.blockers],
        findings=finding_items(preview.findings, holders),
        unblocks=[ref.story_id for ref in preview.unblocks],
    )


# --- claim_story, release_story -------------------------------------------------------------------------------


def claim_view(project_id: str, story_id: str, claim: LibraryClaim) -> StoryClaim:
    return StoryClaim(
        project_id=project_id,
        story_id=story_id,
        claim_token=claim.claim_token,
        story_ids=list(claim.story_ids),
        claimed_at=claim.claimed_at,
        agent_label=claim.agent_label,
        branch=claim.branch,
        repeat=claim.repeat,
        items=[
            StoryTaskItem(
                task_id=task.task_id,
                title=task.title,
                status=task.status,
                ready=task.ready,
                complexity=task.complexity,
                progress=task.progress,
            )
            for task in claim.tasks
        ],
    )


def release_view(project_id: str, story_id: str, released: LibraryRelease) -> StoryRelease:
    return StoryRelease(project_id=project_id, story_id=story_id, released=list(released.released))


# --- error details --------------------------------------------------------------------------------------------


def details_dict(details: ErrorDetails) -> dict[str, object]:
    """The envelope's ``details``: the fields that are set, JSON-ready (a holder keeps its ``name: null``)."""

    return {key: value for key, value in details.model_dump(mode="json").items() if value is not None}


def story_ref(row: StoryRow, holders: Holders) -> StoryRefItem:
    return StoryRefItem(
        story_id=row.story_id, title=row.title, state=row.state, holder=holders.of(row.claimed_by, row.agent_label)
    )


def capped_alternatives(rows: Sequence[StoryRow], holders: Holders) -> list[StoryRefItem]:
    return [story_ref(row, holders) for row in rows[:MAX_ALTERNATIVES]]


def capped_blockers(blockers: Sequence[StoryBlocker], holders: Holders) -> list[StoryRefItem] | None:
    """The first blockers, or ``None`` when there are none: an error leaves out what it has nothing to say about."""

    return [blocker_item(blocker, holders) for blocker in blockers[:MAX_BLOCKERS]] or None


def finding_refs(findings: Iterable[Finding | None]) -> list[FindingRef] | None:
    """The open blocking findings an error names, with the text cut like a story row's ``question`` (any member of
    the project reads it there and in preview_story); a resolved or dismissed one is left out. ``None`` for none."""

    return [
        FindingRef(finding_id=item.finding_id, kind=item.kind, text=_cut(item.text))
        for item in findings
        if item is not None and item.status == "open"
    ][:MAX_BLOCKERS] or None
