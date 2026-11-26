"""``FakeLinear`` behaves like Linear where the streams depend on it."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest

from linear.errors import (
    LinearAlreadyExistsError,
    LinearInvalidError,
    LinearNotFoundError,
    LinearRateLimitedError,
    LinearUnavailableError,
)
from linear.fake import FakeLinear
from linear.models import Issue, NewIssue, Project, RelationType


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def linear() -> FakeLinear:
    fake = FakeLinear()
    fake.add_team("ENG", "Engineering", team_id="00000000-0000-0000-0000-00000000e001")
    return fake


@pytest.fixture
def team_id(linear: FakeLinear) -> str:
    return next(iter(linear._teams))


async def issue(linear: FakeLinear, team_id: str, title: str = "Story", **kwargs: Any) -> Issue:
    return await linear.create_issue(NewIssue(issue_id=new_id(), team_id=team_id, title=title, **kwargs))


async def test_the_viewer_is_the_member_and_the_workspace(linear: FakeLinear) -> None:
    viewer = await linear.viewer()

    assert (viewer.id, viewer.name) == (linear.user_id, "Fake Member")
    assert viewer.organization.url_key == "graphico-app"
    assert [team.key for team in await linear.teams()] == ["ENG"]


async def test_an_issue_gets_the_next_identifier_of_its_team_and_the_first_state(
    linear: FakeLinear, team_id: str
) -> None:
    first = await issue(linear, team_id, "Add login")
    second = await issue(linear, team_id, "Add logout")

    assert (first.identifier, second.identifier) == ("ENG-1", "ENG-2")
    assert first.state.type == "backlog"
    assert first.url == "https://linear.app/graphico-app/issue/ENG-1/add-login"
    assert (await linear.issue("ENG-2")).id == second.id  # by identifier, any case
    assert (await linear.issue("eng-2")).id == second.id


async def test_a_reused_client_id_is_already_exists_for_every_create(linear: FakeLinear, team_id: str) -> None:
    key = new_id()
    created = await linear.create_issue(NewIssue(issue_id=key, team_id=team_id, title="A"))
    project = await linear.create_project(new_id(), team_id, "P")
    milestone = await linear.create_milestone(project.id, new_id(), "R1")
    label = await linear.create_label(team_id, new_id(), "area-x")

    with pytest.raises(LinearAlreadyExistsError):
        await linear.create_issue(NewIssue(issue_id=key, team_id=team_id, title="A again"))
    with pytest.raises(LinearAlreadyExistsError):
        await linear.create_project(project.id, team_id, "P2")
    with pytest.raises(LinearAlreadyExistsError):
        await linear.create_milestone(project.id, milestone.id, "R1")
    with pytest.raises(LinearAlreadyExistsError):
        await linear.create_label(team_id, label.id, "area-y")
    assert [i.identifier for i in linear.all_issues()] == [created.identifier]  # nothing was duplicated


async def test_a_client_id_must_be_a_uuid(linear: FakeLinear, team_id: str) -> None:
    with pytest.raises(LinearInvalidError) as raised:
        await linear.create_issue(NewIssue(issue_id="not-a-uuid", team_id=team_id, title="A"))

    assert raised.value.field == "issue_id"


async def test_a_sub_issue_hangs_under_its_parent_and_cannot_cycle(linear: FakeLinear, team_id: str) -> None:
    story = await issue(linear, team_id, "Story")
    task = await issue(linear, team_id, "Task", parent_id=story.id)

    assert task.parent_id == story.id
    assert [child.id for child in await linear.children(story.id)] == [task.id]
    with pytest.raises(LinearInvalidError):
        await linear.update_issue(story.id, parent_id=task.id)
    other = linear.add_team("OPS")
    with pytest.raises(LinearInvalidError):  # a parent of another team
        await linear.create_issue(NewIssue(issue_id=new_id(), team_id=other.id, title="X", parent_id=story.id))


async def test_a_milestone_needs_its_project_and_an_issue_keeps_both(linear: FakeLinear, team_id: str) -> None:
    project = await linear.create_project(new_id(), team_id, "Graphico")
    milestone = await linear.create_milestone(project.id, new_id(), "Release 1")
    other = await linear.create_project(new_id(), team_id, "Other")

    placed = await issue(linear, team_id, project_id=project.id, milestone_id=milestone.id)
    with pytest.raises(LinearInvalidError):
        await issue(linear, team_id, project_id=other.id, milestone_id=milestone.id)
    with pytest.raises(LinearInvalidError):
        await issue(linear, team_id, milestone_id=milestone.id)  # a milestone without its project

    assert (placed.project_id, placed.milestone_id) == (project.id, milestone.id)
    page = await linear.issues_of_project(project.id)
    assert [i.id for i in page.items] == [placed.id]
    assert [p.id for p in await linear.projects(team_id)] == [project.id, other.id]


async def test_issues_of_a_project_are_paged_by_cursor(team_id: str) -> None:
    linear = FakeLinear(page_size=2)
    team = linear.add_team("ENG")
    project = await linear.create_project(new_id(), team.id, "P")
    for number in range(5):
        await issue(linear, team.id, f"I{number}", project_id=project.id)

    pages, cursor = [], None
    while True:
        page = await linear.issues_of_project(project.id, cursor)
        pages.append([i.title for i in page.items])
        cursor = page.next_cursor
        if cursor is None:
            break

    assert pages == [["I0", "I1"], ["I2", "I3"], ["I4"]]


async def test_labels_group_children_are_single_select(linear: FakeLinear, team_id: str) -> None:
    group = await linear.create_label(team_id, new_id(), "Graphico area", is_group=True)
    auth = await linear.create_label(team_id, new_id(), "area-auth", parent_id=group.id)
    billing = await linear.create_label(team_id, new_id(), "area-billing", parent_id=group.id)
    plain = await linear.create_label(team_id, new_id(), "graphico")

    tagged = await issue(linear, team_id, label_ids=(auth.id, plain.id))

    assert {label.name for label in tagged.labels} == {"area-auth", "graphico"}
    assert tagged.label_ids == (auth.id, plain.id)
    with pytest.raises(LinearInvalidError):  # two children of one group
        await issue(linear, team_id, label_ids=(auth.id, billing.id))
    with pytest.raises(LinearInvalidError):  # a group is not applied itself
        await issue(linear, team_id, label_ids=(group.id,))
    with pytest.raises(LinearInvalidError):  # a parent must be a group
        await linear.create_label(team_id, new_id(), "x", parent_id=plain.id)
    with pytest.raises(LinearAlreadyExistsError):  # the same name under the same parent
        await linear.create_label(team_id, new_id(), "area-auth", parent_id=group.id)
    assert {label.name for label in await linear.labels(team_id)} == {
        "Graphico area",
        "area-auth",
        "area-billing",
        "graphico",
    }


async def test_the_estimate_scale_of_the_team_can_refuse_a_value(team_id: str) -> None:
    linear = FakeLinear(estimate_allowed=lambda value: value in (1, 2, 3, 5, 8))
    team = linear.add_team("ENG")

    accepted = await issue(linear, team.id, estimate=5)
    with pytest.raises(LinearInvalidError) as raised:
        await issue(linear, team.id, estimate=4)
    with pytest.raises(LinearInvalidError):
        await linear.update_issue(accepted.id, estimate=7)

    assert accepted.estimate == 5
    assert raised.value.field == "estimate"


async def test_update_overwrites_only_the_fields_given_and_refuses_unknown_ones(
    linear: FakeLinear, team_id: str
) -> None:
    created = await issue(linear, team_id, "Old", description="body", estimate=3)
    states = await linear.workflow_states(team_id)
    started = next(state for state in states if state.type == "started")

    updated = await linear.update_issue(created.id, title="New", state_id=started.id)

    assert (updated.title, updated.description, updated.estimate) == ("New", "body", 3)
    assert updated.state.type == "started"
    assert updated.updated_at is not None and created.updated_at is not None and updated.updated_at > created.updated_at
    with pytest.raises(LinearInvalidError) as raised:
        await linear.update_issue(created.id, assignee="x")
    assert raised.value.field == "assignee"
    with pytest.raises(LinearInvalidError):
        await linear.update_issue(created.id, state_id=new_id())


async def test_state_types_and_positions_come_with_the_team(linear: FakeLinear, team_id: str) -> None:
    states = await linear.workflow_states(team_id)

    assert [state.type for state in states] == ["backlog", "unstarted", "started", "started", "completed", "canceled"]
    assert [state.position for state in states] == sorted(state.position for state in states)


async def test_comments_carry_their_author_and_since_filters_by_time(linear: FakeLinear, team_id: str) -> None:
    target = await issue(linear, team_id)
    human = linear.add_user("Dana")
    mine = await linear.create_comment(target.id, "Asked.")
    theirs = linear.add_comment_as(target.id, human, "B")
    later = linear.add_comment_as(target.id, human, "C", created_at=theirs.created_at + timedelta(minutes=5))

    everything = await linear.comments(target.id)
    fresh = await linear.comments(target.id, since=mine.created_at)

    assert [c.body for c in everything] == ["Asked.", "B", "C"]
    assert [(c.user_id, c.user_name) for c in everything] == [
        (linear.user_id, "Fake Member"),
        (human, "Dana"),
        (human, "Dana"),
    ]
    assert [c.id for c in fresh] == [theirs.id, later.id]
    with pytest.raises(LinearInvalidError):
        await linear.create_comment(target.id, "  ")


async def test_a_comment_with_a_client_id_is_idempotent(linear: FakeLinear, team_id: str) -> None:
    target = await issue(linear, team_id)
    key = new_id()

    await linear.create_comment(target.id, "Applied.", comment_id=key)
    with pytest.raises(LinearAlreadyExistsError):
        await linear.create_comment(target.id, "Applied.", comment_id=key)

    assert len(await linear.comments(target.id)) == 1


async def test_relations_are_read_from_both_sides_and_do_not_repeat(linear: FakeLinear, team_id: str) -> None:
    first, second = await issue(linear, team_id, "A"), await issue(linear, team_id, "B")

    relation = await linear.create_relation(new_id(), first.id, second.id, "blocks")

    assert (relation.issue_id, relation.related_issue_id, relation.type) == (first.id, second.id, "blocks")
    assert [r.id for r in await linear.relations(first.id)] == [relation.id]
    assert [r.id for r in await linear.relations(second.id)] == [relation.id]
    with pytest.raises(LinearAlreadyExistsError):
        await linear.create_relation(new_id(), first.id, second.id, "blocks")
    with pytest.raises(LinearInvalidError):
        await linear.create_relation(new_id(), first.id, first.id, "related")
    with pytest.raises(LinearInvalidError):
        await linear.create_relation(new_id(), first.id, second.id, cast("RelationType", "depends"))


async def test_attaching_the_same_link_again_updates_the_title(linear: FakeLinear, team_id: str) -> None:
    target = await issue(linear, team_id)

    first = await linear.attach_link(target.id, "https://example.test/pr/1", "PR")
    second = await linear.attach_link(target.id, "https://example.test/pr/1", "PR #1")

    assert second.id == first.id
    assert [(a.url, a.title) for a in linear.attachments(target.id)] == [("https://example.test/pr/1", "PR #1")]
    with pytest.raises(LinearInvalidError):
        await linear.attach_link(target.id, "javascript:alert(1)", "x")


async def test_project_documents_are_listed_per_project(linear: FakeLinear, team_id: str) -> None:
    project = await linear.create_project(new_id(), team_id, "P")
    other = await linear.create_project(new_id(), team_id, "Q")
    linear.add_document(project.id, "Brief", "# Brief")

    assert [d.title for d in await linear.project_documents(project.id)] == ["Brief"]
    assert await linear.project_documents(other.id) == []


async def test_a_link_or_identifier_resolves_to_an_issue_or_a_project(linear: FakeLinear, team_id: str) -> None:
    project = await linear.create_project(new_id(), team_id, "Graphico Plan")
    created = await issue(linear, team_id, "Story")

    by_identifier = await linear.resolve_link("ENG-1")
    by_issue_url = await linear.resolve_link(created.url)
    by_project_url = await linear.resolve_link(project.url)

    assert isinstance(by_identifier, Issue) and by_identifier.id == created.id
    assert isinstance(by_issue_url, Issue) and by_issue_url.id == created.id
    assert isinstance(by_project_url, Project) and by_project_url.id == project.id
    with pytest.raises(LinearNotFoundError):
        await linear.resolve_link("ENG-99")
    with pytest.raises(LinearNotFoundError):  # another workspace
        await linear.resolve_link("https://linear.app/other-org/issue/ENG-1/x")
    with pytest.raises(LinearInvalidError):
        await linear.resolve_link("not a link")


async def test_an_unknown_id_is_not_found(linear: FakeLinear) -> None:
    with pytest.raises(LinearNotFoundError):
        await linear.issue(new_id())
    with pytest.raises(LinearNotFoundError):
        await linear.project(new_id())
    with pytest.raises(LinearNotFoundError):
        await linear.workflow_states(new_id())


async def test_a_person_moving_an_issue_is_not_a_write(linear: FakeLinear, team_id: str) -> None:
    target = await issue(linear, team_id)
    writes = len(linear.writes)

    moved = linear.move_issue(target.id, "In Review")

    assert moved.state.name == "In Review"
    assert len(linear.writes) == writes
    assert (await linear.issue(target.id)).state.name == "In Review"


async def test_an_injected_rate_limit_fails_the_next_calls_only(linear: FakeLinear, team_id: str) -> None:
    linear.rate_limit_next(7.0, method="create_issue", times=2)

    for _ in range(2):
        with pytest.raises(LinearRateLimitedError) as raised:
            await issue(linear, team_id)
        assert raised.value.retry_after == 7.0
    ok = await issue(linear, team_id)

    assert ok.identifier == "ENG-1"  # a refused create made nothing and used no number
    assert await linear.teams()  # other methods were never limited


async def test_an_injected_failure_can_hit_any_method(linear: FakeLinear) -> None:
    linear.fail_next(LinearUnavailableError())

    with pytest.raises(LinearUnavailableError):
        await linear.viewer()
    assert (await linear.viewer()).id == linear.user_id


async def test_calls_and_writes_are_recorded_so_a_rerun_can_be_shown_to_write_nothing(
    linear: FakeLinear, team_id: str
) -> None:
    await linear.viewer()
    await issue(linear, team_id)

    assert linear.count("viewer") == 1
    assert [name for name, _ in linear.writes] == ["create_issue"]
