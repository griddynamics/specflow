"""The contract gate of the MCP tooling tools (MCP-TOOLING D29): ``list_areas``, ``list_stories``, ``preview_story``,
``claim_story``, ``release_story``, ``get_story_brief``, ``trace``, ``get_node``, ``search_spec``, ``similar``,
``get_evidence``, ``path``, ``expand_brief``, ``add_finding``, ``report_task`` and ``complete_story``, frozen as
golden files NEXT TO the split's (whose files stay as the split froze them, ``tools/list`` aside).

Read the way a client reads them, over JSON-RPC and HTTP against the in-process MCP app, whose API is the real app over
the shop graph (``mcp_golden_tooling_seam``): one success result per tool and shape (structured content and text
block), one error result per ``ErrorCode`` these tools return. The goldens hold when the data behind the tools moves.

Regenerate on purpose, after a deliberate contract change, and review the diff before commit::

    cd backend && MCP_GOLDEN_UPDATE=1 uv run pytest tests/test_mcp_golden_tooling.py -q

Later sessions of the tooling add their tools' results and error cases to these files, never to the split's.
"""

from __future__ import annotations

import json
from typing import Any

from mcp_golden_tooling_seam import ALPHA, CHECKOUT, UNKNOWN_PROJECT
from test_mcp_golden import check_golden, tool

# The ErrorCodes these tools return (tools.md), kept literal so nothing here imports the product; a code a new tool
# returns must be added here with its case and golden.
ERROR_CODES = (
    "project_not_found",
    "no_plan",
    "story_not_found",
    "node_not_found",
    "invalid_arguments",
    "unknown_relation",
    "graph_unavailable",
    "database_unavailable",
    "internal_error",
    "invalid_cursor",
    "area_not_found",
    "story_claimed",
    "not_available",
    "partly_claimed",
    "plan_applying",
    "claim_mismatch",
    "task_not_found",
    "invalid_commit",
    "finding_limit",
)
STRIPE = "STORY::STRIPE::CORE"
TASK = "TASK::WEB_SHOP::LOGIC_CHECKOUT"
COMMIT = "e44f0c1a7b9d3c5e8f2a4b6d8e0f1a2b3c4d5e6f"
PR = "https://git.example.test/shop/pull/7"
FIRST_CLAIM = "00000000000000000000000000000001"  # the first claim token of a scenario
NOBODYS_CLAIM = "00000000000000000000000000000099"  # a claim token nobody holds
PAYMENTS = "STORY::PAYMENTS_SERVICE::CARD_PAYMENT"

SUCCESSES: dict[str, tuple[str, dict[str, Any]]] = {
    "get_story_brief": ("get_story_brief", {"project_id": ALPHA, "story_id": CHECKOUT}),
    "get_story_brief_no_neighbours": (
        "get_story_brief",
        {"project_id": ALPHA, "story_id": CHECKOUT, "neighbours": 0},
    ),
    "trace_story_based_on": ("trace", {"project_id": ALPHA, "story_id": CHECKOUT, "direction": "based_on"}),
    "trace_spec_ids_affects_with_holders": (
        "trace",
        {"project_id": ALPHA, "spec_ids": ["IFC::STRIPE_API", "NOPE::X"], "direction": "affects", "depth": 1},
    ),
    "get_node": ("get_node", {"project_id": ALPHA, "spec_id": "MOD::STRIPE"}),
    "get_node_hub_summarised": ("get_node", {"project_id": ALPHA, "spec_id": "MOD::ORDERS_SERVICE", "edge_limit": 3}),
    "get_node_provenance_only": (
        "get_node",
        {"project_id": ALPHA, "spec_id": "REQ::LOGIC_EXPORT", "include": ["provenance"]},
    ),
    "search_spec": ("search_spec", {"project_id": ALPHA, "query": "checkout"}),
    "search_spec_near_story": ("search_spec", {"project_id": ALPHA, "query": "checkout", "near": CHECKOUT, "limit": 3}),
    "search_spec_id_hit": ("search_spec", {"project_id": ALPHA, "query": "what is FEAT::CARD_PAYMENT", "limit": 3}),
    "similar_story": ("similar", {"project_id": ALPHA, "story_id": CHECKOUT}),
    "similar_spec_ids_linked_kept": (
        "similar",
        {
            "project_id": ALPHA,
            "spec_ids": ["FEAT::CHECKOUT", "FEAT::NOPE"],
            "exclude_linked": False,
            "min_score": 0.85,
        },
    ),
    "get_evidence": ("get_evidence", {"project_id": ALPHA, "spec_id": "CON::EU_REGION"}),
    "get_evidence_without_context": (
        "get_evidence",
        {"project_id": ALPHA, "spec_id": "REQ::LOGIC_EXPORT", "context_lines": 0},
    ),
    "path_based_on": ("path", {"project_id": ALPHA, "from": "MOD::PAYMENTS_SERVICE", "to": "MOD::STRIPE"}),
    "path_affects": ("path", {"project_id": ALPHA, "from": "FEAT::CARD_PAYMENT", "to": "FEAT::CHECKOUT"}),
    # a module inside a chain is passed over a call between modules (QA F16): this pair now has a based_on chain
    "path_through_a_module_call": ("path", {"project_id": ALPHA, "from": "MOD::PAYMENTS_SERVICE", "to": "CON::STRIPE"}),
    "path_same_node_reason": ("path", {"project_id": ALPHA, "from": "FEAT::CHECKOUT", "to": "FEAT::CHECKOUT"}),
    "expand_brief": ("expand_brief", {"project_id": ALPHA, "story_id": CHECKOUT}),
    "expand_brief_cut_at_the_budget": (
        "expand_brief",
        {"project_id": ALPHA, "story_id": CHECKOUT, "budget_tokens": 500},
    ),
    "list_areas": ("list_areas", {"project_id": ALPHA}),
    "list_stories_open": ("list_stories", {"project_id": ALPHA}),
    "list_stories_open_first_page": ("list_stories", {"project_id": ALPHA, "limit": 1}),
    "list_stories_one_area": ("list_stories", {"project_id": ALPHA, "area_id": "AREA::STRIPE"}),
    "list_stories_mine_none": ("list_stories", {"project_id": ALPHA, "view": "mine"}),
    "list_stories_done_none": ("list_stories", {"project_id": ALPHA, "view": "done"}),
    "preview_story_ready": ("preview_story", {"project_id": ALPHA, "story_id": CHECKOUT}),
    "preview_story_in_progress": ("preview_story", {"project_id": ALPHA, "story_id": STRIPE}),
    "preview_story_blocked": ("preview_story", {"project_id": ALPHA, "story_id": PAYMENTS}),
}
# Against a plan the story calls can write (``free``, ``claimed``): the tools in the order they are called.
CLAIMED_SUCCESSES: dict[str, tuple[str, dict[str, Any]]] = {
    "list_areas_with_a_holder": ("list_areas", {"project_id": ALPHA}),
    "list_stories_open_with_a_holder": ("list_stories", {"project_id": ALPHA}),
    "preview_story_held": ("preview_story", {"project_id": ALPHA, "story_id": CHECKOUT}),
}
NO_PLAN_SUCCESSES: dict[str, tuple[str, dict[str, Any]]] = {
    "get_node_without_a_plan": ("get_node", {"project_id": ALPHA, "spec_id": "MOD::STRIPE"}),
}


async def agent_writes(client: Any) -> dict[str, Any]:
    """Alice claims Checkout, records a finding (and again), reports its task, completes the story (and again)."""

    results: dict[str, Any] = {}
    claimed = await tool(client, "claim_story", project_id=ALPHA, story_id=CHECKOUT)
    token = claimed["structuredContent"]["claim_token"]
    finding = {
        "project_id": ALPHA,
        "kind": "gap",
        "text": "The spec does not say how long a cart is kept.",
        "about": ["FEAT::CHECKOUT"],
        "refers_to": [
            {"type": "pr", "value": "https://git.example.test/shop/pull/7"},
            {"type": "commit", "value": "e44f0c1"},
        ],
        "story_id": CHECKOUT,
    }
    results["add_finding"] = await tool(client, "add_finding", **finding)
    results["add_finding_repeat"] = await tool(client, "add_finding", **finding)
    results["add_finding_blocking_repeat_without_a_claim"] = await tool(
        client, "add_finding", **{**finding, "blocking": True}
    )  # the repeat is answered before the blocking rules
    report = {"project_id": ALPHA, "task_id": TASK, "claim_token": token}
    results["report_task"] = await tool(client, "report_task", **report, progress="done", note="Built and tested")
    results["report_task_repeat"] = await tool(
        client, "report_task", **report, progress="done", note="Built and tested"
    )
    results["report_task_skipped"] = await tool(client, "report_task", **report, progress="skipped", note="Not needed")
    completion = {"project_id": ALPHA, "story_id": CHECKOUT, "claim_token": token, "merge_commit": COMMIT}
    results["complete_story"] = await tool(client, "complete_story", **completion, pr_url=PR)
    results["complete_story_repeat"] = await tool(client, "complete_story", **completion, pr_url=PR)
    results["list_stories_done_completed"] = await tool(client, "list_stories", project_id=ALPHA, view="done")
    return results


async def blocking_finding(client: Any) -> dict[str, Any]:
    """A blocking question: the claim ends, the task is blocked and the story lists as blocked with its question."""

    claimed = await tool(client, "claim_story", project_id=ALPHA, story_id=CHECKOUT)
    blocking = {
        "project_id": ALPHA,
        "kind": "question",
        "text": "Is a cart kept for one day or for a week?",
        "about": ["FEAT::CHECKOUT"],
        "story_id": CHECKOUT,
        "claim_token": claimed["structuredContent"]["claim_token"],
        "blocking": True,
    }
    return {
        "add_finding_blocking": await tool(client, "add_finding", **blocking),
        "list_stories_mine_blocked": await tool(client, "list_stories", project_id=ALPHA, view="mine"),
    }


async def test_every_tool_answers_the_golden_success_results(tooling_double):
    """One success per tool and shape: structured content equal to the one JSON text block."""

    results: dict[str, Any] = {}
    names: dict[str, str] = {}
    for scenario, successes in (("ok", SUCCESSES), ("no_plan", NO_PLAN_SUCCESSES), ("claimed", CLAIMED_SUCCESSES)):
        async with tooling_double.serve(scenario) as client:
            for key, (name, arguments) in successes.items():
                results[key], names[key] = await tool(client, name, **arguments), name
    async with tooling_double.serve("free") as client:  # the writes: a claim, its repeat, a release, its repeat
        claim = {
            "project_id": ALPHA,
            "story_id": CHECKOUT,
            "agent_label": "Claude Code · laptop",
            "branch": "feat/checkout",
        }
        results["claim_story"] = await tool(client, "claim_story", **claim)
        token = results["claim_story"]["structuredContent"]["claim_token"]
        results["claim_story_repeat"] = await tool(client, "claim_story", project_id=ALPHA, story_id=CHECKOUT)
        results["list_stories_mine_held"] = await tool(client, "list_stories", project_id=ALPHA, view="mine")
        release = {
            "project_id": ALPHA,
            "story_id": CHECKOUT,
            "claim_token": token,
            "note": "Stopped for the day",
            "branch": "feat/checkout",
        }
        results["release_story"] = await tool(client, "release_story", **release)
        results["release_story_repeat"] = await tool(client, "release_story", **release)
        results["preview_story_released"] = await tool(client, "preview_story", project_id=ALPHA, story_id=CHECKOUT)
    async with tooling_double.serve("free") as client:  # the agent writes: a story from claim to completion
        results |= await agent_writes(client)
    async with tooling_double.serve("free") as client:  # a blocking finding ends the claim and blocks the story
        results |= await blocking_finding(client)
    names |= {
        "add_finding": "add_finding",
        "add_finding_repeat": "add_finding",
        "add_finding_blocking_repeat_without_a_claim": "add_finding",
        "report_task": "report_task",
        "report_task_skipped": "report_task",
        "report_task_repeat": "report_task",
        "complete_story": "complete_story",
        "complete_story_repeat": "complete_story",
        "list_stories_done_completed": "list_stories",
        "add_finding_blocking": "add_finding",
        "list_stories_mine_blocked": "list_stories",
        "claim_story": "claim_story",
        "claim_story_repeat": "claim_story",
        "list_stories_mine_held": "list_stories",
        "release_story": "release_story",
        "release_story_repeat": "release_story",
        "preview_story_released": "preview_story",
    }
    assert (
        results["claim_story"]["structuredContent"]["claim_token"]
        == results["claim_story_repeat"]["structuredContent"]["claim_token"]
    )
    assert results["release_story_repeat"]["structuredContent"]["released"] == []
    for key, result in results.items():
        assert result.get("isError", False) is False, key
        assert len(result["content"]) == 1, key
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"], key
    check_golden("tool_results_tooling.json", results)


def limit_finding(text: str) -> dict[str, Any]:
    return {"project_id": ALPHA, "kind": "question", "text": text, "about": ["FEAT::CHECKOUT"], "story_id": CHECKOUT}


async def test_every_error_code_the_tools_return_has_a_golden_error_result(tooling_double):
    """One error result per ``ErrorCode``, each reached the way an agent reaches it."""

    cases: dict[str, tuple[str, str, dict[str, Any]]] = {
        "project_not_found": ("ok", "get_story_brief", {"project_id": UNKNOWN_PROJECT, "story_id": CHECKOUT}),
        "no_plan": ("no_plan", "get_story_brief", {"project_id": ALPHA, "story_id": CHECKOUT}),
        "story_not_found": ("ok", "get_story_brief", {"project_id": ALPHA, "story_id": "STORY::NOPE"}),
        "node_not_found": ("ok", "get_node", {"project_id": ALPHA, "spec_id": "FEAT::NOPE"}),
        "invalid_arguments": ("ok", "trace", {"project_id": ALPHA, "direction": "affects"}),
        "unknown_relation": (
            "ok",
            "trace",
            {"project_id": ALPHA, "story_id": CHECKOUT, "direction": "affects", "relations": ["NOPE"]},
        ),
        "graph_unavailable": ("graph_down", "get_node", {"project_id": ALPHA, "spec_id": "MOD::STRIPE"}),
        "database_unavailable": ("database_down", "get_node", {"project_id": ALPHA, "spec_id": "MOD::STRIPE"}),
        "internal_error": ("bug", "get_node", {"project_id": ALPHA, "spec_id": "MOD::STRIPE"}),
        "invalid_cursor": ("ok", "list_stories", {"project_id": ALPHA, "cursor": "not-a-cursor"}),
        "area_not_found": ("ok", "list_stories", {"project_id": ALPHA, "area_id": "AREA::NOPE"}),
        "story_claimed": ("claimed", "claim_story", {"project_id": ALPHA, "story_id": CHECKOUT}),
        "not_available": ("free", "claim_story", {"project_id": ALPHA, "story_id": PAYMENTS}),
        "partly_claimed": ("free", "claim_story", {"project_id": ALPHA, "story_id": STRIPE}),
        "plan_applying": ("applying", "claim_story", {"project_id": ALPHA, "story_id": CHECKOUT}),
        "claim_mismatch": (
            "free",
            "release_story",
            {"project_id": ALPHA, "story_id": CHECKOUT, "claim_token": "not-the-token"},
        ),
        "task_not_found": (
            "free",
            "report_task",
            {"project_id": ALPHA, "task_id": "TASK::NOPE", "claim_token": NOBODYS_CLAIM, "progress": "done"},
        ),
        "invalid_commit": (
            "free",
            "complete_story",
            {"project_id": ALPHA, "story_id": CHECKOUT, "claim_token": NOBODYS_CLAIM, "merge_commit": "abc"},
        ),
    }
    # More than one way to an error code: the case is named ``<code>:<what>``, its golden next to the plain one.
    extra: dict[str, tuple[str, str, dict[str, Any]]] = {
        "invalid_arguments:agent_label": (
            "free",
            "claim_story",
            {"project_id": ALPHA, "story_id": CHECKOUT, "agent_label": "two\nlines"},
        ),
        "invalid_arguments:branch": (
            "free",
            "release_story",
            {"project_id": ALPHA, "story_id": CHECKOUT, "claim_token": "t", "branch": ""},
        ),
        "plan_applying:release_story": (
            "applying",
            "release_story",
            {"project_id": ALPHA, "story_id": CHECKOUT, "claim_token": "t"},
        ),
    }
    held = {"project_id": ALPHA, "story_id": CHECKOUT, "claim_token": FIRST_CLAIM}
    extra |= {
        "node_not_found:add_finding": (
            "free",
            "add_finding",
            {"project_id": ALPHA, "kind": "note", "text": "x", "about": ["FEAT::CHECKOUT", "REQ::NOPE"]},
        ),
        "invalid_arguments:add_finding_refers_to": (
            "free",
            "add_finding",
            {
                "project_id": ALPHA,
                "kind": "note",
                "text": "x",
                "about": ["FEAT::CHECKOUT"],
                "refers_to": [{"type": "url", "value": "ftp://example.com/x"}],
            },
        ),
        "invalid_arguments:add_finding_blocking_note": (
            "free",
            "add_finding",
            {
                "project_id": ALPHA,
                "kind": "note",
                "text": "x",
                "about": ["FEAT::CHECKOUT"],
                "story_id": CHECKOUT,
                "claim_token": NOBODYS_CLAIM,
                "blocking": True,
            },
        ),
        "invalid_arguments:report_task_skipped_without_a_note": (
            "free",
            "report_task",
            {"project_id": ALPHA, "task_id": TASK, "claim_token": NOBODYS_CLAIM, "progress": "skipped"},
        ),
        "invalid_arguments:complete_story_pr_url": (
            "free",
            "complete_story",
            {**held, "merge_commit": COMMIT, "pr_url": "https://user:pw@example.com/p/1"},
        ),
        "story_not_found:add_finding": (
            "free",
            "add_finding",
            {"project_id": ALPHA, "kind": "note", "text": "x", "about": ["FEAT::CHECKOUT"], "story_id": "STORY::NOPE"},
        ),
        "claim_mismatch:add_finding": (
            "free",
            "add_finding",
            {
                "project_id": ALPHA,
                "kind": "question",
                "text": "x",
                "about": ["FEAT::CHECKOUT"],
                "story_id": CHECKOUT,
                "claim_token": NOBODYS_CLAIM,
                "blocking": True,
            },
        ),
        "claim_mismatch:report_task": (
            "free",
            "report_task",
            {"project_id": ALPHA, "task_id": TASK, "claim_token": NOBODYS_CLAIM, "progress": "done"},
        ),
        "claim_mismatch:complete_story": (
            "free",
            "complete_story",
            {**held, "claim_token": NOBODYS_CLAIM, "merge_commit": COMMIT},
        ),
        "plan_applying:add_finding": (
            "held_applying",
            "add_finding",
            {
                "project_id": ALPHA,
                "kind": "question",
                "text": "x",
                "about": ["FEAT::CHECKOUT"],
                "story_id": CHECKOUT,
                "claim_token": FIRST_CLAIM,
                "blocking": True,
            },
        ),
        "plan_applying:report_task": (
            "held_applying",
            "report_task",
            {"project_id": ALPHA, "task_id": TASK, "claim_token": FIRST_CLAIM, "progress": "done"},
        ),
        "plan_applying:complete_story": (
            "held_applying",
            "complete_story",
            {**held, "merge_commit": COMMIT},
        ),
    }
    assert set(cases) == set(ERROR_CODES) - {"finding_limit"}, "the cases must be the ErrorCodes these tools return"
    errors: dict[str, Any] = {}
    every = {**cases, **extra}
    for scenario in dict.fromkeys(scenario for scenario, _, _ in every.values()):  # one server per scenario
        async with tooling_double.serve(scenario) as client:
            for code, (_, name, arguments) in ((c, case) for c, case in every.items() if case[0] == scenario):
                errors[code] = await tool(client, name, **arguments)
    async with tooling_double.serve("free") as client:  # the 51st open finding of the caller on the story
        for number in range(50):
            added = await tool(client, "add_finding", **limit_finding(f"Question {number}"))
            assert added.get("isError", False) is False, number
        errors["finding_limit"] = await tool(client, "add_finding", **limit_finding("One too many"))
    for code, result in errors.items():
        assert result["isError"] is True and result["structuredContent"]["error"]["code"] == code.split(":")[0]
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    for code in (
        "story_claimed",
        "not_available",
        "partly_claimed",
        "invalid_arguments:agent_label",
        "node_not_found:add_finding",
        "invalid_commit",
    ):
        assert "details" in errors[code]["structuredContent"]["error"], code  # the typed details of tools.md
    order = [*ERROR_CODES, *extra]
    check_golden("tool_errors_tooling.json", dict(sorted(errors.items(), key=lambda item: order.index(item[0]))))
