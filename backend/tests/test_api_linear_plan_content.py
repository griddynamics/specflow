"""The Linear content of a plan item (plans/LINEAR/LINEAR-PLAN.md section 3): the canonical content the pass stores and
hashes, the Markdown description, the estimate and labels, the comment that lists a change; and the label and milestone
scaffold the issues hang on."""

from __future__ import annotations

import uuid
from contextlib import suppress
from typing import Any

from api.exports.digest import digest
from api.jira.desired import SpecRef, build
from api.linear_plan import content as rules
from api.linear_plan.scaffold import Scaffold
from jira_sync_support import make_plan, story, task
from linear.errors import LinearAlreadyExistsError
from linear.fake import FakeLinear
from linear.ids import KIND_LABEL, KIND_MILESTONE, node_uuid
from linear.models import Label


def items(**plan: Any) -> dict[str, Any]:
    stories = [story("STORY::A::ONE", release="R1")]
    tasks = [
        task(
            "TASK::A::1_R2",
            description="Do the thing.",
            phase="logic",
            complexity="high",
            done_when=({"text": "it works", "source": "planner"}, {"text": "  ", "source": "planner"}),
            implements=("FEAT::X",),
        )
    ]
    loaded = make_plan(stories, tasks, implements=[("STORY::A::ONE", "FEAT::X")])
    return build(loaded, project_name="", epic_key="-", rows={}, names={"FEAT::X": SpecRef("FEAT::X", "Pay")}).items


def test_the_canonical_content_swaps_the_shared_label_for_the_owned_one_and_names_the_parent_node() -> None:
    found = items()

    story_values = rules.canonical(found["STORY::A::ONE"])
    task_values = rules.canonical(found["TASK::A::1"])

    assert story_values["labels"] == ["area-a", "graphico"] and story_values["parent"] is None
    assert (task_values["kind"], task_values["parent"], task_values["points"]) == ("subtask", "STORY::A::ONE", 5)
    assert "specflow" not in str(task_values) and digest(task_values) == digest(rules.canonical(found["TASK::A::1"]))


def test_a_task_description_has_the_lead_the_note_the_checklist_the_specs_and_the_collapsed_facts() -> None:
    found = items()
    values = rules.canonical(found["TASK::A::1"])

    rendered = rules.render(values, "TASK::A::1")

    assert rendered.title == "Task TASK::A::1_R2" and rendered.estimate == 5
    assert rendered.label_names == ("graphico", "area-a")
    lines = rendered.description.split("\n")
    assert lines[0] == "Do the thing."
    assert lines.index("> [!NOTE]") < lines.index("### Done when") < lines.index(">>> Graphico")
    assert lines[lines.index("### Done when") + 1] == "- [ ] it works" and "- [ ]   " not in rendered.description
    assert "**Specs:** Pay (FEAT::X)" in lines
    facts = lines[lines.index(">>> Graphico") : -1]
    assert facts == [
        ">>> Graphico",
        "- Node: `TASK::A::1`",
        "- Kind: Task",
        "- Area: a",
        "- Phase: logic",
        "- Revision: 2",
        "- Points: 5",
    ]
    assert lines[-1] == ">>>"


def test_a_story_description_names_its_release_and_has_no_checklist_or_revision() -> None:
    values = rules.canonical(items()["STORY::A::ONE"])

    rendered = rules.render(values, "STORY::A::ONE")

    assert "### Done when" not in rendered.description and "- Revision" not in rendered.description
    assert (
        "- Release: R1" in rendered.description and rendered.release == "R1" and "- Kind: Story" in rendered.description
    )
    assert rendered.estimate == 5  # the sum of its tasks


def test_the_change_list_names_each_field_that_differs_in_the_jira_wording_as_markdown() -> None:
    old = rules.canonical(items()["TASK::A::1"])
    new = {**old, "summary": "Renamed", "text": "New text\nsecond line", "points": 3, "labels": ["area-b", "graphico"]}

    lines = rules.change_items(old, new)

    assert 'Summary: "Task TASK::A::1_R2" → "Renamed"' in lines[0] and lines[0].startswith("- ")
    assert any(
        line.startswith("- Description changed. New text:") and "  > New text\n  > second line" in line
        for line in lines
    )
    assert "- Story points: 5 → 3" in lines
    assert "- Label: added area-b" in lines and "- Label: removed area-a" in lines
    assert not any("Blocked by" in line for line in lines)  # relations are not content
    assert rules.change_items(old, old) == []


def test_the_comment_leads_with_the_node_and_the_state_then_the_list() -> None:
    assert rules.comment("N", rules.CHANGED, ["- a"], "In Progress") == (
        'Graphico plan update for `N`. This issue is in state "In Progress", '
        "so Graphico left its fields as they are.\n\n- a"
    )
    assert rules.comment("N", rules.REMOVED).endswith("removed from the Graphico plan.")
    assert rules.comment("N", rules.REVIVED).endswith("back in the Graphico plan.")


# The scaffold -----------------------------------------------------------------------------------------------------


class StaleLabels(FakeLinear):
    """A Linear whose first label read does not show the label a person made a moment ago."""

    async def labels(self, team_id: str) -> list[Label]:
        found = await super().labels(team_id)
        if self.count("labels") == 1:
            return []
        return found


async def test_labels_are_found_by_name_before_they_are_made_and_the_areas_hang_under_one_group() -> None:
    linear = FakeLinear()
    team = linear.add_team("ENG")
    person = await linear.create_label(team.id, str(uuid.uuid4()), "Graphico")  # made by hand, other case, other id
    scaffold = Scaffold(linear, uuid.uuid4(), team_id=team.id, linear_project_id="p")

    ids = await scaffold.label_ids(["graphico", "area-a", "area-b"])

    assert ids[0] == person.id
    every = {label.name: label for label in await linear.labels(team.id)}
    group = every["Graphico area"]
    assert group.is_group and every["area-a"].parent_id == group.id == every["area-b"].parent_id
    assert [call for call, _ in linear.writes].count("create_label") == 4  # the person's, the group and the two areas
    assert await scaffold.is_owned(every["area-a"]) and await scaffold.is_owned(person)


async def test_a_second_project_of_the_team_makes_no_label_the_first_one_made() -> None:
    linear = FakeLinear()
    team = linear.add_team("ENG")
    first = Scaffold(linear, uuid.uuid4(), team_id=team.id, linear_project_id="p")
    await first.label_ids(["graphico", "area-a"])
    writes = len(linear.writes)

    second = Scaffold(linear, uuid.uuid4(), team_id=team.id, linear_project_id="q")
    assert await second.label_ids(["graphico", "area-a"]) == await first.label_ids(["graphico", "area-a"])

    assert len(linear.writes) == writes


async def test_a_label_made_meanwhile_is_adopted_when_the_create_is_refused() -> None:
    linear = StaleLabels()
    team = linear.add_team("ENG")
    project = uuid.uuid4()
    made = await linear.create_label(team.id, str(node_uuid(project, KIND_LABEL, "/graphico")), "graphico")
    scaffold = Scaffold(linear, project, team_id=team.id, linear_project_id="p")

    assert await scaffold.label_ids(["graphico"]) == [made.id]


async def test_a_milestone_is_made_once_per_pass_and_an_existing_one_is_adopted() -> None:
    linear = FakeLinear()
    team = linear.add_team("ENG")
    linear_project = linear.add_project(team.id, "Shop")
    graphico = uuid.uuid4()
    expected = str(node_uuid(graphico, KIND_MILESTONE, "R1"))
    await linear.create_milestone(linear_project.id, expected, "R1")
    scaffold = Scaffold(linear, graphico, team_id=team.id, linear_project_id=linear_project.id)

    assert await scaffold.milestone("R1") == expected and await scaffold.milestone("R1") == expected
    assert linear.count("create_milestone") == 2  # the setup and one adopted try; the second ask made no call
    with suppress(LinearAlreadyExistsError):
        await linear.create_milestone(linear_project.id, expected, "R1")
