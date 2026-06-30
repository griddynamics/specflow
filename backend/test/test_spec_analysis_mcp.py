"""Tests for spec_mcp_servers_and_tools() — Figma-only MCP helper for spec analysis workflows."""

from unittest.mock import Mock


from app.core.config import Settings
from app.core.tool_usage import get_figma_mcp_tools


def _settings(*, mcp_servers_enabled="playwright", figma_token=None, figma_cmd="npx"):
    s = Mock(spec=Settings)
    s.MCP_SERVERS_ENABLED = mcp_servers_enabled
    s.FIGMA_MCP_COMMAND = figma_cmd
    s.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
    s.FIGMA_ACCESS_TOKEN = figma_token
    s.FIGMA_API_KEY = None
    return s


class TestSpecFigmaMcp:
    """spec_mcp_servers_and_tools() only wires Figma — Playwright is not used during spec analysis."""

    def test_figma_in_enabled_mcps_with_token_returns_config(self):
        from app.core.mcp_config import spec_mcp_servers_and_tools as _spec_figma_mcp

        servers, tools = _spec_figma_mcp(_settings(figma_token="tok"), frozenset({"figma"}))
        assert "Figma" in servers
        assert tools == get_figma_mcp_tools()

    def test_figma_not_in_enabled_mcps_returns_empty(self):
        from app.core.mcp_config import spec_mcp_servers_and_tools as _spec_figma_mcp

        servers, tools = _spec_figma_mcp(_settings(figma_token="tok"), frozenset({"playwright"}))
        assert servers == {}
        assert tools == []

    def test_empty_enabled_mcps_returns_empty(self):
        from app.core.mcp_config import spec_mcp_servers_and_tools as _spec_figma_mcp

        servers, tools = _spec_figma_mcp(_settings(), frozenset())
        assert servers == {}
        assert tools == []

    def test_figma_requested_but_token_missing_returns_empty(self):
        from app.core.mcp_config import spec_mcp_servers_and_tools as _spec_figma_mcp

        servers, tools = _spec_figma_mcp(_settings(figma_token=None), frozenset({"figma"}))
        assert servers == {}
        assert tools == []

    def test_playwright_not_wired_even_when_requested(self):
        """Playwright is intentionally excluded from spec analysis agents."""
        from app.core.mcp_config import spec_mcp_servers_and_tools as _spec_figma_mcp

        servers, tools = _spec_figma_mcp(_settings(), frozenset({"playwright"}))
        assert "playwright" not in servers
        assert tools == []

    def test_none_falls_back_to_settings_with_figma(self):
        """None enabled_mcps falls back to settings.MCP_SERVERS_ENABLED."""
        from app.core.mcp_config import spec_mcp_servers_and_tools as _spec_figma_mcp

        servers, tools = _spec_figma_mcp(
            _settings(mcp_servers_enabled="figma", figma_token="tok"), None
        )
        assert "Figma" in servers
        assert tools == get_figma_mcp_tools()

    def test_none_falls_back_to_settings_without_figma(self):
        """None enabled_mcps falls back; default 'playwright' means no Figma."""
        from app.core.mcp_config import spec_mcp_servers_and_tools as _spec_figma_mcp

        servers, tools = _spec_figma_mcp(_settings(mcp_servers_enabled="playwright"), None)
        assert servers == {}
        assert tools == []
