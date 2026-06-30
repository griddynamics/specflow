"""
Unit tests for agents_claude_code module.
"""

import pytest
from unittest.mock import Mock

from app.core.artifact_subdirs import PLANNING_SUBDIR
from app.core.config import (
    MCP_FIGMA,
    MCP_FIGMA_SERVER_KEY,
    MCP_PLAYWRIGHT,
    ROSETTA_SERVER_KEY,
    Settings,
)
from app.core.mcp_config import coding_mcp_servers_and_tools
from app.core.tool_usage import get_figma_mcp_tools, get_playwright_mcp_tools, get_rosetta_kb_tools
from app.prompts.agents_claude_code import is_agent_complete
from app.schemas.agent import AgentResult


class TestIsAgentComplete:
    """Tests for is_agent_complete function."""

    def test_work_complete_in_last_line(self):
        """Test that WORK_COMPLETE in last line returns True."""
        validator_result = AgentResult(
            result="Some summary text\nWORK_COMPLETE",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is True

    def test_work_complete_case_insensitive(self):
        """Test that WORK_COMPLETE is case-insensitive."""
        test_cases = [
            "work_complete",
            "WORK_COMPLETE",
            "Work_Complete",
            "WoRk_CoMpLeTe",
        ]
        for case in test_cases:
            validator_result = AgentResult(
                result=f"Summary\n{case}",
                session_id="test-session"
            )
            assert is_agent_complete(validator_result) is True, f"Failed for case: {case}"

    def test_work_complete_not_in_last_line(self):
        """Test that WORK_COMPLETE not in last line returns False."""
        validator_result = AgentResult(
            result="WORK_COMPLETE\nSome other text",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is False

    def test_work_complete_in_middle_of_last_line(self):
        """Test that WORK_COMPLETE can be part of a larger string in last line."""
        validator_result = AgentResult(
            result="Summary\nThe result is WORK_COMPLETE and done",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is True

    def test_result_is_none(self):
        """Test that None result returns False."""
        validator_result = AgentResult(
            result=None,
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is False

    def test_result_is_empty_string(self):
        """Test that empty string result returns False."""
        validator_result = AgentResult(
            result="",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is False

    def test_result_is_only_newlines(self):
        """Test that result with only newlines returns False."""
        validator_result = AgentResult(
            result="\n\n\n",
            session_id="test-session"
        )
        # splitlines() removes trailing empty lines, so "\n\n\n" becomes []
        assert is_agent_complete(validator_result) is False
    
    def test_empty_last_line_after_non_empty_lines(self):
        """Test that empty last line after non-empty lines returns False."""
        validator_result = AgentResult(
            result="Summary\nSome text\n",
            session_id="test-session"
        )
        # splitlines() removes trailing empty lines, so this becomes ["Summary", "Some text"]
        # Last line is "Some text" which doesn't contain WORK_COMPLETE
        assert is_agent_complete(validator_result) is False

    def test_last_line_is_empty_but_work_complete_exists(self):
        """Test that trailing newline is stripped by splitlines(), so WORK_COMPLETE\n still matches."""
        validator_result = AgentResult(
            result="WORK_COMPLETE\n",
            session_id="test-session"
        )
        # splitlines() removes trailing empty lines, so this becomes ["WORK_COMPLETE"]
        assert is_agent_complete(validator_result) is True

    def test_multiple_lines_with_work_complete_in_last(self):
        """Test multiple lines with WORK_COMPLETE in last line."""
        validator_result = AgentResult(
            result="Line 1\nLine 2\nLine 3\nWORK_COMPLETE",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is True

    def test_work_incomplete_in_last_line(self):
        """Test that WORK_INCOMPLETE in last line returns False."""
        validator_result = AgentResult(
            result="Summary\nWORK_INCOMPLETE",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is False

    def test_no_session_id(self):
        """Test that function works without session_id."""
        validator_result = AgentResult(
            result="Summary\nWORK_COMPLETE",
            session_id=None
        )
        assert is_agent_complete(validator_result) is True

    def test_work_complete_with_whitespace(self):
        """Test that WORK_COMPLETE with surrounding whitespace still matches."""
        validator_result = AgentResult(
            result="Summary\n  WORK_COMPLETE  ",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is True

    def test_single_line_with_work_complete(self):
        """Test single line result with WORK_COMPLETE."""
        validator_result = AgentResult(
            result="WORK_COMPLETE",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is True

    def test_single_line_without_work_complete(self):
        """Test single line result without WORK_COMPLETE."""
        validator_result = AgentResult(
            result="Some summary text",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is False

    def test_work_complete_partial_match(self):
        """Test that partial matches don't trigger false positives."""
        validator_result = AgentResult(
            result="Summary\nWORK_COMPLET",
            session_id="test-session"
        )
        assert is_agent_complete(validator_result) is False

    def test_work_complete_with_newline_at_end(self):
        """Test WORK_COMPLETE with trailing newline."""
        validator_result = AgentResult(
            result="Summary\nWORK_COMPLETE\n",
            session_id="test-session"
        )
        # splitlines() removes trailing empty lines, so this becomes ["Summary", "WORK_COMPLETE"]
        assert is_agent_complete(validator_result) is True


class TestPlanningAgentFallback:
    """Tests for planning_agent_fn fallback to planning_phases.json."""

    VALID_PHASES_JSON = """{
        "phase_count": 2,
        "phases": [
            {"number": 1, "name": "Setup", "description": "Project setup", "estimated_commits": 3},
            {"number": 2, "name": "Backend", "description": "Backend API", "estimated_commits": 5}
        ]
    }"""

    @pytest.mark.asyncio
    async def test_planning_agent_fn_returns_none_for_reparse(
        self, tmp_path
    ):
        """planning_agent_fn always returns None — conversion agent (REPARSE_PLAN) produces JSON."""
        from unittest.mock import AsyncMock, Mock, patch
        from app.services.claude_code import planning_agent_fn
        from app.schemas.agent import AgentResult
        from app.schemas.workspace import WorkspaceSettings

        outputs_dir = "specflow"
        (tmp_path / outputs_dir / PLANNING_SUBDIR).mkdir(parents=True)
        (tmp_path / outputs_dir / PLANNING_SUBDIR / "planning_phases.json").write_text(self.VALID_PHASES_JSON)

        mock_workspace = Mock(spec=WorkspaceSettings)
        mock_workspace.workspace_path = tmp_path
        mock_workspace.model = "test-model"
        mock_workspace.get_isolated_root = Mock(return_value=str(tmp_path))

        mock_request = Mock()
        mock_request.generation_id = "est-1"
        mock_request.outputs_dir = outputs_dir
        mock_request.spec_path = "specs"

        with patch("app.services.claude_code.agent_query", new_callable=AsyncMock) as mock_query, \
             patch("app.services.claude_code.is_skip_mode_enabled", return_value=False), \
             patch("app.services.claude_code.create_agent_logger", return_value=Mock()):
            mock_query.return_value = AgentResult(result="Planning complete. Wrote IMPLEMENTATION_PLAN.md", session_id=None)

            result = await planning_agent_fn(
                primary_workspace=mock_workspace,
                provider_instance=Mock(),
                request=mock_request,
                logger=Mock(),
                model="test-model",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_when_result_and_file_both_missing(self, tmp_path):
        """planning_agent_fn always returns None regardless of agent output content."""
        from unittest.mock import AsyncMock, Mock, patch
        from app.services.claude_code import planning_agent_fn
        from app.schemas.agent import AgentResult
        from app.schemas.workspace import WorkspaceSettings

        mock_workspace = Mock(spec=WorkspaceSettings)
        mock_workspace.workspace_path = tmp_path
        mock_workspace.model = "test-model"
        mock_workspace.get_isolated_root = Mock(return_value=str(tmp_path))

        mock_request = Mock()
        mock_request.generation_id = "est-1"
        mock_request.outputs_dir = "specflow"
        mock_request.spec_path = "specs"

        with patch("app.services.claude_code.agent_query", new_callable=AsyncMock) as mock_query, \
             patch("app.services.claude_code.is_skip_mode_enabled", return_value=False), \
             patch("app.services.claude_code.create_agent_logger", return_value=Mock()):
            mock_query.return_value = AgentResult(result="Now", session_id=None)

            # After PR #176, when both JSON parse attempts fail, planning_agent_fn returns None
            # (REPARSE_PLAN will handle JSON conversion)
            result = await planning_agent_fn(
                primary_workspace=mock_workspace,
                provider_instance=Mock(),
                request=mock_request,
                logger=Mock(),
                model="test-model",
            )
            assert result is None


class TestCodingMcpServersAndTools:
    """Tests for coding_mcp_servers_and_tools() — Playwright + Figma + optional Rosetta."""

    def _settings(self, *, pw_cmd="npx", figma_token="tok"):
        s = Mock(spec=Settings)
        s.PLAYWRIGHT_MCP_COMMAND = pw_cmd
        s.PLAYWRIGHT_MCP_ARGS = "-y @playwright/mcp@latest"
        s.FIGMA_MCP_COMMAND = "npx"
        s.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
        s.FIGMA_ACCESS_TOKEN = figma_token
        s.FIGMA_API_KEY = None
        s.ROSETTA_MCP_ENABLED = False
        return s

    def test_neither_enabled_returns_empty(self):
        servers, tools = coding_mcp_servers_and_tools(self._settings(), frozenset())
        assert servers == {}
        assert tools == []

    def test_playwright_only(self):
        servers, tools = coding_mcp_servers_and_tools(self._settings(), frozenset({MCP_PLAYWRIGHT}))
        assert MCP_PLAYWRIGHT in servers
        assert MCP_FIGMA_SERVER_KEY not in servers
        assert tools == get_playwright_mcp_tools()

    def test_figma_only(self):
        servers, tools = coding_mcp_servers_and_tools(self._settings(), frozenset({MCP_FIGMA}))
        assert MCP_FIGMA_SERVER_KEY in servers
        assert MCP_PLAYWRIGHT not in servers
        assert tools == get_figma_mcp_tools()

    def test_both_enabled_merges_configs_and_tools(self):
        servers, tools = coding_mcp_servers_and_tools(
            self._settings(), frozenset({MCP_PLAYWRIGHT, MCP_FIGMA})
        )
        assert MCP_PLAYWRIGHT in servers
        assert MCP_FIGMA_SERVER_KEY in servers
        assert set(tools) == set(get_playwright_mcp_tools() + get_figma_mcp_tools())

    def test_playwright_command_missing_skips_playwright(self):
        servers, tools = coding_mcp_servers_and_tools(
            self._settings(pw_cmd=""), frozenset({MCP_PLAYWRIGHT})
        )
        assert servers == {}
        assert tools == []

    def test_figma_token_missing_skips_figma(self):
        servers, tools = coding_mcp_servers_and_tools(
            self._settings(figma_token=None), frozenset({MCP_FIGMA})
        )
        assert servers == {}
        assert tools == []

    def test_rosetta_enabled_merges_with_playwright(self):
        s = self._settings()
        s.ROSETTA_MCP_ENABLED = True
        s.ROSETTA_MCP_COMMAND = "uvx"
        s.ROSETTA_MCP_ARGS = "ims-mcp@latest"
        s.ROSETTA_SERVER_URL = None
        s.ROSETTA_USER_EMAIL = None
        s.ROSETTA_IMS_VERSION = ""
        s.ROSETTA_API_KEY = None

        servers, tools = coding_mcp_servers_and_tools(s, frozenset({MCP_PLAYWRIGHT}))
        assert ROSETTA_SERVER_KEY in servers
        assert MCP_PLAYWRIGHT in servers
        assert set(tools) == set(get_rosetta_kb_tools() + get_playwright_mcp_tools())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
