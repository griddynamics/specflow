"""Live tier of ``db.members`` and invite adoption in ``upsert_user`` (a migrated scratch database per test).

Gate: ``SPECFLOW_TEST_POSTGRES=1`` (see ``specflow_live``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.emails import InvalidCollaboratorsError, check_collaborators
from db import members
from db import repositories as repo
from db.models import JiraTarget, Project, ProjectInvite, ProjectMember, ProjectRepository, User
from db.session import build_engine, build_session_factory
from logger_harness import events, fields
from settings import DatabaseSettings
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

DOMAINS = {"example.com"}


class Principal:
    def __init__(self, email: str, name: str | None = None, subject: str | None = None) -> None:
        self.subject, self.email, self.name = subject or f"user:{email}", email, name


@pytest_asyncio.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def signed_in(factory, email: str, name: str | None = None, subject: str | None = None) -> User:
    async with factory() as session, session.begin():
        user = await repo.upsert_user(session, Principal(email, name, subject))
        session.expunge(user)
    return user


async def new_project(factory, owner: User, name: str = "Alpha") -> uuid.UUID:
    async with factory() as session, session.begin():
        return (await repo.create_project(session, owner.id, name)).id


async def invite_row(factory, project_id: uuid.UUID, owner: User, email_norm: str) -> None:
    async with factory() as session, session.begin():
        session.add(ProjectInvite(project_id=project_id, email_norm=email_norm, invited_by=owner.id))


async def member_roles(factory, project_id: uuid.UUID) -> dict[uuid.UUID, str]:
    async with factory() as session:
        rows = await session.execute(
            select(ProjectMember.user_id, ProjectMember.role).where(ProjectMember.project_id == project_id)
        )
        return dict(rows.all())


async def invite_count(factory, project_id: uuid.UUID) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).where(ProjectInvite.project_id == project_id)) or 0


# Adoption ------------------------------------------------------------------------


async def test_new_user_adopts_invites_of_two_projects_and_logs_ids_only(factory, caplog):
    """FR-PRJ-INV-01: the email is matched normalised, both projects adopted, no invite left."""

    owner = await signed_in(factory, "owner@example.com")
    p1, p2 = await new_project(factory, owner, "A"), await new_project(factory, owner, "B")
    await invite_row(factory, p1, owner, "c@example.com")
    await invite_row(factory, p2, owner, "c@example.com")

    with caplog.at_level("INFO"):
        cy = await signed_in(factory, "C@Example.com")

    assert (await member_roles(factory, p1))[cy.id] == "collaborator"
    assert (await member_roles(factory, p2))[cy.id] == "collaborator"
    assert await invite_count(factory, p1) == await invite_count(factory, p2) == 0
    records = events(caplog, "db.invite.adopted")
    assert sorted(fields(r)["project_id"] for r in records) == sorted([str(p1), str(p2)])
    assert all("@" not in str(fields(r)) for r in records)


async def test_an_invite_inserted_after_first_sign_in_is_adopted_on_the_next_request(factory):
    """FR-PRJ-INV-04, existing-user return path."""

    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    bob = await signed_in(factory, "bob@example.com")
    await invite_row(factory, project, owner, "bob@example.com")
    assert bob.id not in await member_roles(factory, project)

    again = await signed_in(factory, "bob@example.com")

    assert again.id == bob.id and (await member_roles(factory, project))[bob.id] == "collaborator"
    assert await invite_count(factory, project) == 0


async def test_an_account_whose_email_changed_adopts_the_invite_of_the_new_email(factory):
    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    await signed_in(factory, "old@example.com", subject="sub-moves")
    await invite_row(factory, project, owner, "new@example.com")

    moved = await signed_in(factory, "new@example.com", subject="sub-moves")

    assert (await member_roles(factory, project))[moved.id] == "collaborator"


async def test_the_raced_first_sign_in_path_adopts_too(factory, monkeypatch):
    """The third return path of ``upsert_user``: the insert loses to a concurrent first sign-in."""

    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    await invite_row(factory, project, owner, "racer@example.com")
    real = repo._resolve_user

    async def lose_the_race(session, principal):
        async with factory() as other, other.begin():
            winner = User(
                actor_id="human:winner", display_name="W", email=principal.email, auth_subject=principal.subject
            )
            other.add(winner)
        return await real(session, principal)

    monkeypatch.setattr(repo, "_resolve_user", lose_the_race)
    user = await signed_in(factory, "racer@example.com")

    assert (await member_roles(factory, project))[user.id] == "collaborator"


async def test_two_sessions_adopting_at_once_leave_one_member_row_and_no_error(factory):
    """FR-PRJ-INV-03."""

    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    dee = await signed_in(factory, "dee@example.com")
    await invite_row(factory, project, owner, "dee@example.com")
    gate, second_started = asyncio.Event(), asyncio.Event()

    async def request(started: asyncio.Event | None = None) -> None:
        async with factory() as session, session.begin():
            if started is not None:
                started.set()
            await repo.upsert_user(session, Principal("dee@example.com"))
            await gate.wait()  # the first transaction holds the invite row lock until the second is under way

    first = asyncio.create_task(request())
    second = asyncio.create_task(request(second_started))
    await second_started.wait()
    gate.set()
    await asyncio.gather(first, second)

    roles = await member_roles(factory, project)
    assert roles[dee.id] == "collaborator" and len(roles) == 2
    assert await invite_count(factory, project) == 0


async def test_an_existing_member_who_is_also_invited_loses_the_invite_and_keeps_one_row(factory):
    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    await invite_row(factory, project, owner, "owner@example.com")

    again = await signed_in(factory, "owner@example.com")

    assert (await member_roles(factory, project)) == {again.id: "owner"}
    assert await invite_count(factory, project) == 0


async def test_a_token_email_the_normaliser_rejects_signs_in_and_adopts_nothing(factory):
    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    await invite_row(factory, project, owner, "x@example.com")
    long_email = "x" * 260 + "@example.com"

    user = await signed_in(factory, long_email)

    assert user.id is not None and user.email == long_email
    assert await invite_count(factory, project) == 1 and len(await member_roles(factory, project)) == 1


# Add, list, remove, leave, cancel, rename --------------------------------------------


async def add(factory, project_id: uuid.UUID, owner: User, email: str):
    async with factory() as session, session.begin():
        return await members.add_member_or_invite(session, project_id, owner.id, email, DOMAINS)


async def test_add_a_signed_in_user_is_a_member_and_an_unknown_email_is_an_invite(factory):
    owner = await signed_in(factory, "owner@example.com", "Ada")
    bob = await signed_in(factory, "Bob@Example.com", "Bob")
    project = await new_project(factory, owner)

    as_member = await add(factory, project, owner, " bob@example.com ")
    as_invite = await add(factory, project, owner, "New@Example.COM")

    assert as_member.kind == "member" and as_member.member.user_id == bob.id and as_member.member.role == "collaborator"
    assert as_invite.kind == "invite" and as_invite.invite.email == "new@example.com"
    assert await member_roles(factory, project) == {owner.id: "owner", bob.id: "collaborator"}
    assert await invite_count(factory, project) == 1


async def test_add_refusals_have_their_codes_and_write_nothing(factory):
    owner = await signed_in(factory, "owner@example.com")
    await signed_in(factory, "bob@example.com")
    project = await new_project(factory, owner)
    await add(factory, project, owner, "bob@example.com")
    await add(factory, project, owner, "x@example.com")

    expectations = {
        "bob@example.com": "already_member",
        "OWNER@example.com": "already_member",  # the owner's own email
        " X@Example.COM ": "already_invited",
        "nope": "invalid_email",
        "a@other.org": "email_domain_not_allowed",
    }
    for email, code in expectations.items():
        with pytest.raises(members.MemberError) as raised:
            await add(factory, project, owner, email)
        assert raised.value.code == code
    assert len(await member_roles(factory, project)) == 2 and await invite_count(factory, project) == 1


async def test_add_picks_the_newest_user_when_two_rows_share_an_email(factory):
    owner = await signed_in(factory, "owner@example.com")
    older = await signed_in(factory, "dup@example.com", subject="sub-old")
    newer = await signed_in(factory, "DUP@example.com", subject="sub-new")
    project = await new_project(factory, owner)

    added = await add(factory, project, owner, "dup@example.com")

    assert added.kind == "member" and added.member.user_id == newer.id != older.id
    assert (await members_snapshot(factory, project, owner)).members[1].email == "DUP@example.com"


async def members_snapshot(factory, project_id: uuid.UUID, caller: User) -> members.Members:
    async with factory() as session, session.begin():
        return await members.list_members(session, project_id, caller.id)


async def test_list_members_orders_owner_first_then_joined_and_names_follow_the_member_names_rule(factory):
    owner = await signed_in(factory, "owner@example.com", "Ada")
    bob = await signed_in(factory, "bob@example.com", "Bob")
    cy = await signed_in(factory, "cy@example.com")  # display name = email: no name
    project = await new_project(factory, owner)
    await add(factory, project, owner, "cy@example.com")
    await add(factory, project, owner, "bob@example.com")
    await add(factory, project, owner, "zed@example.com")
    await add(factory, project, owner, "amy@example.com")

    as_bob = await members_snapshot(factory, project, bob)

    assert as_bob.limit == 50
    assert [m.user_id for m in as_bob.members] == [owner.id, cy.id, bob.id]
    assert [m.name for m in as_bob.members] == ["Ada", None, "Bob"]
    assert [m.is_you for m in as_bob.members] == [False, False, True]
    assert [i.email for i in as_bob.invites] == ["zed@example.com", "amy@example.com"]
    assert [m.is_you for m in (await members_snapshot(factory, project, owner)).members] == [True, False, False]
    async with factory() as session:  # the same names as ``member_names``
        names = await repo.member_names(session, project, [m.user_id for m in as_bob.members])
    assert [names[m.user_id] for m in as_bob.members] == [m.name for m in as_bob.members]


async def acting_accounts(factory, *project_ids: uuid.UUID) -> list[uuid.UUID | None]:
    async with factory() as session:
        query = select(JiraTarget.connected_user_id)
        return [await session.scalar(query.where(JiraTarget.project_id == p)) for p in project_ids]


async def test_a_removed_or_departed_member_stops_being_the_jira_acting_account_of_that_project_only(factory):
    owner = await signed_in(factory, "owner@example.com")
    bob = await signed_in(factory, "bob@example.com")
    cy = await signed_in(factory, "cy@example.com")
    alpha, beta, gamma = [await new_project(factory, owner, name) for name in ("Alpha", "Beta", "Gamma")]
    for project in (alpha, beta, gamma):
        await add(factory, project, owner, "bob@example.com")
        await add(factory, project, owner, "cy@example.com")
    async with factory() as session, session.begin():
        for project, acting in ((alpha, bob.id), (beta, cy.id), (gamma, bob.id)):
            target = JiraTarget(project_id=project, jira_project="PRJ", enabled=True, updated_at=func.now())
            target.connected_user_id = acting
            session.add(target)

    async with factory() as session, session.begin():
        await members.remove_member(session, alpha, bob.id)
        await members.leave_project(session, beta, cy.id)
        await members.leave_project(session, gamma, cy.id)  # cy is not gamma's acting account: bob stays
    with pytest.raises(members.CannotRemoveOwnerError):
        async with factory() as session, session.begin():
            await members.remove_member(session, gamma, owner.id)

    assert await acting_accounts(factory, alpha, beta, gamma) == [None, None, bob.id]


async def test_a_removed_member_stops_being_the_adder_of_that_projects_repositories_only(factory):
    owner = await signed_in(factory, "owner@example.com")
    bob = await signed_in(factory, "bob@example.com")
    alpha, beta = [await new_project(factory, owner, name) for name in ("Alpha", "Beta")]
    for project in (alpha, beta):
        await add(factory, project, owner, "bob@example.com")
    async with factory() as session, session.begin():
        for project in (alpha, beta):
            session.add(
                ProjectRepository(
                    project_id=project,
                    github_repo_id=42,
                    full_name="octo/repo",
                    html_url="https://github.com/octo/repo",
                    private=True,
                    added_by=bob.id,
                )
            )

    async with factory() as session, session.begin():
        await members.remove_member(session, alpha, bob.id)

    async with factory() as session:
        query = select(ProjectRepository.added_by)
        adders = [await session.scalar(query.where(ProjectRepository.project_id == p)) for p in (alpha, beta)]
    assert adders == [None, bob.id]


async def test_remove_leave_cancel_and_rename(factory):
    owner = await signed_in(factory, "owner@example.com")
    bob = await signed_in(factory, "bob@example.com")
    cy = await signed_in(factory, "cy@example.com")
    project = await new_project(factory, owner)
    await add(factory, project, owner, "bob@example.com")
    await add(factory, project, owner, "cy@example.com")
    invite = (await add(factory, project, owner, "later@example.com")).invite

    async def run(function, *args):
        async with factory() as session, session.begin():
            return await function(session, project, *args)

    await run(members.remove_member, bob.id)
    await run(members.leave_project, cy.id)
    assert await member_roles(factory, project) == {owner.id: "owner"}
    for function, who, code in [
        (members.remove_member, owner.id, "cannot_remove_owner"),
        (members.leave_project, owner.id, "owner_cannot_leave"),
        (members.remove_member, bob.id, "member_not_found"),
        (members.leave_project, bob.id, "member_not_found"),
        (members.cancel_invite, uuid.uuid4(), "invite_not_found"),
    ]:
        with pytest.raises(members.MemberError) as raised:
            await run(function, who)
        assert raised.value.code == code
    await run(members.cancel_invite, invite.id)
    assert await invite_count(factory, project) == 0
    later = await signed_in(factory, "later@example.com")  # FR-PRJ-MEM-09: nothing to adopt
    assert later.id not in await member_roles(factory, project)

    before = await run(members.rename_project, "Alpha")
    renamed = await run(members.rename_project, "Beta")
    assert renamed is not None and renamed.name == "Beta" and renamed.updated_at >= before.updated_at
    assert await run(members.rename_project, "x") is not None
    async with factory() as session, session.begin():
        assert await members.rename_project(session, uuid.uuid4(), "x") is None


async def test_limit_holds_under_two_concurrent_adds_at_49(factory):
    """FR-PRJ-MEM-06: owner + 48 invites = 49; two concurrent adds give one success and one ``member_limit``."""

    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    async with factory() as session, session.begin():
        session.add_all(
            ProjectInvite(project_id=project, email_norm=f"i{n}@example.com", invited_by=owner.id) for n in range(48)
        )

    results = await asyncio.gather(
        add(factory, project, owner, "p@example.com"),
        add(factory, project, owner, "q@example.com"),
        return_exceptions=True,
    )

    assert sorted(type(r).__name__ for r in results) == ["InviteAdded", "MemberLimitError"]
    assert await invite_count(factory, project) == 49
    with pytest.raises(members.MemberLimitError):
        await add(factory, project, owner, "r@example.com")  # now at 50


async def test_adoption_does_not_change_the_count_at_the_limit(factory):
    owner = await signed_in(factory, "owner@example.com")
    project = await new_project(factory, owner)
    async with factory() as session, session.begin():
        session.add_all(
            ProjectInvite(project_id=project, email_norm=f"i{n}@example.com", invited_by=owner.id) for n in range(49)
        )
    await signed_in(factory, "i7@example.com")  # moves one row from invites to members

    async with factory() as session, session.begin():
        assert await members.count_people(session, project) == 50


# Create with collaborators -------------------------------------------------------------


async def create(factory, creator: User, emails: list[str], creator_email: str = "owner@example.com") -> uuid.UUID:
    checked = check_collaborators(emails, DOMAINS, creator_email)
    async with factory() as session, session.begin():
        return (await repo.create_project(session, creator.id, "Gamma", checked)).id


async def test_create_with_one_signed_in_and_one_unknown_email(factory):
    """FR-PRJ-CRT-01, and the creator's own email and a duplicate are dropped."""

    owner = await signed_in(factory, "owner@example.com")
    bob = await signed_in(factory, "bob@example.com")

    project = await create(
        factory, owner, ["Bob@example.com", "bob@EXAMPLE.com", "OWNER@example.com", "new@example.com"]
    )

    assert await member_roles(factory, project) == {owner.id: "owner", bob.id: "collaborator"}
    assert await invite_count(factory, project) == 1


async def test_create_without_collaborators_behaves_as_before(factory):
    owner = await signed_in(factory, "owner@example.com")

    project = await create(factory, owner, [])

    assert await member_roles(factory, project) == {owner.id: "owner"} and await invite_count(factory, project) == 0


async def test_create_with_a_bad_email_writes_nothing(factory):
    """FR-PRJ-CRT-02: the check fails before any write, with the index."""

    owner = await signed_in(factory, "owner@example.com")

    with pytest.raises(InvalidCollaboratorsError) as raised:
        await create(factory, owner, ["ok@example.com", "bad"])

    assert [(e.index, e.type) for e in raised.value.errors] == [(1, "invalid_email")]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 0


async def test_create_is_atomic_when_a_write_fails_midway(factory):
    """A failure after the project and owner rows were written rolls everything back."""

    owner = await signed_in(factory, "owner@example.com")
    bob = await signed_in(factory, "bob@example.com")

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            # An unnormalised email breaks ``ck_project_invites_email_norm`` after bob's member row is staged.
            await repo.create_project(session, owner.id, "Gamma", ["bob@example.com", "Bad@Example.com"])

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 0
        assert await session.scalar(select(func.count()).where(ProjectMember.user_id == bob.id)) == 0


async def test_create_prefers_the_newest_user_of_a_duplicate_email(factory):
    owner = await signed_in(factory, "owner@example.com")
    await signed_in(factory, "dup@example.com", subject="sub-old")
    newer = await signed_in(factory, "dup@example.com", subject="sub-new")

    project = await create(factory, owner, ["dup@example.com"])

    assert await member_roles(factory, project) == {owner.id: "owner", newer.id: "collaborator"}


# Membership and suggestions --------------------------------------------------------------


async def test_get_membership_returns_project_and_role_for_any_member_only(factory):
    owner = await signed_in(factory, "owner@example.com")
    bob = await signed_in(factory, "bob@example.com")
    cy = await signed_in(factory, "cy@example.com")
    project = await new_project(factory, owner)
    await add(factory, project, owner, "bob@example.com")

    async with factory() as session, session.begin():
        as_owner = await repo.get_membership(session, owner.id, project)
        as_bob = await repo.get_membership(session, bob.id, project)
        assert as_owner is not None and as_owner[1] == "owner" and as_owner[0].id == project
        assert as_bob is not None and as_bob[1] == "collaborator"
        assert await repo.get_membership(session, cy.id, project) is None
        assert await repo.get_membership(session, owner.id, uuid.uuid4()) is None
        assert (await repo.get_member_project(session, bob.id, project)).id == project


async def suggest(factory, caller: User, q: str, project_id: uuid.UUID | None = None):
    async with factory() as session, session.begin():
        return await members.suggest_users(session, caller.id, q, DOMAINS, project_id)


async def test_suggestions_cap_at_eight_exclude_the_caller_and_carry_name_and_email(factory):
    caller = await signed_in(factory, "ann0@example.com", "Ann Zero")
    for n in range(1, 11):
        await signed_in(factory, f"ann{n:02d}@example.com", f"Ann {n:02d}")

    found = await suggest(factory, caller, "ann")

    assert len(found) == 8 and "ann0@example.com" not in [s.email for s in found]
    assert [s.email for s in found] == sorted(s.email for s in found)
    assert {f.name for f in found} <= {f"Ann {n:02d}" for n in range(1, 11)}


async def test_suggestions_match_email_name_and_name_word_prefixes_not_infixes(factory):
    caller = await signed_in(factory, "me@example.com")
    await signed_in(factory, "z1@example.com", "Maria Konopelska")
    await signed_in(factory, "konrad@example.com", "Someone")
    await signed_in(factory, "z3@example.com", "Mikonopelski")

    by_word = await suggest(factory, caller, "kon")
    by_email = await suggest(factory, caller, "KONR ")

    assert [s.email for s in by_word] == ["z1@example.com", "konrad@example.com"]  # by name: Maria, Someone
    assert [s.email for s in by_email] == ["konrad@example.com"]  # normalised; infix "Mikonopelski" never matches


async def test_suggestions_treat_percent_and_underscore_as_plain_characters(factory):
    """FR-PRJ-SUG-04."""

    caller = await signed_in(factory, "me@example.com")
    await signed_in(factory, "axb@example.com")
    await signed_in(factory, "a_b@example.com")
    await signed_in(factory, "a%bc@example.com")

    assert [s.email for s in await suggest(factory, caller, "a_b")] == ["a_b@example.com"]
    assert [s.email for s in await suggest(factory, caller, "a%b")] == ["a%bc@example.com"]
    assert await suggest(factory, caller, "a\\b") == []
    assert await suggest(factory, caller, "%%%") == []


async def test_suggestions_exclude_outside_domains_members_and_invited_emails(factory):
    owner = await signed_in(factory, "owner@example.com")
    await signed_in(factory, "ann.member@example.com")
    await signed_in(factory, "ann.free@example.com")
    await signed_in(factory, "ann.out@other.org")
    await signed_in(factory, "ann.sub@sub.example.com")
    project = await new_project(factory, owner)
    await add(factory, project, owner, "ann.member@example.com")
    async with factory() as session, session.begin():  # an invite of an email that has a user row (raced add)
        session.add(ProjectInvite(project_id=project, email_norm="ann.free@example.com", invited_by=owner.id))
    await signed_in(factory, "ann.late@example.com")

    anywhere = await suggest(factory, owner, "ann")
    inside = await suggest(factory, owner, "ann", project)

    assert [s.email for s in anywhere] == ["ann.free@example.com", "ann.late@example.com", "ann.member@example.com"]
    assert [s.email for s in inside] == ["ann.late@example.com"]


async def test_suggestions_collapse_duplicate_addresses(factory):
    caller = await signed_in(factory, "me@example.com")
    await signed_in(factory, "dup@example.com", "Dup", subject="s1")
    await signed_in(factory, "DUP@example.com", "Dup", subject="s2")

    assert len(await suggest(factory, caller, "dup")) == 1


async def test_address_display_names_are_returned_as_no_name(factory):
    caller = await signed_in(factory, "me@example.com")
    await signed_in(factory, "plain@example.com")  # display name = email

    assert await suggest(factory, caller, "plain") == [members.Suggestion(None, "plain@example.com")]


async def test_a_second_owner_row_is_refused_by_the_database(factory):
    """DM1 stays enforced beneath the repository: ``ux_project_members_one_owner``."""

    owner = await signed_in(factory, "owner@example.com")
    bob = await signed_in(factory, "bob@example.com")
    project = await new_project(factory, owner)

    async def second_owner() -> None:
        async with factory() as session, session.begin():
            session.add(ProjectMember(project_id=project, user_id=bob.id, role="owner"))
            await session.flush()

    with pytest.raises(IntegrityError):
        await second_owner()
