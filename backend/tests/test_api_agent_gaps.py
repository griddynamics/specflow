"""The ``list_gaps`` agent route (``GET /agent/gaps``; docs/mcp-server/tools.md "list_gaps", FR-GWEB-03), the two
routes of ``answer_gap`` (``GET /agent/gap-card``, ``POST /agent/gap-answer``), the routes of ``browse_gaps``
(``GET /agent/gap-inbox``: the rows of the web Gaps screen) and ``open_gap`` (``GET /agent/gap-detail``: one gap whole)
and the open gaps the agent briefs show, over the shop gap corpus after a gap run (``gap_agent_support``)."""

from __future__ import annotations

import pytest

from agent_contracts import AGENT_ROUTES, GAP_CARD_ROUTE
from gap_acceptance_support import Planted
from gap_agent_support import ALPHA, API_LOGIC, API_STORY, QUESTION, GapRig, serve_gaps
from graph.gap.gaps import gap_uuid
from specflow_api_agent_support import FOREIGN, agent_env

GAPS = AGENT_ROUTES["list_gaps"]
ANSWER = AGENT_ROUTES["answer_gap"]
INBOX = AGENT_ROUTES["browse_gaps"]
DETAIL = AGENT_ROUTES["open_gap"]
WEB = "https://app.example.test"


@pytest.fixture
async def gaps(monkeypatch, valid_env):
    async with serve_gaps(monkeypatch, agent_env(valid_env)) as rig:
        yield rig


@pytest.fixture
async def cards(monkeypatch, valid_env):
    async with serve_gaps(monkeypatch, agent_env(valid_env, PUBLIC_URL=WEB)) as rig:
        yield rig


def resolve(rig: GapRig, gap_id: str) -> None:
    """Marks the gap resolved, as an apply build would leave it (the answer routes come with session 08)."""

    node = rig.world.executor.nodes[("Gap", gap_uuid(ALPHA, gap_id))]
    node.update(status="resolved", resolution="answered", resolved_at=node["created_at"])


async def test_list_gaps_by_story_and_by_spec_ids_agree(gaps: GapRig) -> None:
    by_story = (await gaps.get(GAPS, project_id=ALPHA, story_id=API_STORY)).json()
    by_node = (await gaps.get(GAPS, project_id=ALPHA, spec_ids=[Planted.PLACE_ORDER])).json()

    assert by_story["project_id"] == ALPHA and by_story["next_cursor"] is None
    assert [item["gap_id"] for item in by_story["items"]] == [item["gap_id"] for item in by_node["items"]]
    assert all(Planted.PLACE_ORDER in item["about"] for item in by_story["items"])
    ranks = [("blocker", "major", "minor").index(item["severity"]) for item in by_story["items"]]
    assert ranks == sorted(ranks)


async def test_list_gaps_pages_with_its_cursor(gaps: GapRig) -> None:
    about = sorted({spec_id for gap_id in gaps.world.gap_nodes() for spec_id in gaps.world.about(gap_id)})[:20]
    every = (await gaps.get(GAPS, project_id=ALPHA, spec_ids=about)).json()["items"]
    first = (await gaps.get(GAPS, project_id=ALPHA, spec_ids=about, limit=1)).json()
    rest = (await gaps.get(GAPS, project_id=ALPHA, spec_ids=about, cursor=first["next_cursor"])).json()

    assert len(every) >= 2 and first["next_cursor"] is not None
    assert [item["gap_id"] for item in (*first["items"], *rest["items"])] == [item["gap_id"] for item in every]


@pytest.mark.parametrize(
    "params",
    [{}, {"story_id": API_STORY, "spec_ids": [Planted.PLACE_ORDER]}],
    ids=["neither", "both"],
)
async def test_list_gaps_takes_exactly_one_of_story_id_and_spec_ids(gaps: GapRig, params: dict) -> None:
    response = await gaps.get(GAPS, project_id=ALPHA, **params)

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_arguments"


async def test_list_gaps_refuses_a_bad_cursor_and_an_unknown_story(gaps: GapRig) -> None:
    bad = await gaps.get(GAPS, project_id=ALPHA, spec_ids=[Planted.PLACE_ORDER], cursor="not-a-cursor")
    long = await gaps.get(GAPS, project_id=ALPHA, spec_ids=[Planted.PLACE_ORDER], cursor="x" * 513)
    story = await gaps.get(GAPS, project_id=ALPHA, story_id="STORY::NOPE")

    assert (bad.status_code, bad.json()["error"]["code"]) == (400, "invalid_cursor")
    assert (long.status_code, long.json()["error"]["code"]) == (400, "invalid_cursor")
    assert (story.status_code, story.json()["error"]["code"]) == (404, "story_not_found")


async def test_list_gaps_refuses_a_foreign_project(gaps: GapRig) -> None:
    response = await gaps.get(GAPS, project_id=FOREIGN, spec_ids=[Planted.PLACE_ORDER])

    assert (response.status_code, response.json()["error"]["code"]) == (404, "project_not_found")


async def test_list_gaps_lists_resolved_gaps_only_when_asked(gaps: GapRig) -> None:
    gap_id = gaps.question_gap()
    resolve(gaps, gap_id)

    default = (await gaps.get(GAPS, project_id=ALPHA, spec_ids=[Planted.PLACE_ORDER])).json()["items"]
    asked = (await gaps.get(GAPS, project_id=ALPHA, spec_ids=[Planted.PLACE_ORDER], status=["resolved"])).json()[
        "items"
    ]

    assert gap_id not in {item["gap_id"] for item in default}
    assert [(item["gap_id"], item["status"]) for item in asked] == [(gap_id, "resolved")]


async def test_without_a_gap_runner_the_project_has_no_gaps(gaps: GapRig) -> None:
    gaps.rig.runtime.gaps = None

    response = await gaps.get(GAPS, project_id=ALPHA, spec_ids=[Planted.PLACE_ORDER])

    assert response.status_code == 200 and response.json()["items"] == []


async def test_the_task_brief_lists_the_open_gaps_about_the_nodes_the_task_implements(gaps: GapRig) -> None:
    gap_id = gaps.question_gap()

    brief = (await gaps.get(AGENT_ROUTES["get_task_brief"], project_id=ALPHA, task_id=API_LOGIC)).json()
    other = (await gaps.get(AGENT_ROUTES["get_task_brief"], project_id=ALPHA, task_id="TASK::A::UI_SCREEN")).json()

    found = {item["finding_id"]: item for item in brief["findings"]}
    assert found[gap_id]["severity"] == "major" and found[gap_id]["author"] == {
        "name": None,
        "you": False,
        "agent_label": None,
    }
    assert brief["findings"][0]["blocking"] or not any(item["blocking"] for item in brief["findings"])
    assert other["findings"] == []


async def test_the_story_preview_shows_the_gap_with_its_severity(gaps: GapRig) -> None:
    gap_id = gaps.question_gap()

    preview = (await gaps.get(AGENT_ROUTES["preview_story"], project_id=ALPHA, story_id=API_STORY)).json()

    assert {item["finding_id"]: item["severity"] for item in preview["findings"]}[gap_id] == "major"


# --- the card text: what the chat shows before the dialog -------------------------------------------------------


async def test_the_card_text_holds_the_question_the_options_the_quotes_and_the_web_link(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    whole = await cards.gaps.get_gap(ALPHA, gap_id)

    item = (await cards.get(GAP_CARD_ROUTE, project_id=ALPHA, gap_id=gap_id)).json()

    assert (item["severity"], item["kind"], item["title"]) == (whole.severity, whole.kind, whole.title)
    assert item["web_url"] == f"{WEB}/projects/{ALPHA}"
    card = item["card"]
    assert card.startswith(f"**{whole.title}** ({whole.severity} open question, found by analysis, `{gap_id}`)")
    assert f"**Question:** {QUESTION}" in card
    labels = [option.label for option in sorted(whole.options, key=lambda option: option.rank)]
    assert labels and all(f"- **{chr(65 + index)}) {label}" in card for index, label in enumerate(labels))
    assert card.count("(recommended)") == 1 and "- **Other**: your own answer" in card
    assert '> orders-spec.md, "Place order", line' in card and f': "{QUESTION}"' in card
    assert card.endswith(f"Web card: {item['web_url']}")


async def test_without_the_web_app_address_the_card_has_no_link(gaps: GapRig) -> None:
    item = (await gaps.get(GAP_CARD_ROUTE, project_id=ALPHA, gap_id=gaps.question_gap())).json()

    assert item["web_url"] is None and "Web card:" not in item["card"]


async def test_the_card_names_its_assignee(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    before = (await cards.get(GAP_CARD_ROUTE, project_id=ALPHA, gap_id=gap_id)).json()
    await cards.gaps.assign_gap(ALPHA, gap_id, cards.rig.alice_id, by=cards.rig.alice_id)

    after = (await cards.get(GAP_CARD_ROUTE, project_id=ALPHA, gap_id=gap_id)).json()

    assert before["assignee"] is None and "**Assigned to:** nobody" in before["card"]
    assert after["assignee"] == {"name": "Alice", "you": True, "agent_label": None}
    assert "**Assigned to:** you" in after["card"]


# --- answer_gap's routes: the card of the dialog and the answer the person sent ----------------------------------


async def test_the_gap_card_has_the_parts_the_dialog_shows(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    whole = await cards.gaps.get_gap(ALPHA, gap_id)

    card = (await cards.get(GAP_CARD_ROUTE, project_id=ALPHA, gap_id=gap_id)).json()

    assert (card["gap_id"], card["question"], card["severity"], card["origin"]) == (
        gap_id,
        QUESTION,
        whole.severity,
        "analysis",
    )
    assert [(o["code"], o["rank"], o["recommended"]) for o in card["options"]] == [
        (chr(65 + index), option.rank, option.recommended)
        for index, option in enumerate(sorted(whole.options, key=lambda option: option.rank))
    ]
    assert card["updated_at"].startswith(whole.updated_at.strftime("%Y-%m-%dT%H:%M:%S"))
    assert card["web_url"] == f"{WEB}/projects/{ALPHA}" and card["card"].startswith("**")


async def test_the_gap_card_is_only_for_a_gap_that_waits_for_an_answer(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    resolve(cards, gap_id)

    closed = await cards.get(GAP_CARD_ROUTE, project_id=ALPHA, gap_id=gap_id)
    unknown = await cards.get(GAP_CARD_ROUTE, project_id=ALPHA, gap_id="GAP::000000000000")
    foreign = await cards.get(GAP_CARD_ROUTE, project_id=FOREIGN, gap_id=gap_id)

    assert (closed.status_code, closed.json()["error"]["code"]) == (409, "gap_wrong_state")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "gap_not_found")
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (404, "project_not_found")


async def chosen(rig: GapRig, gap_id: str, **answer: object) -> dict[str, object]:
    whole = await rig.gaps.get_gap(ALPHA, gap_id)
    return {"gap_id": gap_id, "updated_at": whole.updated_at.isoformat(), **answer}


async def test_an_answer_from_the_dialog_is_recorded_as_the_callers_with_via_elicitation(cards: GapRig) -> None:
    gap_id = cards.question_gap()

    response = await cards.rig.post(ANSWER, await chosen(cards, gap_id, option_rank=1), project_id=ALPHA)

    record = response.json()
    gap = await cards.gaps.get_gap(ALPHA, gap_id)
    assert response.status_code == 200 and record["gap_id"] == gap_id and record["immediate"] is False
    assert record["apply_at"] is None  # staged: nothing applies it until someone presses "Resolve gaps"
    assert gap.status == "answered" and gap.clarification is not None
    assert (gap.clarification.clr_id, gap.clarification.mode) == (record["clr_id"], "option")
    assert gap.clarification.answered_by == cards.rig.alice_id
    answered = [event for event in gap.history.events if event.event == "answered"] if gap.history else []
    assert [event.detail["via"] for event in answered] == ["elicitation"]


async def test_an_own_answer_from_the_dialog_goes_to_the_clarifications_document_as_written(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    text = "Yes, until the parcel leaves the warehouse."

    record = (await cards.rig.post(ANSWER, await chosen(cards, gap_id, own_text=text), project_id=ALPHA)).json()

    gap = await cards.gaps.get_gap(ALPHA, gap_id)
    assert record["placement"] == "document"
    assert gap.clarification is not None and (gap.clarification.mode, gap.clarification.text) == ("own", text)


@pytest.mark.parametrize(
    ("answer", "status", "code"),
    [
        ({"option_rank": 1, "own_text": "x"}, 400, "invalid_arguments"),
        ({}, 400, "invalid_arguments"),
        ({"option_rank": 4}, 400, "answer_invalid"),
    ],
    ids=["both", "neither", "no such option"],
)
async def test_the_answer_route_refuses_anything_but_one_answer(cards: GapRig, answer, status, code) -> None:
    gap_id = cards.question_gap()

    response = await cards.rig.post(ANSWER, await chosen(cards, gap_id, **answer), project_id=ALPHA)

    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert (await cards.gaps.get_gap(ALPHA, gap_id)).status == "open"


async def test_an_own_answer_to_a_contradiction_is_refused_because_it_cannot_amend(cards: GapRig) -> None:
    gap_id = cards.contradiction_gap()

    response = await cards.rig.post(
        ANSWER, await chosen(cards, gap_id, own_text="500 EUR everywhere."), project_id=ALPHA
    )

    error = response.json()["error"]
    assert (response.status_code, error["code"]) == (400, "answer_invalid")
    assert (error["details"]["field"], error["details"]["reason"]) == ("placement", "contradiction_amends")
    assert (await cards.gaps.get_gap(ALPHA, gap_id)).status == "open"


async def test_an_answer_to_a_gap_that_changed_since_the_dialog_is_stale(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    body = {**await chosen(cards, gap_id, option_rank=1), "updated_at": "2020-01-01T00:00:00+00:00"}

    stale = await cards.rig.post(ANSWER, body, project_id=ALPHA)
    unknown = await cards.rig.post(ANSWER, {**body, "gap_id": "GAP::000000000000"}, project_id=ALPHA)
    foreign = await cards.rig.post(ANSWER, body, project_id=FOREIGN)

    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "gap_stale")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "gap_not_found")
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (404, "project_not_found")
    assert (await cards.gaps.get_gap(ALPHA, gap_id)).status == "open"


# --- browse_gaps: the rows of the web Gaps screen, with its filters ----------------------------------------------


async def rows(rig: GapRig, **params: object) -> list[dict]:
    response = await rig.get(INBOX, project_id=ALPHA, limit=100, **params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def test_browse_gaps_lists_the_gaps_people_answer_that_are_not_resolved_as_rows(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    whole = await cards.gaps.get_gap(ALPHA, gap_id)
    inbox = await cards.gaps.list_gaps(ALPHA, route=["ask"], limit=100)

    listed = await rows(cards)

    assert [row["gap_id"] for row in listed] == [gap.gap_id for gap in inbox.gaps] and len(listed) > 2
    row = {row["gap_id"]: row for row in listed}[gap_id]
    assert row == {
        "gap_id": gap_id,
        "title": whole.title,
        "severity": whole.severity,
        "kind": "open_question",
        "status": "open",
        "resolving": False,
        "assignee": None,
    }
    ranks = [("blocker", "major", "minor").index(row["severity"]) for row in listed]
    assert ranks == sorted(ranks)


async def test_browse_gaps_filters_by_status_kind_and_severity(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    resolve(cards, gap_id)
    every = await rows(cards, status=["open", "staged", "resolved"])
    live = [row for row in every if row["status"] != "resolved"]

    default = await rows(cards)
    resolved = await rows(cards, status=["resolved"])
    questions = await rows(cards, kind=["open_question", "assumption"])
    major = await rows(cards, severity=["major"])

    assert gap_id not in {row["gap_id"] for row in default}
    assert [(row["gap_id"], row["status"]) for row in resolved] == [(gap_id, "resolved")]
    assert questions and {row["kind"] for row in questions} <= {"open_question", "assumption"}
    assert [row for row in live if row["kind"] in ("open_question", "assumption")] == questions
    assert major and [row for row in live if row["severity"] == "major"] == major
    assert default == live


async def test_browse_gaps_shows_an_answered_gap_as_staged_until_resolve_gaps(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    await cards.rig.post(ANSWER, await chosen(cards, gap_id, option_rank=1), project_id=ALPHA)

    staged = await rows(cards, status=["staged"])
    still_open = await rows(cards, status=["open"])
    default = await rows(cards)

    assert [(row["gap_id"], row["status"], row["resolving"]) for row in staged] == [(gap_id, "staged", False)]
    assert gap_id not in {row["gap_id"] for row in still_open}
    assert gap_id in {row["gap_id"] for row in default}


async def test_browse_gaps_keeps_my_gaps_the_unassigned_ones_or_a_members_by_name(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    alice = cards.rig.alice_id
    await cards.gaps.assign_gap(ALPHA, gap_id, alice, by=alice)

    mine = await rows(cards, assigned="me")
    by_name = await rows(cards, assignee="  ALICE ")
    unassigned = await rows(cards, assigned="unassigned")
    every = await rows(cards)

    assert [row["gap_id"] for row in mine] == [gap_id]
    assert mine[0]["assignee"] == {"name": "Alice", "you": True, "agent_label": None}
    assert by_name == mine
    assert gap_id not in {row["gap_id"] for row in unassigned} and len(unassigned) == len(every) - 1
    assert all(row["assignee"] is None for row in unassigned)


@pytest.mark.parametrize(
    "params", [{"assignee": "Nobody Here"}, {"assignee": "Alice", "assigned": "me"}], ids=["unknown", "both"]
)
async def test_browse_gaps_refuses_an_unknown_member_and_assigned_with_assignee(cards: GapRig, params: dict) -> None:
    response = await cards.get(INBOX, project_id=ALPHA, **params)

    assert (response.status_code, response.json()["error"]["code"]) == (400, "invalid_arguments")
    assert response.json()["error"]["details"] == {"argument": "assignee"}


async def test_browse_gaps_by_story_lists_the_gaps_about_the_nodes_its_tasks_implement(cards: GapRig) -> None:
    gap_id = cards.question_gap()

    by_story = await rows(cards, story_id=API_STORY)
    about = (await cards.get(GAPS, project_id=ALPHA, story_id=API_STORY, limit=100)).json()["items"]
    unknown = await cards.get(INBOX, project_id=ALPHA, story_id="STORY::NOPE")

    assert gap_id in {row["gap_id"] for row in by_story}
    assert {row["gap_id"] for row in by_story} <= {item["gap_id"] for item in about}
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "story_not_found")


async def test_browse_gaps_pages_and_refuses_a_foreign_project_and_a_bad_cursor(cards: GapRig) -> None:
    every = await rows(cards)
    first = (await cards.get(INBOX, project_id=ALPHA, limit=2)).json()
    rest = (await cards.get(INBOX, project_id=ALPHA, limit=100, cursor=first["next_cursor"])).json()
    foreign = await cards.get(INBOX, project_id=FOREIGN)
    bad = await cards.get(INBOX, project_id=ALPHA, cursor="not-a-cursor")

    assert [row["gap_id"] for row in (*first["items"], *rest["items"])] == [row["gap_id"] for row in every]
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (404, "project_not_found")
    assert (bad.status_code, bad.json()["error"]["code"]) == (400, "invalid_cursor")


async def test_without_a_gap_runner_there_are_no_rows(cards: GapRig) -> None:
    cards.rig.runtime.gaps = None

    response = await cards.get(INBOX, project_id=ALPHA)

    assert response.status_code == 200 and response.json() == {"project_id": ALPHA, "items": [], "next_cursor": None}


# --- open_gap: one gap whole, as its web card shows it -----------------------------------------------------------


async def test_open_gap_shows_an_open_gap_whole_as_its_web_card_does(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    whole = await cards.gaps.get_gap(ALPHA, gap_id)
    ranked = sorted(whole.options, key=lambda option: option.rank)

    detail = (await cards.get(DETAIL, project_id=ALPHA, gap_id=gap_id)).json()

    assert (detail["project_id"], detail["gap_id"], detail["question"], detail["why"]) == (
        ALPHA,
        gap_id,
        QUESTION,
        whole.why,
    )
    assert (detail["status"], detail["answerable"], detail["answer"], detail["assignee"]) == ("open", True, None, None)
    assert [node["spec_id"] for node in detail["about"]] == [node.spec_id for node in whole.about]
    assert [node["spec_id"] for node in detail["impacts"]] == [node.spec_id for node in whole.impacts]
    first = whole.about[0]
    assert detail["about"][0] == {"spec_id": first.spec_id, "type": first.label, "name": first.name}
    assert detail["quotes"] and detail["quotes"][0]["source"].startswith('orders-spec.md, "Place order", line')
    assert [len(option["edits"]) for option in detail["options"]] == [len(option.edits) for option in ranked]
    edits = [edit for option in detail["options"] for edit in option["edits"]]
    assert edits and all(edit["where"].startswith(("the line", "the source", "the clarifications")) for edit in edits)
    assert detail["history"][0]["text"] == "Gap found" and detail["history"][0]["by"] == "SpecFlow"
    assert (detail["reach_stories"], detail["reach_tasks"]) == (list(whole.reach.stories), list(whole.reach.tasks))
    card = detail["card"]
    assert card.startswith(f"**{whole.title}** ({whole.severity} open question, found by analysis, `{gap_id}`)")
    assert "**Status:** open · **Assigned to:** nobody" in card and f"**Question:** {QUESTION}" in card
    assert f"`{whole.about[0].spec_id}`" in card and "- **Other**: your own answer" in card
    assert "  - Changes " in card and "**History:**\n- " in card and "Gap found (SpecFlow)" in card
    assert card.endswith(f"Web card: {WEB}/projects/{ALPHA}")


async def test_open_gap_reads_an_answered_gap_with_its_answer_and_its_history_in_words(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    alice = cards.rig.alice_id
    text = "Yes, until the parcel leaves the warehouse."
    await cards.gaps.assign_gap(ALPHA, gap_id, alice, by=alice)
    await cards.rig.post(ANSWER, await chosen(cards, gap_id, own_text=text), project_id=ALPHA)

    detail = (await cards.get(DETAIL, project_id=ALPHA, gap_id=gap_id)).json()

    assert (detail["status"], detail["resolving"], detail["answerable"]) == ("staged", False, False)
    answer = detail["answer"]
    assert (answer["mode"], answer["answer"], answer["status"]) == ("own", text, "pending")
    assert answer["answered_by"] == "you"
    assert [(item["text"], item["by"]) for item in detail["history"] if item["by"] == "you"] == [
        ("Assigned to you", "you"),
        ("Answered", "you"),
    ]
    card = detail["card"]
    assert "**Status:** staged · **Assigned to:** you" in card
    assert f"**Answer:** {text} (by you, " in card and "; waiting to be applied)" in card
    assert "- **Other**" not in card and "Assigned to you (you)" in card


async def test_open_gap_reads_a_resolved_gap_and_refuses_an_unknown_one(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    resolve(cards, gap_id)

    resolved = await cards.get(DETAIL, project_id=ALPHA, gap_id=gap_id)
    unknown = await cards.get(DETAIL, project_id=ALPHA, gap_id="GAP::000000000000")
    foreign = await cards.get(DETAIL, project_id=FOREIGN, gap_id=gap_id)
    cards.rig.runtime.gaps = None
    unconfigured = await cards.get(DETAIL, project_id=ALPHA, gap_id=gap_id)

    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    assert resolved.json()["answerable"] is False and "- **Other**" not in resolved.json()["card"]
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "gap_not_found")
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (404, "project_not_found")
    assert (unconfigured.status_code, unconfigured.json()["error"]["code"]) == (404, "gap_not_found")
