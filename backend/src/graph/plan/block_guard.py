"""The guard of a blocking finding: the caller holds the story under the token, and a node the finding is about is
implemented by a task of the story ([gap.md](gap.md) "Blocking link", step 2)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from graph.plan.changes import resolve_spec_id
from graph.plan.claim_support import same_token
from graph.plan.errors import TaskStateError
from graph.plan.invariants import CLOSED
from graph.plan.reader import LoadedPlan


def check_block(
    loaded: LoadedPlan,
    aliases: Mapping[str, str],
    story_id: str,
    *,
    claimed_by: str,
    claim_token: str,
    spec_ids: Iterable[str],
) -> None:
    """Raise ``TaskStateError("claim_mismatch")`` unless ``claimed_by`` holds the active story with ``claim_token``
    and a node of ``spec_ids`` (merged ids resolved) is implemented by one of its open or completed tasks. An unknown
    story, a story nobody holds and a wrong token are all the same refusal: the answer never says which."""

    story = next((item for item in loaded.current.stories if item.story_id == story_id), None)
    held = (
        story is not None
        and story.status == "active"
        and story.claimed_by == claimed_by
        and same_token(story.claim_token, claim_token)
    )
    wanted = {resolve_spec_id(spec_id, aliases) for spec_id in spec_ids}
    implemented = {
        resolve_spec_id(spec_id, aliases)
        for task in loaded.story_tasks(story_id)
        if task.status not in CLOSED
        for spec_id in task.implements
    }
    if not held or not wanted & implemented:
        raise TaskStateError("claim_mismatch")
