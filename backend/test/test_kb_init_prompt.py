"""Unit tests for kb_init_agent_template prompt (provisioned Rosetta plugin)."""


from app.core.config import LLM_MEDIUM_DEFAULT_FIRST_MODEL
from app.prompts.knowledge_base_agent import kb_init_agent_template


class TestKbInitAgentTemplate:
    """Tests for the direct plugin workflow."""

    def test_writes_to_final_locations(self) -> None:
        prompt = kb_init_agent_template(model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "CLAUDE.md` at the workspace root" in prompt
        assert "docs/CONTEXT.md" in prompt

    def test_honors_custom_outputs_dir(self) -> None:
        prompt = kb_init_agent_template(
            model=LLM_MEDIUM_DEFAULT_FIRST_MODEL,
            outputs_dir="specflow",
        )
        assert "specflow/CONTEXT.md" in prompt

    def test_skip_implementation_plan_instruction(self) -> None:
        prompt = kb_init_agent_template(model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "SKIP implementation plan" in prompt

    def test_plugin_entry_points_are_explicit(self) -> None:
        prompt = kb_init_agent_template(model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "## Rosetta Plugin Usage" in prompt
        assert "init-workspace-flow" in prompt
        assert "`Skill` tool" in prompt
        assert "`SlashCommand`" in prompt

    def test_leaves_dot_claude_to_the_plugin(self) -> None:
        prompt = kb_init_agent_template(model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "Leave the provisioned `.claude/` content unchanged." in prompt
        assert "Do NOT create `.cursor/` directories" in prompt

    def test_skips_shells_phase(self) -> None:
        prompt = kb_init_agent_template(model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "shells" in prompt.lower()

    def test_no_human_in_the_loop(self) -> None:
        prompt = kb_init_agent_template(model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "No human in the loop" in prompt

    def test_scope_constraints(self) -> None:
        prompt = kb_init_agent_template(model=LLM_MEDIUM_DEFAULT_FIRST_MODEL)
        assert "Do NOT start coding" in prompt
