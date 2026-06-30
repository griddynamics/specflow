"""Unit tests for kb_init_agent_template prompt."""


from app.core.config import LLM_MEDIUM_DEFAULT_FIRST_MODEL, ROSETTA_SERVER_KEY
from app.prompts.knowledge_base_agent import kb_init_agent_template


class TestKbInitAgentTemplate:
    """Tests for kb_init_agent_template()."""

    def test_contains_rosetta_output_path(self) -> None:
        prompt = kb_init_agent_template(rosetta_output_dir="rosetta", model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "rosetta/" in prompt
        assert "rosetta/CLAUDE.md" in prompt

    def test_custom_output_dir(self) -> None:
        prompt = kb_init_agent_template(rosetta_output_dir="my_kb", model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "my_kb/" in prompt
        assert "my_kb/CLAUDE.md" in prompt
        assert "my_kb/agents/" in prompt

    def test_skip_implementation_plan_instruction(self) -> None:
        prompt = kb_init_agent_template(rosetta_output_dir="rosetta", model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "SKIP implementation plan" in prompt

    def test_mcp_tool_references(self) -> None:
        prompt = kb_init_agent_template(rosetta_output_dir="rosetta", model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert f"mcp__{ROSETTA_SERVER_KEY}__query_instructions" in prompt
        assert "init-workspace-flow.md" in prompt

    def test_uses_claude_paths_not_cursor(self) -> None:
        prompt = kb_init_agent_template(rosetta_output_dir="rosetta", model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "rosetta/agents/" in prompt
        assert "rosetta/skills/" in prompt
        assert "rosetta/commands/" in prompt
        assert ".claude/agents/" in prompt  # mentioned as the final destination after sync
        assert ".claude/skills/" in prompt
        assert ".claude/commands/" in prompt
        assert "Do NOT create `.cursor/` directories" in prompt

    def test_no_human_in_the_loop(self) -> None:
        prompt = kb_init_agent_template(rosetta_output_dir="rosetta", model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "No human in the loop" in prompt

    def test_scope_constraints(self) -> None:
        prompt = kb_init_agent_template(rosetta_output_dir="rosetta", model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "Do NOT start coding" in prompt
        assert "Do NOT modify existing files" in prompt

    # --- Plugin mode: docs go to FINAL locations, no rosetta/ staging, no .claude/ writes ---

    def test_plugin_mode_writes_to_final_locations_not_rosetta(self) -> None:
        prompt = kb_init_agent_template(
            rosetta_output_dir="rosetta",
            model=LLM_MEDIUM_DEFAULT_FIRST_MODEL,
            use_plugin=True,
            outputs_dir="docs",
        )
        # Final locations, not rosetta/ staging.
        assert "CLAUDE.md` at the workspace root" in prompt
        assert "docs/CONTEXT.md" in prompt
        assert "do not stage under `rosetta/`" in prompt.lower()
        # No MCP, and the agent must not touch .claude/ (the plugin owns it).
        assert "mcp__" not in prompt
        assert "write under `.claude/`" in prompt.lower()

    def test_plugin_mode_honors_custom_outputs_dir(self) -> None:
        prompt = kb_init_agent_template(
            rosetta_output_dir="rosetta",
            model=LLM_MEDIUM_DEFAULT_FIRST_MODEL,
            use_plugin=True,
            outputs_dir="specflow",
        )
        assert "specflow/CONTEXT.md" in prompt

    def test_plugin_mode_skips_shells_phase(self) -> None:
        prompt = kb_init_agent_template(
            rosetta_output_dir="rosetta",
            model=LLM_MEDIUM_DEFAULT_FIRST_MODEL,
            use_plugin=True,
        )
        assert "shells" in prompt.lower()
        assert "## Rosetta Plugin Usage" in prompt
