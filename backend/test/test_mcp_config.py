"""Unit tests for mcp_config module."""

import pytest
from unittest.mock import Mock

from app.core.config import ROSETTA_SERVER_KEY, Settings
from app.core.mcp_config import (
    build_figma_mcp_config,
    build_playwright_mcp_config,
    build_rosetta_mcp_config,
    enabled_mcps_to_parameter_string,
    merge_mcp_server_dicts,
    mcp_resolution_from_workspace_sync_parameters,
    parse_mcp_servers_enabled_string,
    resolve_enabled_mcps,
    resolve_enabled_mcps_detailed,
)


class TestBuildRosettaMcpConfig:
    """Tests for build_rosetta_mcp_config() — parity with `uvx ims-mcp@latest` + ROSETTA_* / VERSION env."""

    @pytest.fixture
    def mock_settings(self) -> Mock:
        settings = Mock(spec=Settings)
        settings.ROSETTA_MCP_ENABLED = True
        settings.ROSETTA_MCP_COMMAND = "uvx"
        settings.ROSETTA_MCP_ARGS = "ims-mcp@latest"
        settings.ROSETTA_SERVER_URL = "https://ims.example.com/"
        settings.ROSETTA_API_KEY = "key"
        settings.ROSETTA_USER_EMAIL = "user@example.com"
        settings.ROSETTA_IMS_VERSION = "r2"
        return settings

    def test_returns_empty_when_disabled(self, mock_settings: Mock) -> None:
        """Scenario: feature flag off -> returns empty dict, KB init is skipped."""
        mock_settings.ROSETTA_MCP_ENABLED = False
        result = build_rosetta_mcp_config(mock_settings)
        assert result == {}

    def test_returns_full_config_when_enabled(self, mock_settings: Mock) -> None:
        """Scenario: ims-mcp env surface set -> MCP stdio config returned."""
        result = build_rosetta_mcp_config(mock_settings)

        assert ROSETTA_SERVER_KEY in result
        kb = result[ROSETTA_SERVER_KEY]
        assert kb["command"] == "uvx"
        assert kb["args"] == ["ims-mcp@latest"]
        assert kb["env"]["ROSETTA_SERVER_URL"] == "https://ims.example.com/"
        assert kb["env"]["ROSETTA_API_KEY"] == "key"
        assert kb["env"]["ROSETTA_USER_EMAIL"] == "user@example.com"
        assert kb["env"]["VERSION"] == "r2"

    def test_strips_secrets(self, mock_settings: Mock) -> None:
        """Scenario: ROSETTA_API_KEY has surrounding whitespace -> stripped in env."""
        mock_settings.ROSETTA_API_KEY = "  rk-secret  "
        result = build_rosetta_mcp_config(mock_settings)
        assert result[ROSETTA_SERVER_KEY]["env"]["ROSETTA_API_KEY"] == "rk-secret"

    def test_handles_partial_env_vars(self, mock_settings: Mock) -> None:
        """Scenario: only server URL set -> env has only that key (+ VERSION if set)."""
        mock_settings.ROSETTA_SERVER_URL = "https://only-url/"
        mock_settings.ROSETTA_API_KEY = None
        mock_settings.ROSETTA_USER_EMAIL = None
        mock_settings.ROSETTA_IMS_VERSION = ""

        result = build_rosetta_mcp_config(mock_settings)

        env = result[ROSETTA_SERVER_KEY]["env"]
        assert env == {"ROSETTA_SERVER_URL": "https://only-url/"}

    def test_all_optional_cleared(self, mock_settings: Mock) -> None:
        """Scenario: all optional strings empty -> env dict is empty; command still ims-mcp@latest."""
        mock_settings.ROSETTA_SERVER_URL = None
        mock_settings.ROSETTA_API_KEY = None
        mock_settings.ROSETTA_USER_EMAIL = None
        mock_settings.ROSETTA_IMS_VERSION = ""

        result = build_rosetta_mcp_config(mock_settings)

        assert result[ROSETTA_SERVER_KEY]["env"] == {}
        assert result[ROSETTA_SERVER_KEY]["command"] == "uvx"
        assert result[ROSETTA_SERVER_KEY]["args"] == ["ims-mcp@latest"]


class TestParseMcpServersEnabled:
    def test_filters_unknown_names(self) -> None:
        assert parse_mcp_servers_enabled_string("playwright,figma,unknown") == frozenset(
            {"playwright", "figma"}
        )

    def test_empty_string(self) -> None:
        assert parse_mcp_servers_enabled_string("") == frozenset()
        assert parse_mcp_servers_enabled_string("  ,  ") == frozenset()


class TestResolveEnabledMcps:
    def test_form_wins_over_stored_and_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        got = resolve_enabled_mcps(
            form_value="figma",
            generation_session_parameters={"mcp_servers_enabled": "playwright"},
            settings=s,
        )
        assert got == frozenset({"figma"})

    def test_stored_used_when_form_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        got = resolve_enabled_mcps(
            form_value=None,
            generation_session_parameters={"mcp_servers_enabled": "figma,playwright"},
            settings=s,
        )
        assert got == frozenset({"figma", "playwright"})

    def test_default_when_no_form_or_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "figma")
        s = Settings()
        got = resolve_enabled_mcps(
            form_value=None,
            generation_session_parameters={},
            settings=s,
        )
        assert got == frozenset({"figma"})


class TestResolveEnabledMcpsDetailed:
    def test_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        r1 = resolve_enabled_mcps_detailed(
            form_value="figma",
            generation_session_parameters={"mcp_servers_enabled": "playwright"},
            settings=s,
        )
        assert r1.source == "form"
        assert r1.raw_request_string == "figma"
        r2 = resolve_enabled_mcps_detailed(
            form_value=None,
            generation_session_parameters={"mcp_servers_enabled": "playwright"},
            settings=s,
        )
        assert r2.source == "generation_session_parameters"
        assert r2.raw_request_string == "playwright"
        r3 = resolve_enabled_mcps_detailed(form_value=None, generation_session_parameters={}, settings=s)
        assert r3.source == "backend_settings"
        assert r3.raw_request_string == "playwright"


class TestResolveEnabledMcpsPreferStored:
    """Planning/generation: Firestore mcp_servers_enabled wins over repeated MCP env form."""

    def test_stored_wins_over_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        r = resolve_enabled_mcps_detailed(
            form_value="figma,playwright",
            generation_session_parameters={"mcp_servers_enabled": "figma"},
            settings=s,
            prefer_stored=True,
        )
        assert r.enabled == frozenset({"figma"})
        assert r.source == "generation_session_parameters"
        assert r.raw_request_string == "figma"

    def test_form_when_stored_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        r = resolve_enabled_mcps_detailed(
            form_value="figma",
            generation_session_parameters={},
            settings=s,
            prefer_stored=True,
        )
        assert r.enabled == frozenset({"figma"})
        assert r.source == "form"

    def test_form_when_stored_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        r = resolve_enabled_mcps_detailed(
            form_value="figma",
            generation_session_parameters={"mcp_servers_enabled": "  "},
            settings=s,
            prefer_stored=True,
        )
        assert r.enabled == frozenset({"figma"})
        assert r.source == "form"

    def test_settings_when_no_form_and_no_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        r = resolve_enabled_mcps_detailed(
            form_value=None,
            generation_session_parameters={},
            settings=s,
            prefer_stored=True,
        )
        assert r.enabled == frozenset({"playwright"})
        assert r.source == "backend_settings"


class TestMcpResolutionFromWorkspaceSync:
    def test_uses_param_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "playwright")
        s = Settings()
        r = mcp_resolution_from_workspace_sync_parameters(
            {"mcp_servers_enabled": "figma,playwright"}, s
        )
        assert r.source == "workspace_sync_params"
        assert r.raw_request_string == "figma,playwright"
        assert r.enabled == frozenset({"figma", "playwright"})

    def test_falls_back_to_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVERS_ENABLED", "figma")
        s = Settings()
        r = mcp_resolution_from_workspace_sync_parameters({}, s)
        assert r.source == "backend_settings"
        assert r.raw_request_string == "figma"


class TestMergeMcpServerDicts:
    def test_merges_and_later_wins(self) -> None:
        a = {"x": {"command": "a"}}
        b = {"y": {"command": "b"}, "x": {"command": "c"}}
        assert merge_mcp_server_dicts(a, b) == {
            "x": {"command": "c"},
            "y": {"command": "b"},
        }


class TestBuildPlaywrightMcpConfig:
    def test_builds_stdio_config(self) -> None:
        m = Mock(spec=Settings)
        m.PLAYWRIGHT_MCP_COMMAND = "npx"
        m.PLAYWRIGHT_MCP_ARGS = "-y @playwright/mcp@latest"
        cfg = build_playwright_mcp_config(m)
        assert "playwright" in cfg
        assert cfg["playwright"]["command"] == "npx"
        assert "-y" in cfg["playwright"]["args"]

    def test_empty_when_no_command(self) -> None:
        m = Mock(spec=Settings)
        m.PLAYWRIGHT_MCP_COMMAND = ""
        m.PLAYWRIGHT_MCP_ARGS = "-y x"
        assert build_playwright_mcp_config(m) == {}


class TestBuildFigmaMcpConfig:
    def test_empty_without_token(self) -> None:
        m = Mock(spec=Settings)
        m.FIGMA_ACCESS_TOKEN = None
        m.FIGMA_API_KEY = None
        m.FIGMA_MCP_COMMAND = "npx"
        m.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
        assert build_figma_mcp_config(m) == {}

    def test_builds_with_access_token(self) -> None:
        m = Mock(spec=Settings)
        m.FIGMA_ACCESS_TOKEN = "tok"
        m.FIGMA_API_KEY = None
        m.FIGMA_MCP_COMMAND = "npx"
        m.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
        cfg = build_figma_mcp_config(m)
        assert "Figma" in cfg
        assert cfg["Figma"]["env"]["FIGMA_API_KEY"] == "tok"


def test_enabled_mcps_to_parameter_string() -> None:
    assert enabled_mcps_to_parameter_string(frozenset({"figma", "playwright"})) == "figma,playwright"


# ---------------------------------------------------------------------------
# Blank-config no-op regression tests (FR-10 / S4.1 insurance)
# ---------------------------------------------------------------------------

class TestRosettaBlankConfigNoOp:
    """ROSETTA_MCP_ENABLED=False with blank credentials → empty dict, no error."""

    def test_disabled_flag_returns_empty_no_error(self) -> None:
        """Scenario: ROSETTA_MCP_ENABLED=False → build_rosetta_mcp_config returns {} without raising."""
        m = Mock(spec=Settings)
        m.ROSETTA_MCP_ENABLED = False
        m.ROSETTA_MCP_COMMAND = "uvx"
        m.ROSETTA_MCP_ARGS = "ims-mcp@latest"
        m.ROSETTA_SERVER_URL = None
        m.ROSETTA_API_KEY = None
        m.ROSETTA_USER_EMAIL = None
        m.ROSETTA_IMS_VERSION = ""
        result = build_rosetta_mcp_config(m)
        assert result == {}

    def test_disabled_flag_with_blank_creds_returns_empty(self) -> None:
        """Scenario: flag off AND all creds blank → still returns {}, no KeyError or AttributeError."""
        m = Mock(spec=Settings)
        m.ROSETTA_MCP_ENABLED = False
        m.ROSETTA_MCP_COMMAND = ""
        m.ROSETTA_MCP_ARGS = ""
        m.ROSETTA_SERVER_URL = ""
        m.ROSETTA_API_KEY = ""
        m.ROSETTA_USER_EMAIL = ""
        m.ROSETTA_IMS_VERSION = ""
        result = build_rosetta_mcp_config(m)
        assert result == {}


class TestFigmaBlankConfigNoOp:
    """Blank FIGMA_ACCESS_TOKEN and FIGMA_API_KEY → empty dict, no error (FR-10)."""

    def test_both_tokens_none_returns_empty(self) -> None:
        """Scenario: FIGMA_ACCESS_TOKEN=None, FIGMA_API_KEY=None → {} without raising."""
        m = Mock(spec=Settings)
        m.FIGMA_ACCESS_TOKEN = None
        m.FIGMA_API_KEY = None
        m.FIGMA_MCP_COMMAND = "npx"
        m.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
        assert build_figma_mcp_config(m) == {}

    def test_both_tokens_empty_string_returns_empty(self) -> None:
        """Scenario: FIGMA_ACCESS_TOKEN='', FIGMA_API_KEY='' → {} (blank-safe)."""
        m = Mock(spec=Settings)
        m.FIGMA_ACCESS_TOKEN = ""
        m.FIGMA_API_KEY = ""
        m.FIGMA_MCP_COMMAND = "npx"
        m.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
        assert build_figma_mcp_config(m) == {}


