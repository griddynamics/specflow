"""The contract gate of the MCP tooling tools (MCP-TOOLING D29): ``get_story_brief``, ``trace``, ``get_node``,
``search_spec``, ``similar``, ``get_evidence``, ``path`` and ``expand_brief``, frozen as golden files NEXT TO the
split's (whose files stay as the split froze them, ``tools/list`` aside).

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
)

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
    "path_none_through_a_module": ("path", {"project_id": ALPHA, "from": "MOD::PAYMENTS_SERVICE", "to": "CON::STRIPE"}),
    "expand_brief": ("expand_brief", {"project_id": ALPHA, "story_id": CHECKOUT}),
    "expand_brief_cut_at_the_budget": (
        "expand_brief",
        {"project_id": ALPHA, "story_id": CHECKOUT, "budget_tokens": 500},
    ),
}
NO_PLAN_SUCCESSES: dict[str, tuple[str, dict[str, Any]]] = {
    "get_node_without_a_plan": ("get_node", {"project_id": ALPHA, "spec_id": "MOD::STRIPE"}),
}


async def test_every_tool_answers_the_golden_success_results(tooling_double):
    """One success per tool and shape: structured content equal to the one JSON text block."""

    results: dict[str, Any] = {}
    for scenario, successes in (("ok", SUCCESSES), ("no_plan", NO_PLAN_SUCCESSES)):
        async with tooling_double.serve(scenario) as client:
            for key, (name, arguments) in successes.items():
                results[key] = await tool(client, name, **arguments)
    for key, result in results.items():
        assert result.get("isError", False) is False, key
        assert (
            len(result["content"]) == 1 and json.loads(result["content"][0]["text"]) == result["structuredContent"]
        ), key
    check_golden("tool_results_tooling.json", results)


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
    }
    assert set(cases) == set(ERROR_CODES), "the cases must be exactly the ErrorCodes these tools return"
    errors: dict[str, Any] = {}
    for scenario in dict.fromkeys(scenario for scenario, _, _ in cases.values()):  # one server per scenario
        async with tooling_double.serve(scenario) as client:
            for code, (_, name, arguments) in ((c, case) for c, case in cases.items() if case[0] == scenario):
                errors[code] = await tool(client, name, **arguments)
    for code, result in errors.items():
        assert result["isError"] is True and result["structuredContent"]["error"]["code"] == code
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    check_golden("tool_errors_tooling.json", dict(sorted(errors.items(), key=lambda item: ERROR_CODES.index(item[0]))))
