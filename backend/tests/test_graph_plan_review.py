"""Review: one advisory judge call on the review model; issues are recorded and change nothing."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from graph.core.errors import LLMError
from graph.plan.answers import ReviewAnswer, ReviewIssueAnswer
from graph.plan.diff import diff
from graph.plan.files import open_plan_store
from graph.plan.inputs import review_messages
from graph.plan.models import PlanOptions, ReviewArtifact
from graph.plan.prompts import gateway_prompt
from graph.plan.review import review
from graph.plan.stitch import stitch
from graph_plan_support import PlannedShop, planned_shop, planning_context, shop_view
from graph_support import ScriptedGateway, outside_untrusted

PLACE = "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"


async def _planned(tmp_path: Path) -> PlannedShop:
    return await planned_shop(await shop_view(), open_plan_store(tmp_path))


async def _review(shop: PlannedShop, tmp_path: Path, gateway: ScriptedGateway, **options: object) -> ReviewArtifact:
    context = planning_context(
        shop.view, open_plan_store(tmp_path / "review"), gateway, options=PlanOptions.model_validate(options)
    )
    return await review(
        context,
        shop.view,
        describe=shop.describe,
        project=shop.project,
        tasks=shop.tasks,
        stitch=stitch(shop.view, shop.project, shop.tasks),
    )


def _answering(answer: object) -> ScriptedGateway:
    return ScriptedGateway().handle(gateway_prompt("review"), lambda _request: answer)


async def test_a_blocking_issue_is_recorded_on_the_review_model_and_changes_nothing(tmp_path: Path) -> None:
    shop = await _planned(tmp_path)
    issue = ReviewIssueAnswer(area_id="AREA::ORDERS_SERVICE", task_id=PLACE, problem="Too large.", severity="blocking")
    gateway = _answering(ReviewAnswer(issues=[issue]))

    artifact = await _review(shop, tmp_path, gateway, review_model="review-model")

    assert [(item.task_id, item.severity) for item in artifact.issues] == [(PLACE, "blocking")]
    assert [(call.call_class, call.model) for call in gateway.calls] == [("judge", "review-model")]
    # The target plan is computed without the review: an issue, even a blocking one, changes nothing.
    assert "review" not in inspect.signature(diff).parameters


async def test_issues_naming_unknown_areas_or_tasks_are_dropped(tmp_path: Path) -> None:
    shop = await _planned(tmp_path)
    answer = ReviewAnswer(
        issues=[
            ReviewIssueAnswer(area_id="AREA::NOWHERE", task_id="", problem="Unknown area.", severity="advisory"),
            ReviewIssueAnswer(area_id="AREA::WEB_SHOP", task_id="TASK::X::Y", problem="Unknown.", severity="advisory"),
            ReviewIssueAnswer(area_id="AREA::WEB_SHOP", task_id="", problem="Fine.", severity="urgent"),
            ReviewIssueAnswer(area_id="AREA::WEB_SHOP", task_id="", problem="Kept.", severity="advisory"),
        ]
    )

    artifact = await _review(shop, tmp_path, _answering(answer))

    assert [item.problem for item in artifact.issues] == ["Kept."]
    assert artifact.outcome.invalid == 1


async def test_a_failed_review_is_recorded_and_never_raises(tmp_path: Path) -> None:
    shop = await _planned(tmp_path)

    artifact = await _review(shop, tmp_path, _answering(LLMError("timeout")))

    assert artifact.failed and artifact.issues == ()


async def test_review_off_makes_no_call(tmp_path: Path) -> None:
    shop = await _planned(tmp_path)
    gateway = ScriptedGateway()

    artifact = await _review(shop, tmp_path, gateway, review=False)

    assert artifact.skipped and gateway.calls == []


async def test_the_review_input_holds_spec_and_planner_text_only_in_untrusted_blocks(tmp_path: Path) -> None:
    shop = await _planned(tmp_path)
    tasks = (*shop.project.setup, *shop.project.closing, *shop.tasks.tasks)
    messages = review_messages(
        shop.view, describe=shop.describe, project=shop.project, tasks=tasks, waits={}, tasks_artifact=shop.tasks
    )

    visible = outside_untrusted(messages[1]["content"])

    texts = {task.title for task in tasks} | {item.text for task in tasks for item in task.done_when}
    texts |= {story.title for story in shop.view.stories.values()}
    assert not [text for text in texts if len(text) > 3 and text in visible]
    assert json.loads(visible[visible.index("{") : visible.rindex("}") + 1])["tasks"]
