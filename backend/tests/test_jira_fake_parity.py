"""The fake and the real gateway expose the same public async methods and signatures."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from jira.gateway import JiraError, JiraGateway
from jira.http import HttpJiraGateway
from jira.reasons import Reason
from jira_fake_support import FakeJiraGateway


def public_methods(cls):
    return {name: member for name, member in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")}


def shape(function):
    return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]


def protocol_methods():
    return {name for name in vars(JiraGateway) if not name.startswith("_")} - {"mode"}


@pytest.mark.parametrize("cls", [HttpJiraGateway, FakeJiraGateway])
def test_implements_every_protocol_method_as_async(cls):
    methods = public_methods(cls)
    for name in protocol_methods():
        assert inspect.iscoroutinefunction(methods[name]), name


def test_signatures_are_identical():
    real, fake = public_methods(HttpJiraGateway), public_methods(FakeJiraGateway)
    for name in protocol_methods():
        assert shape(real[name]) == shape(fake[name]) == shape(getattr(JiraGateway, name)), name


def test_real_exposes_nothing_beyond_the_protocol():
    assert set(public_methods(HttpJiraGateway)) == protocol_methods()


# Behaviour of the fake that sessions 03 to 05 rely on


async def test_fake_create_find_and_crash_after_the_create_reached_jira():
    fake = FakeJiraGateway()
    fake.fail("create_issue", Reason.UNREACHABLE, after_effect=True)
    with pytest.raises(JiraError):
        await fake.create_issue({"summary": "S", "labels": ["specflow"]}, project_id="p-1", node_id="n-1")
    await fake.create_issue({"summary": "T"}, project_id="p-2", node_id="n-2")
    assert await fake.find_created("PRJ", project_id="p-1", minutes=10) == {"n-1": "PRJ-1"}
    assert fake.issues["PRJ-1"].labels == {"specflow"}


async def test_fake_deleted_issue_is_none_in_states_and_404_elsewhere():
    fake = FakeJiraGateway()
    key = await fake.create_issue({"summary": "S"}, project_id="p", node_id="n")
    fake.delete(key)
    assert await fake.issue_states([key]) == {key: None}
    with pytest.raises(JiraError) as caught:
        await fake.add_comment(key, {})
    assert caught.value.http_status == 404


async def test_fake_links_blocked_by_and_delete():
    fake = FakeJiraGateway()
    first = await fake.create_issue({"summary": "A"}, project_id="p", node_id="a")
    second = await fake.create_issue({"summary": "B"}, project_id="p", node_id="b")
    await fake.add_blocks_link(prerequisite=first, waiter=second)
    state = (await fake.issue_states([second]))[second]
    assert list(state.blocked_by) == [first]
    await fake.delete_link(state.blocked_by[first])
    assert (await fake.issue_states([second]))[second].blocked_by == {}


async def test_fake_rank_moves_within_the_parent_and_labels_are_edited():
    fake = FakeJiraGateway()
    epic = fake.add_issue({"summary": "E"}).key
    keys = [await fake.create_issue({"summary": n, "parent": {"key": epic}}, project_id="p", node_id=n) for n in "abc"]
    await fake.rank([keys[2]], before=keys[0])
    assert fake.rank_order[epic] == [keys[2], keys[0], keys[1]]
    await fake.edit_issue(keys[0], fields={"summary": "z"}, add_labels=["x"], remove_labels=["y"])
    assert fake.issues[keys[0]].labels == {"x"}
    assert fake.issues[keys[0]].fields["summary"] == "z"


async def test_fake_rate_limit_waits_or_ends_like_the_real_gateway():
    fake = FakeJiraGateway()
    fake.rate_limit("myself", 3, times=2)
    await fake.myself()
    assert fake.waits == [3, 3]
    fake.rate_limit("myself", 120)
    with pytest.raises(JiraError):
        await fake.myself()
    fake.rate_limit("myself", 1, times=6)
    with pytest.raises(JiraError):
        await fake.myself()


async def test_fake_hook_and_hold_run_inside_the_call():
    fake = FakeJiraGateway()
    seen = []
    fake.on_call = seen.append

    async def hold():
        seen.append("held")

    fake.holds["myself"] = hold
    await fake.myself()
    assert seen == ["myself", "held"]
    assert fake.called("myself") == [{}]


# Fidelity of the fake: the behaviours the real Jira shows and the fake must share


async def test_fake_find_created_needs_the_label_and_the_window():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = FakeJiraGateway()
    fake.now = lambda: now
    labelled = {"summary": "A", "labels": ["specflow"]}
    await fake.create_issue({"summary": "B"}, project_id="p", node_id="unlabelled")
    await fake.create_issue(labelled, project_id="p", node_id="old")
    now += timedelta(minutes=10)
    await fake.create_issue(labelled, project_id="p", node_id="recent")
    assert await fake.find_created("PRJ", project_id="p", minutes=5) == {"recent": "PRJ-3"}
    assert set(await fake.find_created("PRJ", project_id="p", minutes=15)) == {"old", "recent"}


async def test_fake_answers_400_for_a_parent_that_is_unknown_or_deleted():
    fake = FakeJiraGateway()
    epic = fake.add_issue({"summary": "E"}).key
    child = await fake.create_issue({"summary": "C", "parent": {"key": epic}}, project_id="p", node_id="c")
    for parent in ("PRJ-404", epic):
        if parent == epic:
            fake.delete(epic)
        with pytest.raises(JiraError) as caught:
            await fake.create_issue({"summary": "X", "parent": {"key": parent}}, project_id="p", node_id="x")
        assert caught.value.http_status == 400
        with pytest.raises(JiraError):
            await fake.edit_issue(child, fields={"parent": {"key": parent}})


async def test_fake_seeding_a_key_still_advances_the_key_counter():
    fake = FakeJiraGateway()
    fake.add_issue(key="PRJ-1")
    assert (await fake.create_issue({"summary": "S"}, project_id="p", node_id="n")) == "PRJ-2"
