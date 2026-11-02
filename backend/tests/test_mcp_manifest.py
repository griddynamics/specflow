"""The tool manifest rendered from the registry stays equal to the file the web app's guide reads (FR-MCP-UI-06)."""

from __future__ import annotations

import json

from api.mcp.registry import PROMPTS, TOOLS, render_manifest, render_manifest_json
from mcp_manifest_support import MANIFEST_PATH


def test_the_committed_manifest_matches_the_registry() -> None:
    committed = MANIFEST_PATH.read_bytes()
    assert committed == render_manifest_json().encode("utf-8"), (
        "frontend/lib/guides/mcp-tools.json is stale: run `make mcp-manifest`"
    )


def test_every_declaration_has_a_short_one_line_summary() -> None:
    for declaration in (*TOOLS, *PROMPTS):
        summary = declaration.summary
        assert summary.strip(), declaration.name
        assert len(summary) <= 120, declaration.name
        assert "\n" not in summary and "\r" not in summary, declaration.name


def test_the_manifest_holds_no_description_text() -> None:
    text = render_manifest_json()
    for declaration in (*TOOLS, *PROMPTS):
        assert declaration.description not in text, declaration.name
    assert "description" not in text and "schema" not in text.lower()
    keys = {key for entry in render_manifest()["tools"] for key in entry}
    assert keys == {"name", "summary", "read_only"}


def test_the_manifest_lists_the_declarations_in_order() -> None:
    manifest = render_manifest()
    assert [t["name"] for t in manifest["tools"]] == [t.name for t in TOOLS]
    assert [p["name"] for p in manifest["prompts"]] == [p.name for p in PROMPTS]
    assert all(t["read_only"] is True for t in manifest["tools"])
    assert manifest["prompts"][0]["arguments"] == [{"name": "project", "required": False}]


def test_the_rendering_is_deterministic_and_plain_json() -> None:
    first, second = render_manifest_json(), render_manifest_json()
    assert first == second
    assert first.endswith("}\n") and not first.endswith("\n\n")
    assert json.loads(first) == render_manifest()
    assert '\n  "prompts": [' in first  # two-space indent, sorted keys
