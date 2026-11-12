"""The story reads for people (area summary, story lists, preview) and the story board rules, on the in-memory plan of
``plan_claims_support.mini_plan``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from graph.core.errors import ConfigurationError
from graph.plan import PlanReadError, PlanService
from graph.plan.errors import TaskStateError
from graph.plan.reader import LoadedPlan
from graph.plan.snapshot import read_current_plan
from graph.plan.story_board import StoryBoard
from graph_plan_support import plan_ontology, plan_rules
from plan_claims_support import MemoryPlanExecutor, mini_plan, story_row

API, UI, LATE, STUCK, SETUP = ("STORY::A::API", "STORY::A::UI", "STORY::A::LATE", "STORY::A::STUCK", "STORY::P::SETUP")
X, Y = "STORY::A::X", "STORY::A::Y"


def _service(memory: MemoryPlanExecutor | None = None) -> tuple[PlanService, MemoryPlanExecutor]:
    memory = memory or mini_plan()
    ontology = plan_ontology()
    return PlanService(memory, ontology, plan_rules(ontology)), memory


async def _board(memory: MemoryPlanExecutor) -> StoryBoard:
    return StoryBoard(LoadedPlan(await read_current_plan(memory, memory.project_id)))


async def test_the_open_view_lists_ready_stories_first_then_in_progress_waiting_and_blocked() -> None:
    service, _ = _service()

    rows = await service.story_list("shop", caller="u1")

    assert [(row.rank, row.story_id, row.state) for row in rows] == [
        (1, API, "ready"),
        (2, X, "ready"),
        (3, Y, "ready"),
        (4, UI, "waiting"),
        (5, LATE, "waiting"),
        (6, STUCK, "blocked"),
    ]
    by_id = {row.story_id: row for row in rows}
    assert by_id[API].unblocks == 2 and by_id[API].tasks == 2 and by_id[API].release == "R1"
    assert [(item.story_id, item.title) for item in by_id[UI].waits_on] == [(API, "Title of STORY::A::API")]
    assert [item.story_id for item in by_id[LATE].waits_on] == [API, UI]
    assert by_id[STUCK].blocked_by == ("FND::1",) and by_id[X].waits_on == ()
    assert (by_id[X].cycle_with, by_id[Y].cycle_with, by_id[API].cycle_with) == ((Y,), (X,), ())
    assert all(row.pr_url is None and row.commit is None for row in rows)


async def test_unblocks_counts_exactly_the_stories_that_name_the_story_in_waits_on() -> None:
    """One definition for both sides (QA F10): API is waited on by UI directly and by LATE through UI."""

    service, _ = _service()

    rows = await service.story_list("shop", caller="u1")

    for row in rows:
        naming = [other.story_id for other in rows if row.story_id in {ref.story_id for ref in other.waits_on}]
        assert row.unblocks == len(naming), row.story_id
    preview = await service.story_preview("shop", API)
    assert [ref.story_id for ref in preview.unblocks] == [LATE, UI]


async def test_a_story_without_live_tasks_is_empty_not_waiting() -> None:
    """QA F9: a taskless story (or one whose tasks are all closed) has nothing to wait for; it is ``empty``, listed
    after the blocked stories, and a claim refuses it."""

    service, memory = _service()
    memory.stories["STORY::A::HOLLOW"] = story_row("STORY::A::HOLLOW")
    memory.set_task("TASK::A::LATE_ONE", status="superseded")

    rows = await service.story_list("shop", caller="u1")

    by_id = {row.story_id: row for row in rows}
    assert (by_id["STORY::A::HOLLOW"].state, by_id[LATE].state) == ("empty", "empty")
    assert [row.story_id for row in rows][-2:] == [LATE, "STORY::A::HOLLOW"]  # by release, then id
    assert by_id["STORY::A::HOLLOW"].waits_on == () and by_id["STORY::A::HOLLOW"].tasks == 0
    with pytest.raises(TaskStateError) as refused:
        await service.claim_story("shop", "STORY::A::HOLLOW", claimed_by="u1")
    assert refused.value.code == "not_available"


async def test_a_held_story_whose_tasks_were_all_superseded_is_empty_and_stays_in_the_holders_mine_view() -> None:
    """QA F29: ``empty`` is checked before ``in_progress`` (first match wins): the claim holds no work, so the state
    says so; the holder still finds the story under ``mine``, with the claim, and can release it."""

    service, memory = _service()
    token = (await service.claim_story("shop", API, claimed_by="u1")).claim_token
    for task_id in ("TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"):
        memory.set_task(task_id, status="superseded")  # a plan update replaced the story's work

    (row,) = await service.story_list("shop", view="mine", caller="u1")
    released = await service.release_story("shop", API, claimed_by="u1", claim_token=token)

    assert (row.story_id, row.state, row.claimed_by, row.tasks) == (API, "empty", "u1", 0)
    assert released.released == () and await service.story_list("shop", view="mine", caller="u1") == ()


async def test_the_rows_name_their_area_title() -> None:
    """QA F28: a row carries its area's title, so an agent can name the area without list_areas."""

    service, _ = _service()

    rows = await service.story_list("shop", caller="u1", view="done")

    assert [(row.area_id, row.area_title) for row in rows] == [("AREA::P", "Area AREA::P")]


async def test_waits_on_is_worked_out_once_per_claim_unit_for_a_whole_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """QA F29: a list asks every story's ``waits_on`` (its row and ``unblocks``); the board walks each unit once."""

    board = await _board(mini_plan())
    calls: list[tuple[str, ...]] = []
    walk = board._outside_stories

    def counted(unit: tuple[str, ...]) -> tuple[str, ...]:
        calls.append(unit)
        return walk(unit)

    monkeypatch.setattr(board, "_outside_stories", counted)
    first = [board.blocking_stories(story_id) for story_id in board.stories]
    again = [board.blocking_stories(story_id) for story_id in board.stories]

    assert first == again and len(calls) == len(set(calls)) == len({board.unit_of(s) for s in board.stories})


async def test_a_held_story_is_in_progress_and_the_ready_stories_keep_their_order() -> None:
    service, _ = _service()
    await service.claim_story("shop", X, claimed_by="u1", agent_label="builder")

    rows = await service.story_list("shop", caller="u2")

    assert [(row.story_id, row.state) for row in rows][:3] == [(API, "ready"), (X, "in_progress"), (Y, "in_progress")]
    held = rows[1]
    assert (held.claimed_by, held.agent_label) == ("u1", "builder") and held.claimed_at is not None


async def test_the_mine_view_lists_held_stories_and_stories_blocked_by_the_given_findings() -> None:
    service, _ = _service()
    await service.claim_story("shop", X, claimed_by="u1")

    mine = await service.story_list("shop", view="mine", caller="u1", finding_ids=["FND::1"])
    only_held = await service.story_list("shop", view="mine", caller="u1")
    nobody = await service.story_list("shop", view="mine", caller="u9")

    assert [row.story_id for row in mine] == [X, Y, STUCK] and [row.rank for row in mine] == [1, 2, 3]
    assert [row.story_id for row in only_held] == [X, Y] and nobody == ()


async def test_the_done_view_lists_completed_stories_newest_first_with_their_last_completion() -> None:
    service, memory = _service()
    memory.set_task(
        "TASK::A::STUCK_BLOCKED",
        status="completed",
        commit="e" * 40,
        completed_by="bob",
        completed_at="2026-01-03T00:00:00+00:00",
        blocked_by=None,
    )
    memory.set_task(
        "TASK::A::STUCK_OPEN",
        status="completed",
        commit="f" * 40,
        completed_by="cy",
        completed_at="2026-01-04T00:00:00+00:00",
    )
    memory.set_story(STUCK, pr_url="https://git.example.test/p/2")

    rows = await service.story_list("shop", view="done", caller="u1")

    assert [row.story_id for row in rows] == [STUCK, SETUP]
    assert (rows[0].commit, rows[0].completed_by, rows[0].pr_url) == ("f" * 40, "cy", "https://git.example.test/p/2")
    assert rows[0].completed_at == datetime(2026, 1, 4, tzinfo=UTC)
    assert (rows[1].commit, rows[1].completed_by, rows[1].waits_on) == ("c" * 40, "ann", ())
    assert (rows[0].tasks, rows[1].tasks) == (2, 2)  # a completed story counts its completed tasks (QA F7)
    assert rows[1].completed_at == datetime(2026, 1, 1, 12, tzinfo=UTC)


async def test_the_lists_filter_by_area_and_refuse_an_unknown_area_or_view() -> None:
    service, _ = _service()

    in_project = await service.story_list("shop", caller="u1", area_id="AREA::P")
    in_area = await service.story_list("shop", view="done", caller="u1", area_id="AREA::A")

    assert in_project == () and in_area == ()
    with pytest.raises(PlanReadError) as unknown:
        await service.story_list("shop", caller="u1", area_id="AREA::NOPE")
    with pytest.raises(ConfigurationError) as invalid:
        await service.story_list("shop", view="all", caller="u1")
    assert (unknown.value.code, invalid.value.code) == ("area_not_found", "invalid_field")


async def test_the_area_summary_counts_stories_per_state_and_puts_the_project_area_last() -> None:
    service, _ = _service()
    await service.claim_story("shop", X, claimed_by="u1")

    summary = await service.area_summary("shop")

    assert [item.area_id for item in summary] == ["AREA::A", "AREA::P"]
    area, project = summary
    assert area.stories == {"blocked": 1, "completed": 0, "empty": 0, "in_progress": 2, "ready": 1, "waiting": 2}
    assert area.holders == ("u1",) and area.open_tasks == 10 and area.kind == "module"
    assert project.stories == {"blocked": 0, "completed": 1, "empty": 0, "in_progress": 0, "ready": 0, "waiting": 0}
    assert project.holders == () and project.open_tasks == 0 and project.kind == "project"


async def test_a_retired_area_is_left_out_of_the_summary() -> None:
    service, memory = _service()
    memory.areas["AREA::P"]["status"] = "retired"

    assert [item.area_id for item in await service.area_summary("shop")] == ["AREA::A"]


async def test_a_preview_shows_the_unit_the_open_tasks_the_blockers_and_what_it_unblocks() -> None:
    service, _ = _service()

    waiting = await service.story_preview("shop", UI)
    cycle = await service.story_preview("shop", X)

    assert (waiting.state, waiting.unit, waiting.claimed_by) == ("waiting", (UI,), None)
    assert [(task.task_id, task.status, task.ready, task.progress) for task in waiting.tasks] == [
        ("TASK::A::UI_SCREEN", "pending", False, None)
    ]
    assert [(item.story_id, item.state, item.claimed_by) for item in waiting.blockers] == [(API, "ready", None)]
    assert [item.story_id for item in waiting.unblocks] == [LATE]
    assert (cycle.state, cycle.unit, cycle.blockers) == ("ready", (X, Y), ())
    assert (waiting.blocked_by, (await service.story_preview("shop", STUCK)).blocked_by) == ((), ("FND::1",))
    order = [task.task_id for task in cycle.tasks]
    assert len(order) == 4 and order.index("TASK::A::Y_CONTRACT") < order.index("TASK::A::X_LOGIC")
    assert all(task.ready for task in cycle.tasks if task.task_id.endswith("_CONTRACT"))


async def test_a_preview_of_a_held_story_shows_the_claim_but_never_the_token() -> None:
    service, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1", agent_label="builder", branch="feat/api")
    await service.report_progress(
        "shop", "TASK::A::API_CONTRACT", claimed_by="u1", claim_token=claim.claim_token, progress="done"
    )
    await service.release_story("shop", API, claimed_by="u1", claim_token=claim.claim_token, note="paused")
    claim = await service.claim_story("shop", API, claimed_by="u2", agent_label="other")
    await service.report_progress(
        "shop", "TASK::A::API_LOGIC", claimed_by="u2", claim_token=claim.claim_token, progress="skipped", note="n"
    )

    preview = await service.story_preview("shop", API)

    assert (preview.state, preview.claimed_by, preview.agent_label) == ("in_progress", "u2", "other")
    assert (preview.branch, preview.note) == ("feat/api", "paused")
    assert [(task.task_id, task.progress) for task in preview.tasks] == [
        ("TASK::A::API_CONTRACT", None),
        ("TASK::A::API_LOGIC", "skipped"),
    ]
    assert claim.claim_token not in preview.model_dump_json()


@pytest.mark.parametrize("story_id", ["STORY::A::NOPE", "RETIRED"])
async def test_a_preview_of_an_unknown_or_retired_story_is_refused(story_id: str) -> None:
    service, memory = _service()
    memory.set_story(LATE, status="retired")

    with pytest.raises(PlanReadError) as raised:
        await service.story_preview("shop", LATE if story_id == "RETIRED" else story_id)

    assert raised.value.code == "story_not_found"


async def test_every_story_read_of_a_project_without_a_plan_raises_no_plan() -> None:
    service, memory = _service()
    memory.plan = None

    for call in (
        service.area_summary("shop"),
        service.story_list("shop", caller="u1"),
        service.story_preview("shop", API),
    ):
        with pytest.raises(PlanReadError) as raised:
            await call
        assert raised.value.code == "no_plan"


async def test_a_unit_with_a_story_held_by_the_same_user_only_in_part_is_partly_claimed() -> None:
    # A plan run added the cycle after u1 claimed X alone: X is held, Y is not, so Y's unit is claimed in part.
    memory = mini_plan()
    memory.add_waits(("TASK::A::Y_LOGIC", "TASK::A::X_CONTRACT"))
    memory.set_story(X, claimed_by="u1", claimed_at="2026-01-02T00:00:00+00:00", claim_token="t" * 32)
    memory.set_task("TASK::A::X_CONTRACT", status="in_progress", claimed_by="u1", claim_token="t" * 32)

    board = await _board(memory)
    partly = board.verdict(Y, "u1")
    foreign = board.verdict(Y, "u2")

    assert (partly.code, partly.fields["task_id"]) == ("partly_claimed", "TASK::A::X_CONTRACT")
    assert (foreign.code, foreign.fields["claimed_by"]) == ("story_claimed", "u1")
    assert board.state(X) == "in_progress" and board.claimable(Y) is False


async def test_ordered_lists_tasks_on_a_wait_cycle_after_the_rest() -> None:
    # A broken plan (a wait cycle) must not lose tasks: they follow the ordered ones.
    memory = mini_plan()
    memory.add_waits(("TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"))
    board = await _board(memory)

    assert board.ordered(["TASK::A::API_CONTRACT", "TASK::A::API_LOGIC", "TASK::A::X_CONTRACT"]) == (
        "TASK::A::X_CONTRACT",
        "TASK::A::API_CONTRACT",
        "TASK::A::API_LOGIC",
    )


async def test_a_story_that_does_not_exist_cannot_be_claimed_or_ready() -> None:
    board = await _board(mini_plan())

    assert board.verdict("STORY::A::NOPE", "u1").code == "unknown_story"
    assert board.claimable("STORY::A::NOPE") is False and board.alternatives("STORY::A::NOPE") == ()
    assert board.unit_of("STORY::A::NOPE") == ("STORY::A::NOPE",)


async def test_a_story_in_progress_only_through_task_claims_shows_the_oldest_task_holder() -> None:
    # No story claim on API: hotfix-b claimed LOGIC at 10:05, hotfix-a CONTRACT at 10:01; the rows show hotfix-a.
    service, memory = _service()
    memory.set_task(
        "TASK::A::API_LOGIC", status="in_progress", claimed_by="hotfix-b", claimed_at="2026-01-02T10:05:00+00:00"
    )
    memory.set_task(
        "TASK::A::API_CONTRACT", status="in_progress", claimed_by="hotfix-a", claimed_at="2026-01-02T10:01:00+00:00"
    )

    rows = {row.story_id: row for row in await service.story_list("shop", view="open", caller="u1")}
    preview = await service.story_preview("shop", API)
    (area,) = [item for item in await service.area_summary("shop") if item.area_id == "AREA::A"]

    since = datetime(2026, 1, 2, 10, 1, tzinfo=UTC)
    assert (rows[API].state, rows[API].claimed_by, rows[API].claimed_at) == ("in_progress", "hotfix-a", since)
    assert (preview.claimed_by, preview.claimed_at, preview.agent_label) == ("hotfix-a", since, None)
    assert area.holders == ("hotfix-a",)
    assert (rows[UI].claimed_by, rows[UI].claimed_at) == (None, None)
    assert [blocker.claimed_by for blocker in (await service.story_preview("shop", UI)).blockers] == ["hotfix-a"]
