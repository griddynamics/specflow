"""The ``list_gaps`` agent route (``GET /agent/gaps``; docs/mcp-server/tools.md "list_gaps", FR-GWEB-03), the
``list_open_gaps`` route (``GET /agent/open-gaps``: the gap cards, docs/mcp-server/apps.md "Terminal dialogs"), the two
routes of ``answer_gap`` (``GET /agent/gap-card``, ``POST /agent/gap-answer``) and the open gaps the agent briefs show,
over the shop gap corpus after a gap run (``gap_agent_support``)."""

from __future__ import annotations

import pytest

from agent_contracts import AGENT_ROUTES, GAP_CARD_ROUTE
from gap_acceptance_support import Planted
from gap_agent_support import ALPHA, API_LOGIC, API_STORY, QUESTION, GapRig, serve_gaps
from graph.gap.gaps import gap_uuid
from specflow_api_agent_support import FOREIGN, agent_env

GAPS = AGENT_ROUTES["list_gaps"]
OPEN = AGENT_ROUTES["list_open_gaps"]
ANSWER = AGENT_ROUTES["answer_gap"]
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


# --- list_open_gaps: the cards of the gaps that wait for a person -------------------------------------------------


async def test_list_open_gaps_shows_each_open_gap_as_a_card_with_its_web_link(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    whole = await cards.gaps.get_gap(ALPHA, gap_id)

    body = (await cards.get(OPEN, project_id=ALPHA, limit=25)).json()
    item = {item["gap_id"]: item for item in body["items"]}[gap_id]

    assert body["project_id"] == ALPHA and body["next_cursor"] is None
    assert (item["severity"], item["kind"], item["title"]) == (whole.severity, whole.kind, whole.title)
    assert item["web_url"] == f"{WEB}/projects/{ALPHA}/gaps?gap={gap_id}"
    card = item["card"]
    assert card.startswith(f"**{whole.title}** ({whole.severity} open question, found by analysis, `{gap_id}`)")
    assert f"**Question:** {QUESTION}" in card
    labels = [option.label for option in sorted(whole.options, key=lambda option: option.rank)]
    assert labels and all(f"- **{chr(65 + index)}) {label}" in card for index, label in enumerate(labels))
    assert card.count("(recommended)") == 1 and "- **Other**: your own answer" in card
    assert '> orders-spec.md, "Place order", line' in card and f': "{QUESTION}"' in card
    assert card.endswith(f"Web card: {item['web_url']}")


async def test_list_open_gaps_lists_blockers_first_and_pages_with_its_cursor(cards: GapRig) -> None:
    every = (await cards.get(OPEN, project_id=ALPHA, limit=25)).json()["items"]
    first = (await cards.get(OPEN, project_id=ALPHA, limit=2)).json()
    rest = (await cards.get(OPEN, project_id=ALPHA, limit=25, cursor=first["next_cursor"])).json()

    ranks = [("blocker", "major", "minor").index(item["severity"]) for item in every]
    assert len(every) > 2 and ranks == sorted(ranks)
    assert [item["gap_id"] for item in (*first["items"], *rest["items"])] == [item["gap_id"] for item in every]


async def test_list_open_gaps_leaves_out_gaps_no_longer_open(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    resolve(cards, gap_id)

    listed = (await cards.get(OPEN, project_id=ALPHA, limit=25)).json()["items"]

    assert gap_id not in {item["gap_id"] for item in listed}


async def test_without_the_web_app_address_the_cards_have_no_link(gaps: GapRig) -> None:
    listed = (await gaps.get(OPEN, project_id=ALPHA)).json()["items"]

    assert listed and all(item["web_url"] is None and "Web card:" not in item["card"] for item in listed)


async def test_list_open_gaps_refuses_a_foreign_project_and_a_bad_cursor(cards: GapRig) -> None:
    foreign = await cards.get(OPEN, project_id=FOREIGN)
    bad = await cards.get(OPEN, project_id=ALPHA, cursor="not-a-cursor")
    long = await cards.get(OPEN, project_id=ALPHA, cursor="x" * 513)

    assert (foreign.status_code, foreign.json()["error"]["code"]) == (404, "project_not_found")
    assert (bad.status_code, bad.json()["error"]["code"]) == (400, "invalid_cursor")
    assert (long.status_code, long.json()["error"]["code"]) == (400, "invalid_cursor")


async def test_without_a_gap_runner_there_are_no_open_gaps(cards: GapRig) -> None:
    cards.rig.runtime.gaps = None

    response = await cards.get(OPEN, project_id=ALPHA)

    assert response.status_code == 200 and response.json() == {"project_id": ALPHA, "items": [], "next_cursor": None}


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
    assert card["web_url"] == f"{WEB}/projects/{ALPHA}/gaps?gap={gap_id}" and card["card"].startswith("**")


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
    assert gap.status == "answered" and gap.clarification is not None
    assert (gap.clarification.clr_id, gap.clarification.mode) == (record["clr_id"], "option")
    assert gap.clarification.answered_by == cards.rig.alice_id
    answered = [event for event in gap.history.events if event.event == "answered"] if gap.history else []
    assert [event.detail["via"] for event in answered] == ["elicitation"]
    assert [project for project, _ in cards.runner.applies] == [ALPHA]  # the apply waits for the quiet period


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


async def test_an_answer_to_a_gap_that_changed_since_the_dialog_is_stale(cards: GapRig) -> None:
    gap_id = cards.question_gap()
    body = {**await chosen(cards, gap_id, option_rank=1), "updated_at": "2020-01-01T00:00:00+00:00"}

    stale = await cards.rig.post(ANSWER, body, project_id=ALPHA)
    unknown = await cards.rig.post(ANSWER, {**body, "gap_id": "GAP::000000000000"}, project_id=ALPHA)
    foreign = await cards.rig.post(ANSWER, body, project_id=FOREIGN)

    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "gap_stale")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "gap_not_found")
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (404, "project_not_found")
    assert cards.runner.applies == []
