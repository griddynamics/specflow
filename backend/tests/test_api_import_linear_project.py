"""The Linear importer (``api.imports.linear_project``) over ``FakeLinear`` and a writer that records what it stores: a
project's issues over several pages with their comments, the project documents, an issue with its sub-issues, the cap,
the same bytes on a second read (the writer's dedupe), personal data left out, and the failure reasons of the source.

Sequence::

    importer -> connection (the member the project is bound to) -> gateway: project (issues, paged) or issue (children)
    importer -> writer.declare(issues + documents) -> per issue: comments -> writer.add
    an issue whose comments are refused (403/404) -> writer.fail(no_access, read), the import goes on
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from api.imports import ImportFailedError, ImportJob
from api.imports import linear_project as module
from api.imports.linear_project import (
    LinearImport,
    LinearProjectImporter,
    inspect_link,
    project_issues,
    sub_issues,
)
from atlassian_import_support import RecordingWriter, as_writer
from linear.errors import (
    LinearForbiddenError,
    LinearNotFoundError,
    LinearUnauthorizedError,
    LinearUnavailableError,
)
from linear_import_support import ACTING, FakeLinearConnections, Workspace, make_issue, seed_workspace


def job() -> ImportJob:
    return ImportJob(uuid.UUID(int=1), uuid.UUID(int=2), ACTING, "linear_project")


async def run(
    source: LinearImport, space: Workspace, connections: FakeLinearConnections | None = None
) -> RecordingWriter:
    writer = RecordingWriter()
    await LinearProjectImporter(connections or FakeLinearConnections(space.fake), source)(job(), as_writer(writer))
    return writer


def of_project(space: Workspace, exclude: frozenset[str] = frozenset()) -> LinearImport:
    return LinearImport("project", space.project.id, exclude)


async def test_a_project_is_stored_issue_by_issue_over_its_pages_with_the_comments_and_the_documents() -> None:
    space = await seed_workspace()

    writer = await run(of_project(space), space)

    issues = [f"issue:GRA-{n}" for n in range(1, 6)]
    assert [d[0] for d in writer.declared[:5]] == issues
    assert [d[0].split(":")[0] for d in writer.declared[5:]] == ["document", "document"]
    assert list(writer.added)[:5] == issues and len(writer.added) == 7 and writer.failed == []
    assert space.fake.count("issues_of_project") == 3  # five issues over pages of two
    name, markdown, original = writer.added["issue:GRA-1"]
    assert name == "GRA-1 Card flow"
    assert markdown.startswith("# GRA-1 Card flow\n\nState: Backlog | Labels: payments | Priority: High")
    assert "Assignee: Grace Hopper" in markdown and "\n\nURL: https://linear.app/graphico-app/issue/GRA-1/" in markdown
    assert "Pay by card." in markdown and "## Comments\n\n**Ada Lovelace** (2026-01-" in markdown
    assert markdown.endswith("Looks good")
    stored = json.loads(original)
    assert stored["issue"]["identifier"] == "GRA-1" and stored["comments"][0]["author"] == "Ada Lovelace"
    assert "Parent: GRA-1 Card flow" in writer.added["issue:GRA-3"][1]
    assert "**Alan Turing**" in writer.added["issue:GRA-2"][1] and "Estimate: 3" in writer.added["issue:GRA-2"][1]


async def test_the_project_documents_are_stored_by_title_and_content() -> None:
    space = await seed_workspace()

    writer = await run(of_project(space), space)

    documents = {name: markdown for key, (name, markdown, _) in writer.added.items() if key.startswith("document:")}
    assert documents == {
        "Payment glossary": "# Payment glossary\n\nAuthorisation, capture and refund.",
        "Rollout notes": "# Rollout notes\n\nShip behind a flag.",
    }
    key = next(key for key in writer.added if key.startswith("document:"))
    assert json.loads(writer.added[key][2])["document"]["title"] == "Payment glossary"


async def test_an_issue_is_stored_with_its_sub_issues_at_every_level_and_no_project_documents() -> None:
    space = await seed_workspace()

    writer = await run(LinearImport("issue", space.issues[0].id), space)

    assert list(writer.added) == ["issue:GRA-1", "issue:GRA-3", "issue:GRA-4", "issue:GRA-5"]
    assert writer.failed == [] and space.fake.count("project_documents") == 0


async def test_excluded_issues_are_neither_declared_nor_stored_but_the_issue_of_an_issue_source_always_is() -> None:
    space = await seed_workspace()

    in_project = await run(of_project(space, frozenset({"GRA-2", "GRA-99"})), space)
    in_issue = await run(LinearImport("issue", space.issues[0].id, frozenset({"GRA-1", "GRA-3"})), space)

    assert [d[0] for d in in_project.declared[:4]] == ["issue:GRA-1", "issue:GRA-3", "issue:GRA-4", "issue:GRA-5"]
    assert "issue:GRA-2" not in in_project.added
    assert list(in_issue.added) == ["issue:GRA-1", "issue:GRA-4", "issue:GRA-5"]


async def test_the_parent_outside_the_import_is_read_once_and_one_that_is_gone_is_left_out() -> None:
    space = await seed_workspace()
    only_children = await run(of_project(space, frozenset({"GRA-1"})), space)

    assert "Parent: GRA-1 Card flow" in only_children.added["issue:GRA-3"][1]  # known although excluded
    outside = await make_issue(space.fake, space.team, "Epic", description="x")
    await make_issue(space.fake, space.team, "Under epic", project_id=space.project.id, parent_id=outside.id)
    space.fake.calls.clear()

    writer = await run(of_project(space), space)

    assert "Parent: GRA-6 Epic" in writer.added["issue:GRA-7"][1]
    assert space.fake.count("issue") == 1


async def test_the_cap_takes_the_first_issues_and_logs_the_count_not_the_text(monkeypatch, caplog) -> None:
    space = await seed_workspace()
    monkeypatch.setattr(module, "MAX_LINEAR_ISSUES", 3)
    caplog.set_level(logging.INFO)

    writer = await run(of_project(space), space)

    assert list(writer.added)[:3] == ["issue:GRA-1", "issue:GRA-2", "issue:GRA-3"] and "issue:GRA-4" not in writer.added
    capped = [r for r in caplog.records if r.getMessage().endswith("linear_cap_reached")]
    assert len(capped) == 1 and "Card flow" not in caplog.text


async def test_the_readers_stop_at_the_cap_and_say_whether_there_are_more() -> None:
    space = await seed_workspace()
    fake = space.fake

    assert [len(r) for r in (await project_issues(fake, space.project.id, 5))[:1]] == [5]
    assert (await project_issues(fake, space.project.id, 5))[1] is False
    assert [i.identifier for i in (await project_issues(fake, space.project.id, 4))[0]] == [
        "GRA-1",
        "GRA-2",
        "GRA-3",
        "GRA-4",
    ]
    assert (await project_issues(fake, space.project.id, 4))[1] is True
    assert (await sub_issues(fake, space.issues[0].id, 3))[1] is False
    found, capped = await sub_issues(fake, space.issues[0].id, 2)
    assert ([i.identifier for i in found], capped) == (["GRA-3", "GRA-4"], True)
    assert await sub_issues(fake, space.issues[1].id, 5) == ([], False)


async def test_reading_the_same_project_twice_gives_the_same_bytes_so_the_writer_finds_the_copy() -> None:
    space = await seed_workspace()

    first = await run(of_project(space), space)
    second = await run(of_project(space), space)

    assert first.added == second.added and len(first.added) == 7  # same name, Markdown and original of every item


async def test_a_changed_issue_is_a_new_original() -> None:
    space = await seed_workspace()
    before = await run(of_project(space), space)
    await space.fake.update_issue(space.issues[1].id, title="Wallet flow v2")

    after = await run(of_project(space), space)

    changed = [key for key in before.added if before.added[key][2] != after.added[key][2]]
    assert changed == ["issue:GRA-2"]


async def test_no_e_mail_address_and_no_mention_id_reaches_a_document_or_its_original() -> None:
    space = await seed_workspace()
    user = str(uuid.uuid4())
    await space.fake.update_issue(
        space.issues[1].id,
        description=f"Ask ada@example.com or [@Grace Hopper](https://linear.app/x/profiles/grace-h) and @{user}.",
    )
    space.fake.add_comment_as(
        space.issues[1].id,
        space.fake.add_user("Bob"),
        f'Mail me: bob+x@corp.example.org <mention id="{user}">Bob B</mention>',
    )
    space.fake.add_document(space.project.id, "Contacts", "Reach carol@example.net")

    writer = await run(of_project(space), space)

    everything = " ".join(
        f"{name} {markdown} {original.decode()}" for name, markdown, original in writer.added.values()
    )
    assert "@example.com" not in everything and "example.org" not in everything and "example.net" not in everything
    assert user not in everything and "profiles/" not in everything
    markdown = writer.added["issue:GRA-2"][1]
    assert "Ask [email removed] or @Grace Hopper and @someone." in markdown and "@Bob B" in markdown


async def test_an_issue_whose_comments_the_member_cannot_read_is_skipped_and_counted() -> None:
    space = await seed_workspace()
    space.fake.fail_next(LinearForbiddenError(), method="comments")

    writer = await run(of_project(space), space)

    assert writer.failed == [("issue:GRA-1", "no_access", "read")]
    assert "issue:GRA-1" not in writer.added and len(writer.added) == 6


@pytest.mark.parametrize("error", [LinearUnauthorizedError(), LinearForbiddenError(), LinearNotFoundError()])
async def test_a_refused_read_of_the_source_fails_it_with_no_access(error) -> None:
    space = await seed_workspace()
    space.fake.fail_next(error, method="issues_of_project")

    with pytest.raises(ImportFailedError) as raised:
        await run(of_project(space), space)

    assert raised.value.reason == "no_access"


async def test_a_token_that_stays_refused_after_one_refresh_fails_an_issue_source_with_no_access() -> None:
    space = await seed_workspace()
    space.fake.fail_next(LinearUnauthorizedError(), method="children")

    with pytest.raises(ImportFailedError) as raised:
        await run(LinearImport("issue", space.issues[0].id), space)

    assert raised.value.reason == "no_access"


async def test_any_other_failure_is_unknown_and_a_gone_or_expired_connection_is_no_access() -> None:
    space = await seed_workspace()
    space.fake.fail_next(LinearUnavailableError(), method="comments")
    with pytest.raises(ImportFailedError) as raised:
        await run(of_project(space), space)
    assert raised.value.reason == "unknown_error"

    for connections in (
        FakeLinearConnections(space.fake, status="expired"),
        FakeLinearConnections(space.fake, user_id=uuid.UUID(int=5)),
    ):
        with pytest.raises(ImportFailedError) as refused:
            await run(of_project(space), space, connections)
        assert refused.value.reason == "no_access"


async def test_the_check_lists_a_project_with_its_items_parents_and_document_count() -> None:
    space = await seed_workspace()

    found = await inspect_link(space.fake, space.project.url)

    assert (found.target.kind, found.target.name, found.target.url) == ("project", "Checkout", space.project.url)
    assert (found.issues, found.capped, found.documents) == (5, False, 2)
    assert [(i.identifier, i.title, i.state, i.parent) for i in found.items] == [
        ("GRA-1", "Card flow", "Backlog", None),
        ("GRA-2", "Wallet flow", "Backlog", None),
        ("GRA-3", "Card retries", "Backlog", "GRA-1"),
        ("GRA-4", "Card refunds", "Backlog", "GRA-1"),
        ("GRA-5", "Retry backoff", "Backlog", "GRA-3"),
    ]


async def test_the_check_takes_an_issue_by_link_or_identifier_and_lists_its_sub_issues() -> None:
    space = await seed_workspace()

    for link in (space.issues[0].url, "GRA-1", " gra-1 "):
        found = await inspect_link(space.fake, link)

        assert (found.target.kind, found.target.name, found.documents) == ("issue", "GRA-1 Card flow", 0)
        assert [i.identifier for i in found.items] == ["GRA-3", "GRA-4", "GRA-5"] and found.items[0].parent == "GRA-1"


async def test_the_check_says_when_there_are_more_issues_than_the_cap(monkeypatch) -> None:
    space = await seed_workspace()
    monkeypatch.setattr(module, "MAX_LINEAR_ISSUES", 4)

    found = await inspect_link(space.fake, space.project.url)

    assert (found.issues, found.capped, len(found.items)) == (4, True, 4)


async def test_a_project_with_only_a_description_and_an_overview_is_one_document_found_by_its_identifier() -> None:
    space = await seed_workspace()
    empty = space.fake.add_project(
        space.team.id, "New UI", description="Liquid glass header and footer.", content="## Scope\n\nHeader, footer."
    )
    identifier = space.fake.project_identifier(empty.id)

    for link in (identifier, identifier.lower(), f"{empty.url}/overview"):
        found = await inspect_link(space.fake, link)
        assert (found.target.kind, found.target.name, found.issues, found.documents) == ("project", "New UI", 0, 1)

    writer = await run(LinearImport("project", empty.id), space)

    assert writer.added == {
        f"document:{empty.id}": (
            "New UI",
            "# New UI\n\nLiquid glass header and footer.\n\n## Scope\n\nHeader, footer.",
            writer.added[f"document:{empty.id}"][2],
        )
    }
    assert json.loads(writer.added[f"document:{empty.id}"][2])["document"]["url"] == empty.url


async def test_a_project_without_any_text_has_no_overview_document() -> None:
    space = await seed_workspace()
    bare = space.fake.add_project(space.team.id, "Bare")

    found = await inspect_link(space.fake, space.fake.project_identifier(bare.id))

    assert (found.issues, found.documents) == (0, 0)
