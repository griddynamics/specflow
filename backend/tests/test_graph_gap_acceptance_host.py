"""Acceptance tests of graph.gap: Host, routes, MCP and user interface (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

import asyncio

from gap_acceptance_support import pending
from gap_host_support import StubGapRun, drained, make_gap_runner, store_build
from specflow_api_support import bearer


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


@pending("12", "read routes: 05; answer routes: 08")
def test_fr_gweb_02_web_routes_serve_members_only() -> None:
    """Route tests: each route answers for a member and refuses a non-member."""


@pending("07")
def test_fr_gweb_03_mcp_tool_list_gaps_list_open_gaps() -> None:
    """Given an open gap on a story, then `list_gaps` returns it and the story brief lists it."""


@pending("12", "end-to-end; read part: 07")
def test_fr_gweb_04_gaps_screen_show_inbox_filters_question_card() -> None:
    """End-to-end test on the built frontend with the shop corpus fixture."""


@pending("12", "end-to-end")
def test_fr_gweb_05_document_view_counts_answers_and_offers_download() -> None:
    """End-to-end test: an amended document shows the count and the download."""
