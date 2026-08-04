"""SpecFlow skills bundled for distribution via the MCP server.

Skills are Claude Code slash-command definitions (SKILL.md format).
Served via FastMCP `@mcp.tool()` in server.py as `check_specification_completeness` and
`run_planning` — tools return the SKILL.md template for fully local IDE
execution (bundled skills `specflow-analysis`, `specflow-planning`).
Diagnose and compare-variants remain `@mcp.prompt()` slash commands.

Source of truth: services/skills/{name}/SKILL.md — included as package
data so skills are available after pip install, not just from the repo.

Two consumers, one copy. These files are served as MCP tool responses *and*
symlinked into the `plugins/specflow/` marketplace plugin, so an edit here reaches
both. That is the point of the symlink — keep it.

The `specflow2` plugin is a different product and shares nothing with this
directory: it ships the local refinement loop (`specflow-refine`,
`specflow-resolve`) and writes only under `<outputs_dir>/refine/`. An earlier draft
put those skills in `plugins/specflow/` beside the symlinks, writing this flow's
reserved filenames with content its contract validator rejects; splitting the
plugins is what fixed it. So: no analysis or planning skill in `specflow2`, and
nothing in `specflow2` writes to `analysis/` or `planning/`.

Only user-facing skills are bundled here. Developer skills (specflow-backport,
deploy-requirements) live in .claude/skills/ as repo-local slash commands
for SpecFlow engineers and are not distributed via the MCP server.

See README.md for the user-facing workflow overview.
"""

import importlib.resources

_SKILL_META: dict[str, str] = {
    "specflow-analysis": (
        "Analyze spec completeness locally — gap detection across all architectural dimensions. "
        "No backend required. Repeatable as specs evolve. Produces "
        "docs/analysis/specification_completeness.md."
    ),
    "specflow-planning": (
        "Create a phased implementation plan locally from specs and analysis output. "
        "No backend required. Produces docs/planning/IMPLEMENTATION_PLAN.md (and optionally "
        "e2e-test-plan.md)."
    ),
    "specflow-diagnose": (
        "Diagnose errors and symptoms from a SpecFlow-deployed app. Collects observables "
        "(logs, error messages, infra state), identifies root cause through guided "
        "discovery, and proposes fixes appropriate to each actor — SpecFlow agent re-run, "
        "operator intervention, or spec backport."
    ),
    "specflow-compare-variants": (
        "Compare and assemble code from 1–3 SpecFlow-generated workspace repos. "
        "Produces per-workspace inventory, a side-by-side comparison matrix, interactive "
        "component selection, and a concrete file-level assembly plan for migration to a "
        "production repo."
    ),
}

_SKILL_ORDER = [
    "specflow-analysis",
    "specflow-planning",
    "specflow-diagnose",
    "specflow-compare-variants",
]


def _load_skills() -> list[dict[str, str]]:
    skills_pkg = importlib.resources.files("services.skills")
    skills = []
    for name in _SKILL_ORDER:
        try:
            content = (skills_pkg / name / "SKILL.md").read_text(encoding="utf-8")
        except (FileNotFoundError, TypeError):
            continue
        skills.append({
            "name": name,
            "description": _SKILL_META.get(name, ""),
            "content": content,
        })
    return skills


SKILLS: list[dict[str, str]] = _load_skills()
