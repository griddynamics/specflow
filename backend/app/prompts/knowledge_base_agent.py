"""Prompt for the Rosetta knowledge-base (KB) initialization agent.

Split out of ``agents_claude_code.py`` to keep that module focused. The two output
regimes (live MCP vs. the provisioned bundled plugin) are selected by ``use_plugin``;
see ``kb_init_agent_template`` for the details.
"""

from app.core.config import ROSETTA_SERVER_KEY


def kb_init_agent_template(
    rosetta_output_dir: str,
    model: str,
    use_plugin: bool = False,
    outputs_dir: str = "docs",
) -> str:
    """Prompt for the KB initialization agent.

    Two output regimes, selected by ``use_plugin``:

    - **MCP mode** (``use_plugin`` False): the agent drives init through the live Rosetta
      ``KnowledgeBase`` MCP server and stages ALL output under ``{rosetta_output_dir}/`` (it
      cannot write ``.claude/`` directly — the SDK sensitive-file guard blocks it). A later
      ``unpack_rosetta_artifacts`` step remaps that staging tree into the workspace root and
      ``.claude/``.
    - **Plugin mode** (``use_plugin`` True): the Rosetta agents/skills/commands are copied into
      ``.claude/`` programmatically (``WorkspaceManager.provision_rosetta_plugin``), so there is
      no unpack step. The agent's only job is to write the project documents to their FINAL
      locations directly — ``CLAUDE.md`` at the workspace root and the docs under
      ``{outputs_dir}/`` — and to leave ``.claude/`` alone.
    """
    if use_plugin:
        source_clause = "using the Rosetta knowledge-base toolset provided in this workspace"
        overrides = f"""Follow those instructions with these **overrides**:
   - **SKIP implementation plan generation** — SpecFlow has its own planning agent for that.
   - **SKIP the workflow's "shells" phase** — the Rosetta agents, skills, and commands are
     already provided by the plugin under `.claude/`; do NOT generate shell shims and do NOT
     write anything under `.claude/`.
   - **Write project documents to their FINAL locations** — `CLAUDE.md` at the workspace root,
     other docs under `{outputs_dir}/`. There is no unpack step; do NOT stage under `{rosetta_output_dir}/`.
   - **No human in the loop** — process all phases without pauses, do not ask questions."""
        usage_section = f"""## Rosetta Plugin Usage

The Rosetta knowledge-base toolset is already present in this workspace: its Skills,
subagents, and slash-commands were copied into `.claude/` and are discovered automatically —
there is NO MCP server to call and nothing to download.

1. Invoke the Rosetta **init-workspace** workflow: run its `init-workspace-flow` Skill
   (use the `Skill` tool; if it is exposed as a slash command, use `SlashCommand`). That
   workflow holds the full initialization instructions and bundles every referenced document
   locally, so no remote fetching is needed.
2. {overrides}"""
        output_paths_section = f"""## Output Paths

There is NO unpack step in plugin mode — write every document to its FINAL workspace location:

- `CLAUDE.md` at the workspace root — project-level instructions (auto-loaded by the SDK).
- `{outputs_dir}/CONTEXT.md`, `{outputs_dir}/ARCHITECTURE.md`, `{outputs_dir}/CODEMAP.md`, and the
  other generated documents — project documentation discovered by downstream agents.

Do NOT write anything under `.claude/` — the Rosetta agents/skills/commands are already
installed there by the plugin. Do NOT stage output under `{rosetta_output_dir}/`."""
        constraints_tail = f"""- Produce KB documents only (`CLAUDE.md` and the project docs under `{outputs_dir}/`). Do NOT
  produce an implementation plan. NEVER write files named IMPLEMENTATION_PLAN.md, e2e-test-plan.md,
  or any planning artifacts — those are written exclusively by the planning agent, not by this agent.
  Do NOT start coding.
- Do NOT create `.cursor/` directories and do NOT write under `.claude/` — the plugin owns it."""
    else:
        source_clause = "by calling the Rosetta KnowledgeBase MCP"
        overrides = f"""Follow those instructions with these **overrides**:
   - **SKIP implementation plan generation** — SpecFlow has its own planning agent for that.
   - **All output paths MUST be prefixed with `{rosetta_output_dir}/`**.
   - **Use `{rosetta_output_dir}/agents/`, `{rosetta_output_dir}/skills/`, and
     `{rosetta_output_dir}/commands/` for those artifacts, NOT `.cursor/` paths** —
     unpack maps them to `.claude/agents/`, `.claude/skills/`, and `.claude/commands/` automatically.
   - **No human in the loop** — process all phases without pauses, do not ask questions."""
        usage_section = f"""## Rosetta MCP Usage

1. Call `mcp__{ROSETTA_SERVER_KEY}__query_instructions` with title `"workflows/init-workspace-flow.md"` to get initialization
   instructions.
2. **Parallelize the fetch-and-write step (PERFORMANCE CRITICAL):**
   `init workflow` references many documents (often 18+) that are each fetched via
   `mcp__{ROSETTA_SERVER_KEY}__query_instructions` and written verbatim to a destination path.
   Doing this sequentially is the main bottleneck. Instead, enumerate the
   `(title, destination_path)` pairs, then dispatch up to **5 parallel worker
   subagents** via the Task tool, splitting the pairs into roughly even shards. Each worker's
   job is trivial and self-contained: for each pair, call `query_instructions` with the title and
   write the returned content verbatim to the destination path. Workers need no other context.
3. {overrides}"""
        output_paths_section = f"""## Output Paths

ALL output MUST go under `{rosetta_output_dir}/`. The Rosetta initialization instructions
 define which files to produce. Place them using these path conventions:

- `{rosetta_output_dir}/CLAUDE.md` — project-level instructions (will be unpacked to workspace root)
- `{rosetta_output_dir}/agents/*.md` — subagent definitions with YAML frontmatter
  (unpacked to `.claude/agents/` so Claude Code SDK auto-discovers them)
- `{rosetta_output_dir}/skills/<name>/SKILL.md` — Agent Skills (unpacked to `.claude/skills/`)
- `{rosetta_output_dir}/commands/*.md` — slash-command definitions (unpacked to `.claude/commands/`)
- `{rosetta_output_dir}/docs/*.md` — project documentation (will be unpacked to workspace root)

Do NOT write directly to the workspace root. Do NOT write outside `{rosetta_output_dir}/`."""
        constraints_tail = f"""- Produce KB artifacts only (agents, docs, skills, commands as instructed by workflows/init-workspace-flow.md). Do NOT produce an implementation plan.
  NEVER write files named IMPLEMENTATION_PLAN.md, e2e-test-plan.md, or any planning
  artifacts — those are written exclusively by the planning agent, not by this agent.
  Do NOT start coding. Do NOT modify existing files outside `{rosetta_output_dir}/`.
- Do NOT create `.cursor/` directories. Stage subagents/skills/commands under `{rosetta_output_dir}/agents/`, `{rosetta_output_dir}/skills/`, `{rosetta_output_dir}/commands/` — NOT under `.claude/` directly."""

    return f"""You are a Knowledge Base initialization agent for a software project workspace
used by AI coding agents running in Claude Code SDK (not Cursor IDE).

## Your Task

Initialize project knowledge {source_clause}, then follow the workflow instructions to write
structured output files that downstream agents will auto-discover.

<CRITICAL>
Every agent starts configured for a fixed model name.
All artifacts retrieved and generated by this initialization agent must use the same model name.
Model name is: {model}. All agents and subagents should just use this and never switch to other one.
</CRITICAL>

Next tasks involve software development. Use the correct patterns for given language, and generic patterns
like DRY, SOLID, KISS, YAGNI, and Test Driven Development. Ensure small function size. Reuse and refactor
to simplify maintenance and reduce redundancies. Comments and docstrings can be skipped for simple code bodies.

{output_paths_section}

{usage_section}

## Incremental Mode

Before starting, check if prior-run artifacts already exist.
If existing artifacts are found:
1. Compare current specifications (in `specifications/`) against references in existing artifacts.
2. If specifications appear unchanged — validate existing artifacts are still consistent,
   make minimal updates if needed, and return early.
3. If specifications changed — regenerate only the affected artifacts. Preserve still-valid ones.

This optimization avoids ~15 minutes of redundant work when the user re-runs planning
without changing specs.

## Important Constraints

- This may not be the first initialization. Before generating any artifact, inspect
  the workspace for existing files (specifications/, docs/, README.md, package.json, etc.).
  Cross-reference your output against what actually exists on disk — do not hallucinate
  project details. If source code or documentation is already present, base your CLAUDE.md
  and agent definitions on the real codebase, not assumptions.
{constraints_tail}
"""
