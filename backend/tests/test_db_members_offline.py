"""Offline tier of ``db.members`` and the new ``db.repositories`` functions: statement shape, outcomes, the three
``upsert_user`` return paths, log content, and parity of the repo fakes (no database)."""

from __future__ import annotations

import inspect
import traceback
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from db import members
from db import repositories as repo
from db_recorder_support import Recorder
from logger_harness import events, fields
from test_specflow_api_fakes import MEMBER_FUNCTIONS, FakeRepo

PROJECT, USER = uuid.UUID(int=1), uuid.UUID(int=2)


def sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.asyncpg.dialect()))


class Principal:
    def __init__(self, email: str, subject: str = "sub-1", name: str | None = None) -> None:
        self.subject, self.email, self.name = subject, email, name


# Membership: one query ----------------------------------------------------------


async def test_get_membership_is_one_query_and_returns_project_and_role() -> None:
    recorder = Recorder([("project-row", "collaborator")])

    found = await repo.get_membership(cast(Any, recorder), USER, PROJECT)

    assert found == ("project-row", "collaborator")
    assert len(recorder.sql) == 1  # NFR-PRJ-04
    assert "project_members.role" in recorder.sql[0]


async def test_get_member_project_delegates_with_one_query_and_none_for_a_non_member() -> None:
    hit, miss = Recorder([("project-row", "owner")]), Recorder()

    assert await repo.get_member_project(cast(Any, hit), USER, PROJECT) == "project-row"
    assert await repo.get_member_project(cast(Any, miss), USER, PROJECT) is None
    assert len(hit.sql) == len(miss.sql) == 1


# Adoption -------------------------------------------------------------------------


class AdoptionSession:
    """Answers the user lookup, remembers the adoption statement and ``flush``."""

    def __init__(self, existing: SimpleNamespace | None, *, raced: bool = False) -> None:
        self.existing, self.raced = existing, raced
        self.statements: list[str] = []
        self.lookups = 0

    async def scalar(self, _statement: Any) -> Any:
        self.lookups += 1
        if self.raced:
            return None if self.lookups == 1 else self.existing
        return self.existing

    async def scalars(self, statement: Any) -> Any:
        self.statements.append(sql(statement))
        return SimpleNamespace(all=list)

    async def flush(self) -> None:
        return None

    def add(self, _row: Any) -> None:
        return None

    def begin_nested(self) -> Any:
        session = self

        class Nested:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, kind: Any, *_: Any) -> bool:
                if session.raced:
                    raise IntegrityError("insert", {}, Exception("duplicate"))
                return False

        return Nested()


def user_row() -> SimpleNamespace:
    return SimpleNamespace(id=USER, email="old@example.com", display_name="x")


async def test_adoption_is_one_core_statement_with_a_delete_cte_and_conflict_do_nothing() -> None:
    session = AdoptionSession(None)

    await repo.adopt_invites(cast(Any, session), USER, " Bob@Example.com ")

    assert len(session.statements) == 1
    text = " ".join(session.statements[0].split()).replace("( ", "(")
    assert text.startswith("WITH adopted AS (DELETE FROM project_invites WHERE project_invites.email_norm = $")
    assert "RETURNING project_invites.project_id)" in text
    assert "INSERT INTO project_members (project_id, user_id, role) SELECT adopted.project_id" in text
    assert "ON CONFLICT DO NOTHING RETURNING project_members.project_id" in text


async def test_a_rejected_email_adopts_nothing_and_does_not_fail() -> None:
    session = AdoptionSession(None)

    assert await repo.adopt_invites(cast(Any, session), USER, "x" * 300 + "@example.com") == []
    assert await repo.adopt_invites(cast(Any, session), USER, "not an address") == []
    assert session.statements == []


@pytest.mark.parametrize("path", ["existing", "new", "raced"])
async def test_every_upsert_user_return_path_runs_adoption_once(path: str) -> None:
    existing = user_row()
    session = AdoptionSession(existing if path == "existing" else None)
    if path == "raced":
        session = AdoptionSession(existing, raced=True)

    user = await repo.upsert_user(cast(Any, session), Principal("bob@example.com"))

    assert len(session.statements) == 1
    assert user.email == ("old@example.com" if path == "raced" else "bob@example.com")


async def test_adoption_logs_ids_only(caplog: pytest.LogCaptureFixture) -> None:
    project_ids = [uuid.UUID(int=10), uuid.UUID(int=11)]

    class Session:
        async def scalars(self, _statement: Any) -> Any:
            return SimpleNamespace(all=lambda: project_ids)

    with caplog.at_level("INFO"):
        assert await repo.adopt_invites(cast(Any, Session()), USER, "bob@example.com") == project_ids

    records = events(caplog, "db.invite.adopted")
    assert [fields(r)["project_id"] for r in records] == [str(p) for p in project_ids]
    assert all("@" not in str(fields(r)) and fields(r)["user_id"] == str(USER) for r in records)


# Remove and leave --------------------------------------------------------------------


@pytest.mark.parametrize("function", [members.remove_member, members.leave_project])
async def test_a_member_who_goes_stops_being_the_jira_acting_account_in_the_same_transaction(function) -> None:
    recorder = Recorder([PROJECT], ["collaborator"])

    await function(cast(Any, recorder), PROJECT, USER)

    lock, _role, delete_member, clear = (" ".join(text.split()) for text in recorder.sql)
    assert lock.endswith("FOR UPDATE") and delete_member.startswith("DELETE FROM project_members")
    assert clear == (
        "UPDATE jira_targets SET connected_user_id=$1::UUID WHERE jira_targets.project_id = $2::UUID "
        "AND jira_targets.connected_user_id = $3::UUID"
    )


@pytest.mark.parametrize("function", [members.remove_member, members.leave_project])
async def test_the_owner_cannot_go_so_the_acting_account_is_kept(function) -> None:
    recorder = Recorder([PROJECT], ["owner"])

    with pytest.raises(members.MemberError):
        await function(cast(Any, recorder), PROJECT, USER)

    assert not any("jira_targets" in text for text in recorder.sql)


# Typed errors ----------------------------------------------------------------------


def test_every_error_carries_the_api_code() -> None:
    codes = {
        members.InvalidEmailError: "invalid_email",
        members.EmailDomainNotAllowedError: "email_domain_not_allowed",
        members.AlreadyMemberError: "already_member",
        members.AlreadyInvitedError: "already_invited",
        members.MemberLimitError: "member_limit",
        members.MemberNotFoundError: "member_not_found",
        members.CannotRemoveOwnerError: "cannot_remove_owner",
        members.InviteNotFoundError: "invite_not_found",
        members.OwnerCannotLeaveError: "owner_cannot_leave",
        members.ProjectMissingError: "project_not_found",
    }
    for error, code in codes.items():
        assert error().code == code and str(error()) == code


async def test_add_refuses_a_bad_or_foreign_email_before_any_query() -> None:
    recorder = Recorder()
    with pytest.raises(members.InvalidEmailError):
        await members.add_member_or_invite(cast(Any, recorder), PROJECT, USER, "nope", {"example.com"})
    with pytest.raises(members.EmailDomainNotAllowedError):
        await members.add_member_or_invite(cast(Any, recorder), PROJECT, USER, "a@other.org", {"example.com"})
    assert recorder.sql == []


async def test_add_at_the_limit_is_refused_after_locking_the_project() -> None:
    class Session:
        def __init__(self) -> None:
            self.sql: list[str] = []
            self.answers = [PROJECT, None, 30, 20]

        async def scalar(self, statement: Any) -> Any:
            self.sql.append(sql(statement))
            return self.answers.pop(0)

    session = Session()
    with pytest.raises(members.MemberLimitError):
        await members.add_member_or_invite(cast(Any, session), PROJECT, USER, "a@example.com", {"example.com"})
    assert session.sql[0].endswith("FOR UPDATE")
    assert len(session.sql) == 4  # lock, user lookup (none), member count, invite count


class NoDuplicateSession:
    """An invite insert that fails with a driver error holding the SQL parameters, and no existing duplicate."""

    def __init__(self, leaked: str) -> None:
        self.leaked = leaked

    async def scalar(self, _statement: Any) -> Any:
        return None  # no duplicate row: the failure is some other integrity error

    def begin_nested(self) -> Any:
        leaked = self.leaked

        class Nested:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, kind: Any, *_: Any) -> bool:
                params = f"[parameters: ('{leaked}',)]"
                raise IntegrityError("INSERT INTO project_invites", {}, Exception(f"violates a check {params}"))

        return Nested()


async def test_an_unexpected_invite_integrity_error_becomes_a_typed_error_without_the_email() -> None:
    """B9: the driver error (SQL parameters, the email) is neither raised, chained nor in the traceback text."""

    leaked = "leak@example.com"
    with pytest.raises(members.InviteWriteError) as raised:
        await members._add_invite(cast(Any, NoDuplicateSession(leaked)), PROJECT, USER, leaked)

    error = raised.value
    assert error.__cause__ is None and error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    assert leaked not in rendered and leaked not in str(error) and not isinstance(error, members.MemberError)


async def test_suggestion_statement_escapes_like_wildcards_and_binds_the_query() -> None:
    compiled = members.suggestion_statement(USER, "a_b%\\", {"example.com"}, None).compile(
        dialect=postgresql.asyncpg.dialect()
    )
    values = [v for v in compiled.params.values() if isinstance(v, str)]
    assert "a\\_b\\%\\\\%" in values and "% a\\_b\\%\\\\%" in values
    assert "ESCAPE '\\'" in str(compiled)
    assert "a_b" not in str(compiled)  # bound, never inlined


def test_suggestion_statement_with_a_project_excludes_members_and_invites() -> None:
    text = sql(members.suggestion_statement(USER, "ann", {"example.com"}, PROJECT))
    assert "project_members" in text and "project_invites" in text
    assert "project_members" not in sql(members.suggestion_statement(USER, "ann", {"example.com"}, None))


async def test_suggest_users_collapses_duplicate_addresses_and_caps_at_eight() -> None:
    rows = [(f"Ann {n}", f"ann{n // 2}@example.com") for n in range(40)]
    recorder = Recorder(rows)

    found = await members.suggest_users(cast(Any, recorder), USER, "  ANN", {"example.com"})

    assert len(found) == 8 and len({s.email for s in found}) == 8
    assert members.shown_name("a@b.org", "x@y.org") is None and members.shown_name("Ann", "a@b.org") == "Ann"


# Fake parity -----------------------------------------------------------------------


def _params(function: Any) -> list[str]:
    return [p for p in inspect.signature(function).parameters if p not in ("self", "_session", "session")]


@pytest.mark.parametrize("name", MEMBER_FUNCTIONS)
def test_member_fakes_match_the_real_signatures(name: str) -> None:
    assert _params(getattr(FakeRepo, name)) == _params(getattr(members, name))
    assert inspect.iscoroutinefunction(getattr(members, name))


@pytest.mark.parametrize("name", ["upsert_user", "create_project", "get_membership", "get_member_project"])
def test_repo_fakes_match_the_real_signatures(name: str) -> None:
    assert _params(getattr(FakeRepo, name)) == _params(getattr(repo, name))


async def test_the_fake_adopts_on_every_upsert_and_skips_a_rejected_email(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRepo()
    fake.install(monkeypatch)
    owner = await repo.upsert_user(None, Principal("owner@example.com", "s-a"))
    project = await repo.create_project(None, owner.id, "P")
    await members.add_member_or_invite(None, project.id, owner.id, "Cy@Example.com", {"example.com"})
    first = await repo.upsert_user(None, Principal("cy@example.com", "s-c"))  # new user adopts
    assert (project.id, first.id) in fake.members and fake.invites[project.id] == {}
    await members.add_member_or_invite(None, project.id, owner.id, "late@example.com", {"example.com"})
    late = await repo.upsert_user(None, Principal("late@example.com", "s-l"))
    assert fake.roles[(project.id, late.id)] == "collaborator"
    other = await repo.create_project(None, owner.id, "Q", ["cy@example.com", "new@example.com"])
    assert (other.id, first.id) in fake.members and len(fake.invites[other.id]) == 1
    await repo.upsert_user(None, Principal("x" * 300 + "@example.com", "s-x"))  # signs in, adopts nothing
