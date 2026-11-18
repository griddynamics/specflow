"""Acceptance tests of graph.gap: Host, routes, MCP and user interface (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Final

from agent_contracts import AGENT_ROUTES
from api.routers.gaps import router as gaps_router
from gap_acceptance_support import Planted
from gap_agent_support import ALPHA, API_STORY, QUESTION, serve_gaps
from gap_host_support import StubGapRun, drained, make_gap_runner, store_build
from specflow_api_agent_support import agent_env
from specflow_api_support import bearer
from test_api_gap_routes import READ_ROUTES, WRITE_ROUTES, gap_project, url

FRONTEND: Final = Path(__file__).resolve().parents[2] / "frontend"


async def test_fr_gweb_01_host_run_gap_runs_process_at_time(harness, tmp_path) -> None:
    """Given two builds in quick succession, then gap runs do not overlap for the project and the status route
    reports each.

    The host's gap runner (stub ``run_gap``) on the API runtime: the second build returns while the first build's gap
    run still runs; the status route shows the first running with the second waiting, then the second completed.
    """

    created = await harness.client.post("/api/v1/projects", json={"name": "Shop"}, headers=bearer())
    project_id = created.json()["id"]
    await store_build(tmp_path, "build-1", minutes=0, project_id=project_id)
    await store_build(tmp_path, "build-2", minutes=5, project_id=project_id)
    run = StubGapRun(block=True)
    runner = harness.runtime.gaps = make_gap_runner(tmp_path, run)
    status_url = f"/api/v1/projects/{project_id}/gap/runs/latest"

    await runner.after_build(project_id, "build-1")
    await asyncio.wait_for(run.started.wait(), 5)
    await runner.after_build(project_id, "build-2")
    first = (await harness.client.get(status_url, headers=bearer())).json()
    run.release.set()
    await drained(runner, project_id)
    second = (await harness.client.get(status_url, headers=bearer())).json()
    await runner.aclose()

    assert run.max_active == 1
    assert [context.build.build_id for context in run.contexts] == ["build-1", "build-2"]
    assert (first["status"], first["build_id"], first["queued"]) == ("running", "build-1", 1)
    assert (second["status"], second["build_id"], second["queued"]) == ("completed", "build-2", 0)
    assert second["result"]["build_id"] == "build-2"


async def test_fr_gweb_02_web_routes_serve_members_only(harness, monkeypatch) -> None:
    """Route tests: each route answers for a member and refuses a non-member.

    Every route of ``api.routers.gaps`` is one of the route tables of ``test_api_gap_routes``; a member's call
    reaches the gap service (any answer but ``project_not_found``), a non-member's answers ``404 project_not_found``
    and calls nothing.
    """

    async def no_document(*_: object) -> None:
        return None

    # the document filter reads the corpus rows from the database (an empty corpus here)
    monkeypatch.setattr("api.routers.gaps.sources_repo.graph_corpus_document", no_document)

    templates = {(method, route.path) for route in gaps_router.routes for method in getattr(route, "methods", ())}
    covered = {(method, _template(tail)) for method, tail in READ_ROUTES} | {
        (method, _template(tail)) for method, tail, *_ in WRITE_ROUTES
    }
    assert {(method, path.removeprefix("/projects/{pid}")) for method, path in templates} == covered
    calls = [(method, tail, None) for method, tail in READ_ROUTES]
    calls += [(method, tail, body) for method, tail, body, *_ in WRITE_ROUTES]
    for method, tail, body in calls:
        runner, pid = await gap_project(harness)
        stranger = await harness.client.request(method, url(pid, tail), json=body, headers=bearer("bob@example.com"))
        assert (stranger.status_code, stranger.json()["error"]["code"]) == (404, "project_not_found"), tail
        assert runner.gap_service.calls == [] and runner.starts == []
        member = await harness.client.request(method, url(pid, tail), json=body, headers=bearer())
        assert member.status_code < 500, (tail, member.text)
        if member.status_code >= 400:
            assert member.json()["error"]["code"] != "project_not_found", tail


def _template(tail: str) -> str:
    """A route table's path as the router's template: ids become their parameters."""

    tail = tail.split("?", 1)[0]
    tail = re.sub(r"/gaps/GAP::[^/]+", "/gaps/{gap_id}", tail)
    tail = re.sub(r"/clarifications/[^/]+/", "/clarifications/{clr_id}/", tail)
    tail = re.sub(r"/noise_rules/[^/]+$", "/noise_rules/{rule_id}", tail)
    return re.sub(r"/gap/runs/(?!latest)[^/]+/", "/gap/runs/{run_id}/", tail)


async def test_fr_gweb_03_mcp_tool_list_gaps(monkeypatch, valid_env) -> None:
    """Given an open gap on a story, then `list_gaps` returns it and the story brief lists it.

    The open gap about ``REQ::FR::PLACE_ORDER`` (the corpus's stated question) and the story ``STORY::A::API`` whose
    task implements it: the ``list_gaps`` agent route returns it for the story and for the spec id, with severity and
    question; the story brief route lists it with its severity; ``get_node`` lists it on the node.
    """

    async with serve_gaps(monkeypatch, agent_env(valid_env)) as gaps:
        gap_id = gaps.question_gap()
        by_story = (await gaps.get(AGENT_ROUTES["list_gaps"], project_id=ALPHA, story_id=API_STORY)).json()
        by_node = (await gaps.get(AGENT_ROUTES["list_gaps"], project_id=ALPHA, spec_ids=[Planted.PLACE_ORDER])).json()
        brief = (await gaps.get(AGENT_ROUTES["get_story_brief"], project_id=ALPHA, story_id=API_STORY)).json()
        node = (
            await gaps.get(
                AGENT_ROUTES["get_node"], project_id=ALPHA, spec_id=Planted.PLACE_ORDER, include=["findings"]
            )
        ).json()

    for page in (by_story, by_node):
        item = next(item for item in page["items"] if item["gap_id"] == gap_id)
        assert (item["kind"], item["severity"], item["status"], item["question"]) == (
            "open_question",
            "major",
            "open",
            QUESTION,
        )
        assert Planted.PLACE_ORDER in item["about"] and item["answer"] is None
    severities = {item["finding_id"]: item["severity"] for item in brief["brief"]["findings"]}
    assert severities[gap_id] == "major"
    assert {item["finding_id"]: item["severity"] for item in node["findings"]}[gap_id] == "major"


def test_fr_gweb_04_gaps_screen_show_inbox_filters_question_card() -> None:
    """End-to-end test on the built frontend with the shop corpus fixture.

    The browser tests run in ``make e2e-production`` (Playwright on the built frontend, stub API with the shop-corpus
    gaps of ``e2e-production/gaps-fixtures.ts``); here: they exist under the production config and cover the inbox,
    the filters and the question card.
    """

    titles = _e2e_titles("FR-GWEB-04")
    assert any("inbox" in title for title in titles)
    assert any("filters" in title for title in titles)
    assert any("question card" in title for title in titles)
    assert any("Submit" in title for title in titles)
    assert any("'Answered by AI' tab" in title and "Revert" in title for title in titles)
    assert any("noise report" in title for title in titles)


def test_fr_gweb_06_the_gaps_screen_assigns_and_filters_by_assignee() -> None:
    """End-to-end test: a member assigns a gap to a member from the menu in its row without opening the card, and the
    Assigned filter and column show it.

    The browser test of ``make e2e-production`` (``gaps-write-production.spec.ts``); here: it exists under the
    production config.
    """

    titles = _e2e_titles("FR-GWEB-06")
    assert any("assigns" in title and "Assigned" in title for title in titles)


def _e2e_titles(unit_id: str) -> list[str]:
    """The titles of the production browser tests that name ``unit_id`` (``playwright.production.config.ts``)."""

    config = (FRONTEND / "playwright.production.config.ts").read_text(encoding="utf-8")
    assert "e2e-production" in config
    return [
        match.group(1)
        for spec in sorted((FRONTEND / "e2e-production").glob("gaps*-production.spec.ts"))
        for match in re.finditer(r"test\(\s*[\"`]([^\"`]+)[\"`]", spec.read_text(encoding="utf-8"))
        if unit_id in match.group(1)
    ]
