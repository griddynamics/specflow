"""Acceptance tests of graph.gap: Host, routes, MCP and user interface (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("05")
def test_fr_gweb_01_host_run_gap_runs_process_at_time() -> None:
    """Given two builds in quick succession, then gap runs do not overlap for the project and the status route
    reports each.
    """


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
