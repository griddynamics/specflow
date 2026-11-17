"""The collaborator routes of ``docs/projects/api.md``: members, invites, leave, rename, suggestions and Create Project
with collaborators, over the in-memory repository fakes (the real queries are the tier-2 tests of ``db.members``).

Every success and error row, the three standings of a caller (admin, collaborator, non-member), refused extra fields,
and the rule that no answer or log record echoes a submitted value (NFR-PRJ-01, FR-PRJ-ACC-01..03).

Sequence of an admin route::

    web -> current_user (upsert_user) -> project_member (get_membership) -> project_admin (role)
        -> 403 admin_required before the body is read further | route body in one transaction
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from db import members as members_module
from db import repositories as repo
from specflow_api_agent_support import FOREIGN, UNKNOWN_PROJECT, events, log_haystack
from specflow_api_members_support import ScriptedPlan, install
from specflow_api_support import bearer
from test_db_members_offline import NoDuplicateSession

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
OUTSIDER = "dave@elsewhere.org"


async def user_of(h: Any, email: str, name: str | None = None) -> Any:
    return await h.repo.upsert_user(None, SimpleNamespace(subject=f"user:{email}", email=email, name=name))


async def seeded(h: Any) -> SimpleNamespace:
    """Alice is the admin of ``Alpha``, Ben a collaborator; Carol is signed in (no member); Bob admins ``Foreign``."""

    alice = await user_of(h, ALICE, "Alice")
    ben = await user_of(h, BEN, "Ben")
    carol = await user_of(h, CAROL, "Carol Jones")
    bob = await user_of(h, "bob@example.com", "Bob")
    install(h.runtime, ScriptedPlan(no_plan=True))  # no plan: a removal has no claim to free
    alpha = await h.repo.create_project(None, alice.id, "Alpha")
    foreign = await h.repo.create_project(None, bob.id, "Foreign")
    await h.repo.add_member_or_invite(None, alpha.id, alice.id, BEN, ["example.com"])
    return SimpleNamespace(alice=alice, ben=ben, carol=carol, bob=bob, alpha=alpha, foreign=foreign)


async def call(h: Any, method: str, path: str, who: str = ALICE, body: Any = None) -> httpx.Response:
    return await h.client.request(method, f"/api/v1{path}", json=body, headers=bearer(who))


def state(h: Any) -> tuple[Any, ...]:
    return (
        set(h.repo.members),
        {k: dict(v) for k, v in h.repo.invites.items()},
        {pid: p.name for pid, p in h.repo.projects.items()},
    )


def error_of(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


# --- access: the three standings --------------------------------------------------------------------------------------


async def test_a_non_member_a_missing_project_and_a_malformed_id_are_one_byte_identical_404(harness):
    s = await seeded(harness)
    routes = [
        ("GET", "", None),
        ("GET", "/members", None),
        ("POST", "/members", {"email": CAROL}),
        ("DELETE", f"/members/{s.ben.id}", None),
        ("DELETE", f"/invites/{uuid.uuid4()}", None),
        ("POST", "/leave", None),
        ("PATCH", "", {"name": "X"}),
    ]
    before = state(harness)
    answers = [
        await call(harness, method, f"/projects/{project}{suffix}", ALICE, body)
        for method, suffix, body in routes
        for project in (str(s.foreign.id), UNKNOWN_PROJECT, "not-a-uuid")
    ]
    assert state(harness) == before
    assert {error_of(a) for a in answers} == {(404, "project_not_found")}
    assert len({(a.content, a.headers["content-type"]) for a in answers}) == 1


async def test_each_project_route_runs_exactly_one_membership_query(harness, monkeypatch):
    """NFR-PRJ-04: the dependency's ``get_membership`` is the only membership read of a project-scoped request
    (``get_member_project`` delegates to it). The SQL-level count on real rows is in the live suite."""

    s = await seeded(harness)
    seen: list[str] = []
    for name in ("get_membership", "get_member_project"):
        real = getattr(harness.repo, name)

        def spy(*args: Any, _name: str = name, _real: Any = real) -> Any:
            seen.append(_name)
            return _real(*args)

        monkeypatch.setattr(repo, name, spy)
    pid = f"/projects/{s.alpha.id}"
    routes = [
        ("GET", pid, None),
        ("GET", f"{pid}/members", None),
        ("POST", f"{pid}/members", {"email": CAROL}),
        ("DELETE", f"{pid}/members/{s.ben.id}", None),
        ("POST", f"{pid}/leave", None),  # Alice is the admin: refused, after the one membership read
        ("PATCH", pid, {"name": "Renamed"}),
        ("DELETE", f"{pid}/invites/{uuid.uuid4()}", None),
        ("POST", "/users/suggestions", {"q": "car", "project_id": str(s.alpha.id)}),
    ]
    for method, path, body in routes:
        seen.clear()
        answer = await call(harness, method, path, ALICE, body)
        assert answer.status_code < 500
        assert seen == ["get_membership"], (method, path, seen)


async def test_an_unexpected_invite_write_failure_is_a_500_with_no_email_in_any_log_record(
    harness, monkeypatch, caplog
):
    """B9: a non-duplicate integrity error on the invite insert reaches the generic handler as the typed
    ``InviteWriteError``; neither the answer nor any record (traceback text included) holds the email."""

    s = await seeded(harness)
    leaked = "leak@example.com"

    async def failing_add(session: Any, project_id: Any, invited_by: Any, email: str, _domains: Any) -> Any:
        return await members_module._add_invite(NoDuplicateSession(email), project_id, invited_by, email)

    monkeypatch.setattr(members_module, "add_member_or_invite", failing_add)
    with caplog.at_level(logging.INFO):
        answer = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": leaked})

    assert error_of(answer) == (500, "internal_error")
    assert leaked not in answer.text
    formatter = logging.Formatter()
    assert leaked not in "\n".join(formatter.format(r) for r in caplog.records if not r.name.startswith("httpx"))
    assert leaked not in log_haystack(caplog)
    assert [r for r in caplog.records if getattr(r, "event", None) == "api.request.failed"]


async def test_every_route_needs_a_token(harness):
    s = await seeded(harness)
    for method, path in (
        ("GET", f"/projects/{s.alpha.id}/members"),
        ("POST", "/users/suggestions"),
        ("PATCH", f"/projects/{s.alpha.id}"),
    ):
        answer = await harness.client.request(method, f"/api/v1{path}", json={})
        assert answer.status_code == 401


async def test_a_collaborator_gets_403_admin_required_on_every_admin_route_and_nothing_changes(harness):
    s = await seeded(harness)
    pid = s.alpha.id
    harness.repo.invites[pid] = {}
    await call(harness, "POST", f"/projects/{pid}/members", ALICE, {"email": "new@example.com"})
    invite = next(iter(harness.repo.invites[pid]))
    before = state(harness)
    answers = [
        await call(harness, "POST", f"/projects/{pid}/members", BEN, {"email": CAROL}),
        await call(harness, "POST", f"/projects/{pid}/members", BEN, {"not": "even valid"}),  # 403 wins over 422
        await call(harness, "DELETE", f"/projects/{pid}/members/{s.alice.id}", BEN),
        await call(harness, "DELETE", f"/projects/{pid}/invites/{invite}", BEN),
        await call(harness, "PATCH", f"/projects/{pid}", BEN, {"name": "Renamed"}),
        await call(harness, "PATCH", f"/projects/{pid}", BEN, {"name": ""}),
    ]
    assert state(harness) == before
    assert {error_of(a) for a in answers} == {(403, "admin_required")}


# --- list ------------------------------------------------------------------------------------------------------------


async def test_the_list_shows_the_admin_first_then_members_then_invites_to_any_member(harness):
    s = await seeded(harness)
    await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": CAROL})
    await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": "Zed@Example.com "})
    admin_view = await call(harness, "GET", f"/projects/{s.alpha.id}/members", ALICE)
    member_view = await call(harness, "GET", f"/projects/{s.alpha.id}/members", BEN)
    assert admin_view.status_code == member_view.status_code == 200
    body = admin_view.json()
    assert (body["role"], body["limit"]) == ("admin", 50)
    assert [(m["email"], m["role"], m["is_you"]) for m in body["members"]] == [
        (ALICE, "admin", True),
        (BEN, "collaborator", False),
        (CAROL, "collaborator", False),
    ]
    assert [i["email"] for i in body["invites"]] == ["zed@example.com"]
    assert set(body["members"][0]) == {"user_id", "name", "email", "role", "joined_at", "is_you"}
    assert set(body["invites"][0]) == {"id", "email", "invited_at"}
    assert (member_view.json()["role"], [m["is_you"] for m in member_view.json()["members"]]) == (
        "collaborator",
        [False, True, False],
    )


async def test_a_name_that_is_an_address_is_null(harness):
    s = await seeded(harness)
    await user_of(harness, "plain@example.com")  # display name falls back to the email
    await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": "plain@example.com"})
    body = (await call(harness, "GET", f"/projects/{s.alpha.id}/members", ALICE)).json()
    assert {m["email"]: m["name"] for m in body["members"]}["plain@example.com"] is None


# --- add -------------------------------------------------------------------------------------------------------------


async def test_adding_a_signed_in_email_adds_a_member_and_a_new_one_an_invite(harness, caplog):
    s = await seeded(harness)
    with caplog.at_level(logging.INFO):
        member = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": " CAROL@example.com"})
        invite = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": "New@Example.com"})
    assert member.status_code == invite.status_code == 201
    assert member.json()["kind"] == "member" and set(member.json()) == {"kind", "member"}
    assert member.json()["member"]["email"] == CAROL and member.json()["member"]["role"] == "collaborator"
    assert invite.json()["kind"] == "invite" and set(invite.json()) == {"kind", "invite"}
    assert invite.json()["invite"]["email"] == "new@example.com"
    added = events(caplog, "api.member.added")
    assert [(r.kind, hasattr(r, "member_user_id"), hasattr(r, "invite_id")) for r in added] == [
        ("member", True, False),
        ("invite", False, True),
    ]
    assert str(s.carol.id) == added[0].member_user_id
    assert "@" not in log_haystack(caplog)


@pytest.mark.parametrize(
    ("email", "status", "code"),
    [
        ("not an email", 422, "invalid_email"),
        ("a@@example.com", 422, "invalid_email"),
        ("", 422, "invalid_email"),
        ("x@" + "a" * 260 + ".com", 422, "invalid_email"),
        ("dave@elsewhere.org", 422, "email_domain_not_allowed"),
        ("someone@sub.example.com", 422, "email_domain_not_allowed"),
        (BEN, 409, "already_member"),
        (ALICE, 409, "already_member"),  # the admin's own email
        ("  BEN@example.com", 409, "already_member"),
    ],
)
async def test_an_add_that_breaks_a_rule_answers_its_code_with_a_fixed_message_and_changes_nothing(
    harness, email, status, code
):
    s = await seeded(harness)
    before = state(harness)
    response = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": email})
    assert error_of(response) == (status, code)
    assert state(harness) == before
    assert email.strip() == "" or email.strip().lower() not in response.text.lower()


async def test_a_second_invite_for_one_email_is_already_invited(harness):
    s = await seeded(harness)
    first = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": "new@example.com"})
    again = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": "NEW@example.com"})
    assert first.status_code == 201
    assert error_of(again) == (409, "already_invited")


async def test_the_fiftieth_person_fits_and_the_next_is_member_limit(harness):
    s = await seeded(harness)  # two people
    for number in range(47):
        added = await call(
            harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": f"p{number}@example.com"}
        )
        assert added.status_code == 201
    last = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": "p47@example.com"})
    over = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": CAROL})
    assert last.status_code == 201
    assert error_of(over) == (409, "member_limit")


@pytest.mark.parametrize(
    "body", [{}, {"email": CAROL, "role": "admin"}, {"email": 5}, {"email": None}, [], {"email": ["a@example.com"]}]
)
async def test_the_add_body_forbids_extra_fields_and_wrong_types(harness, body):
    s = await seeded(harness)
    before = state(harness)
    response = await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, body)
    assert error_of(response) == (422, "validation_failed")
    assert all(set(item) == {"loc", "type"} for item in response.json()["error"]["details"]["errors"])
    assert state(harness) == before


# --- remove and cancel -----------------------------------------------------------------------------------------------


async def test_the_admin_removes_a_member_then_that_person_sees_404(harness, caplog):
    s = await seeded(harness)
    pid = s.alpha.id
    with caplog.at_level(logging.INFO):
        removed = await call(harness, "DELETE", f"/projects/{pid}/members/{s.ben.id}", ALICE)
    assert (removed.status_code, removed.json()) == (200, {"claims_released": 0, "claims_release_failed": False})
    assert error_of(await call(harness, "GET", f"/projects/{pid}/members", BEN)) == (404, "project_not_found")
    gone = events(caplog, "api.member.removed")
    assert [(r.user_id, r.project_id, r.member_user_id) for r in gone] == [(str(s.alice.id), str(pid), str(s.ben.id))]
    assert "@" not in log_haystack(caplog)


@pytest.mark.parametrize(
    ("who", "target", "expected"),
    [
        ("alice", "alice", (409, "cannot_remove_admin")),
        ("alice", "stranger", (404, "member_not_found")),
        ("alice", "bob", (404, "member_not_found")),  # a user of another project
        ("alice", "garbage", (404, "member_not_found")),
    ],
)
async def test_a_removal_that_breaks_a_rule_answers_its_code(harness, who, target, expected):
    s = await seeded(harness)
    ids = {"alice": s.alice.id, "stranger": uuid.uuid4(), "bob": s.bob.id, "garbage": "not-a-uuid"}
    before = state(harness)
    response = await call(harness, "DELETE", f"/projects/{s.alpha.id}/members/{ids[target]}", ALICE)
    assert (response.status_code, response.json()["error"]["code"]) == expected
    assert state(harness) == before


async def test_the_admin_cancels_an_invite_and_a_repeat_is_404(harness, caplog):
    s = await seeded(harness)
    pid = s.alpha.id
    await call(harness, "POST", f"/projects/{pid}/members", ALICE, {"email": "new@example.com"})
    invite = next(iter(harness.repo.invites[pid]))
    with caplog.at_level(logging.INFO):
        cancelled = await call(harness, "DELETE", f"/projects/{pid}/invites/{invite}", ALICE)
    again = await call(harness, "DELETE", f"/projects/{pid}/invites/{invite}", ALICE)
    garbage = await call(harness, "DELETE", f"/projects/{pid}/invites/not-a-uuid", ALICE)
    assert (cancelled.status_code, cancelled.content) == (204, b"")
    assert error_of(again) == error_of(garbage) == (404, "invite_not_found")
    assert [r.invite_id for r in events(caplog, "api.invite.cancelled")] == [str(invite)]
    assert "@" not in log_haystack(caplog)


async def test_an_invite_of_another_project_is_not_found(harness):
    s = await seeded(harness)
    await call(harness, "POST", f"/projects/{s.foreign.id}/members", "bob@example.com", {"email": "new@example.com"})
    invite = next(iter(harness.repo.invites[s.foreign.id]))
    response = await call(harness, "DELETE", f"/projects/{s.alpha.id}/invites/{invite}", ALICE)
    assert error_of(response) == (404, "invite_not_found")
    assert invite in harness.repo.invites[s.foreign.id]


# --- leave -----------------------------------------------------------------------------------------------------------


async def test_a_collaborator_leaves_and_the_admin_cannot(harness, caplog):
    s = await seeded(harness)
    pid = s.alpha.id
    with caplog.at_level(logging.INFO):
        left = await call(harness, "POST", f"/projects/{pid}/leave", BEN)
    admin = await call(harness, "POST", f"/projects/{pid}/leave", ALICE)
    after = await call(harness, "POST", f"/projects/{pid}/leave", BEN)
    assert (left.status_code, left.json()) == (200, {"claims_released": 0, "claims_release_failed": False})
    assert error_of(admin) == (409, "admin_cannot_leave")
    assert error_of(after) == (404, "project_not_found")
    assert [(r.user_id, r.member_user_id) for r in events(caplog, "api.member.left")] == [
        (str(s.ben.id), str(s.ben.id))
    ]
    assert (pid, s.alice.id) in harness.repo.members


# --- rename ----------------------------------------------------------------------------------------------------------


async def test_the_admin_renames_with_the_create_name_rule(harness, caplog):
    s = await seeded(harness)
    with caplog.at_level(logging.INFO):
        response = await call(harness, "PATCH", f"/projects/{s.alpha.id}", ALICE, {"name": "  New name  "})
    assert response.status_code == 200
    assert set(response.json()) == {"id", "name", "created_at"} and response.json()["name"] == "New name"
    assert harness.repo.projects[s.alpha.id].name == "New name"
    assert [(r.user_id, r.project_id) for r in events(caplog, "api.project.renamed")] == [
        (str(s.alice.id), str(s.alpha.id))
    ]
    longest = await call(harness, "PATCH", f"/projects/{s.alpha.id}", ALICE, {"name": "n" * 200})
    assert longest.status_code == 200


@pytest.mark.parametrize(
    "body",
    [{"name": ""}, {"name": "   "}, {"name": "n" * 201}, {}, {"name": "ok", "extra": 1}, {"name": 5}, {"name": None}],
)
async def test_a_rename_that_breaks_the_rule_is_422_and_changes_nothing(harness, body):
    s = await seeded(harness)
    response = await call(harness, "PATCH", f"/projects/{s.alpha.id}", ALICE, body)
    assert error_of(response) == (422, "validation_failed")
    assert harness.repo.projects[s.alpha.id].name == "Alpha"
    assert all(set(item) == {"loc", "type"} for item in response.json()["error"]["details"]["errors"])
    assert "n" * 20 not in response.text


# --- suggestions -----------------------------------------------------------------------------------------------------


async def test_suggestions_match_by_name_or_email_exclude_members_and_never_leave_a_trace(harness, caplog):
    s = await seeded(harness)
    await user_of(harness, "carla@example.com", "Carla Smith")
    await user_of(harness, OUTSIDER, "Carl Outside")  # outside the allowed domains
    with caplog.at_level(logging.INFO):
        found = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "  CAR "})
        scoped = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "car", "project_id": str(s.alpha.id)})
        by_word = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "jon"})
        nothing = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "zzzz"})
    assert found.status_code == 200 and set(found.json()) == {"items"}
    assert [(i["name"], i["email"]) for i in found.json()["items"]] == [
        ("Carla Smith", "carla@example.com"),
        ("Carol Jones", CAROL),
    ]
    assert all(set(i) == {"name", "email"} for i in found.json()["items"])
    assert scoped.json() == found.json()  # Carol and Carla are not members yet
    assert [i["email"] for i in by_word.json()["items"]] == [CAROL]
    assert nothing.json() == {"items": []}
    await call(harness, "POST", f"/projects/{s.alpha.id}/members", ALICE, {"email": CAROL})
    after = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "car", "project_id": str(s.alpha.id)})
    assert [i["email"] for i in after.json()["items"]] == ["carla@example.com"]
    assert [r.count for r in events(caplog, "api.users.suggested")][:4] == [2, 2, 1, 0]
    haystack = log_haystack(caplog)
    assert "@" not in haystack and "zzzz" not in haystack and "jon" not in haystack.replace("json", "")


async def test_a_suggestion_with_no_name_is_null_and_the_caller_is_excluded(harness):
    await seeded(harness)
    await user_of(harness, "nameless@example.com")
    response = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "nameless"})
    assert response.json()["items"] == [{"name": None, "email": "nameless@example.com"}]
    own = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "alice"})
    assert own.json() == {"items": []}


@pytest.mark.parametrize("q", ["", "ab", "  a  ", "x" * 255])
async def test_the_suggestion_text_is_three_to_254_characters(harness, q):
    await seeded(harness)
    response = await call(harness, "POST", "/users/suggestions", ALICE, {"q": q})
    assert error_of(response) == (422, "validation_failed")
    assert response.json()["error"]["details"]["errors"][0]["loc"] == ["body", "q"]
    assert "xxxx" not in response.text


async def test_the_longest_suggestion_text_is_accepted(harness):
    await seeded(harness)
    assert (await call(harness, "POST", "/users/suggestions", ALICE, {"q": "x" * 254})).status_code == 200


async def test_a_suggestion_scope_the_caller_is_not_a_member_of_is_404(harness):
    s = await seeded(harness)
    for project in (str(s.foreign.id), UNKNOWN_PROJECT, "garbage", FOREIGN):
        response = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "car", "project_id": project})
        assert error_of(response) == (404, "project_not_found")


async def test_the_suggestion_body_forbids_extra_fields(harness):
    await seeded(harness)
    response = await call(harness, "POST", "/users/suggestions", ALICE, {"q": "car", "limit": 100})
    assert error_of(response) == (422, "validation_failed")


# --- create with collaborators ---------------------------------------------------------------------------------------


async def test_create_without_collaborators_behaves_as_before(harness):
    await seeded(harness)
    plain = await call(harness, "POST", "/projects", ALICE, {"name": "Plain"})
    empty = await call(harness, "POST", "/projects", ALICE, {"name": "Empty", "collaborators": []})
    assert plain.status_code == empty.status_code == 201
    assert set(plain.json()) == {"id", "name", "created_at"}
    for response in (plain, empty):
        pid = uuid.UUID(response.json()["id"])
        assert {k for k in harness.repo.members if k[0] == pid} == {(pid, harness.repo.users[f"user:{ALICE}"].id)}


async def test_create_adds_signed_in_emails_as_members_and_the_rest_as_invites_dropping_duplicates_and_self(harness):
    s = await seeded(harness)
    emails = [CAROL, "Carol@Example.com", ALICE.upper(), "new@example.com", " NEW@example.com", BEN]
    response = await call(harness, "POST", "/projects", ALICE, {"name": "Team", "collaborators": emails})
    assert response.status_code == 201 and set(response.json()) == {"id", "name", "created_at"}
    pid = uuid.UUID(response.json()["id"])
    assert {k[1] for k in harness.repo.members if k[0] == pid} == {s.alice.id, s.carol.id, s.ben.id}
    assert harness.repo.roles[(pid, s.alice.id)] == "admin"
    assert [e for e, _, _ in harness.repo.invites[pid].values()] == ["new@example.com"]


async def test_create_with_a_bad_email_answers_422_with_the_index_and_writes_nothing(harness, caplog):
    await seeded(harness)
    before = state(harness)
    body = {"name": "Team", "collaborators": [CAROL, "nope", "ok@example.com", OUTSIDER, "x@@example.com"]}
    with caplog.at_level(logging.INFO):
        response = await call(harness, "POST", "/projects", ALICE, body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["details"]["errors"] == [
        {"loc": ["body", "collaborators", "1"], "type": "invalid_email"},
        {"loc": ["body", "collaborators", "3"], "type": "email_domain_not_allowed"},
        {"loc": ["body", "collaborators", "4"], "type": "invalid_email"},
    ]
    assert state(harness) == before
    assert "nope" not in response.text and OUTSIDER not in response.text
    assert "@" not in log_haystack(caplog)


async def test_create_takes_at_most_49_emails(harness):
    await seeded(harness)
    ok = [f"p{n}@example.com" for n in range(49)]
    fits = await call(harness, "POST", "/projects", ALICE, {"name": "Full", "collaborators": ok})
    before = state(harness)
    over = await call(harness, "POST", "/projects", ALICE, {"name": "Over", "collaborators": [*ok, "p49@example.com"]})
    assert fits.status_code == 201
    assert over.status_code == 422
    assert over.json()["error"]["details"]["errors"] == [{"loc": ["body", "collaborators"], "type": "too_long"}]
    assert state(harness) == before


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "collaborators": "a@example.com"},
        {"name": "x", "collaborators": [5]},
        {"name": "x", "collaborators": {"a": 1}},
        {"name": "x", "collaborator_emails": []},
        {"collaborators": []},
    ],
)
async def test_create_refuses_a_malformed_body(harness, body):
    await seeded(harness)
    before = state(harness)
    response = await call(harness, "POST", "/projects", ALICE, body)
    assert error_of(response) == (422, "validation_failed")
    assert state(harness) == before


async def test_an_invited_email_joins_when_the_person_first_signs_in(harness):
    """FR-PRJ-INV-01/02 at the route level: the web ``/me`` adopts the invite through ``upsert_user``."""

    s = await seeded(harness)
    created = await call(harness, "POST", "/projects", ALICE, {"name": "Team", "collaborators": ["late@example.com"]})
    pid = uuid.UUID(created.json()["id"])
    assert (await call(harness, "GET", f"/projects/{pid}/members", "late@example.com")).status_code == 200
    members = (await call(harness, "GET", f"/projects/{pid}/members", ALICE)).json()
    assert [m["email"] for m in members["members"]] == [ALICE, "late@example.com"]
    assert members["invites"] == [] and s.alice.id
