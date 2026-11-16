"""The ``list_gaps`` agent route (``GET /agent/gaps``; docs/mcp-server/tools.md "list_gaps", FR-GWEB-03) and the open
gaps the agent briefs show, over the shop gap corpus after a gap run (``gap_agent_support``)."""

from __future__ import annotations

import pytest

from agent_contracts import AGENT_ROUTES
from gap_acceptance_support import Planted
from gap_agent_support import ALPHA, API_LOGIC, API_STORY, GapRig, serve_gaps
from graph.gap.gaps import gap_uuid
from specflow_api_agent_support import FOREIGN, agent_env

GAPS = AGENT_ROUTES["list_gaps"]


@pytest.fixture
async def gaps(monkeypatch, valid_env):
    async with serve_gaps(monkeypatch, agent_env(valid_env)) as rig:
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
