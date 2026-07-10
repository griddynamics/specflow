"""Unit tests for the pure MCP client registry (tui/mcp_clients.py).

Everything here runs without a terminal or any client CLI installed — the
correctness-critical logic (JSON forms, arg translation, deeplink encoding,
config merge, detection, the connected marker, the CLI hint) is all pure.
"""

import base64
import json
import urllib.parse

import pytest

from tui import mcp_clients as mc

# A representative live block, matching .specflow-local/mcp-config.json.
BLOCK = mc.ServerBlock(
    command="uvx",
    args=("--refresh", "--no-cache", "--from", "/abs/path/mcp_server", "specflow-mcp"),
    env={"USER_EMAIL": "user@x.com", "WORKSPACE_COUNT": "3"},
)

RAW_CONFIG = {
    "mcpServers": {
        "specflow": {
            "command": "uvx",
            "args": ["--refresh", "--no-cache", "--from", "/abs/path/mcp_server", "specflow-mcp"],
            "env": {"USER_EMAIL": "user@x.com", "WORKSPACE_COUNT": "3"},
        }
    }
}


class TestServerBlock:
    def test_reads_live_block(self):
        block = mc.server_block(RAW_CONFIG)
        assert block.command == "uvx"
        assert block.args[0] == "--refresh"
        assert block.env["USER_EMAIL"] == "user@x.com"

    def test_missing_block_raises_actionable(self):
        with pytest.raises(KeyError, match="run setup first"):
            mc.server_block({"mcpServers": {}})


class TestRenderJson:
    def test_inner_has_no_name_or_type(self):
        obj = json.loads(mc.render_json(BLOCK, mc.JsonForm.INNER))
        assert set(obj) == {"command", "args", "env"}

    def test_with_type_stdio_for_claude(self):
        obj = json.loads(mc.render_json(BLOCK, mc.JsonForm.WITH_TYPE_STDIO))
        assert obj["type"] == "stdio"
        assert obj["command"] == "uvx"

    def test_flat_with_name_for_vscode(self):
        obj = json.loads(mc.render_json(BLOCK, mc.JsonForm.FLAT_WITH_NAME, name="specflow"))
        assert obj["name"] == "specflow"
        assert "type" not in obj

    def test_env_omitted_when_empty(self):
        obj = json.loads(mc.render_json(mc.ServerBlock("uvx", (), {}), mc.JsonForm.INNER))
        assert "env" not in obj


class TestDeeplink:
    def test_round_trips_to_inner_block(self):
        url = mc.build_deeplink(BLOCK, mc.CURSOR)
        assert url.startswith("cursor://anysphere.cursor-deeplink/mcp/install?")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        decoded = json.loads(base64.b64decode(query["config"][0]).decode())
        assert decoded == {
            "command": "uvx",
            "args": list(BLOCK.args),
            "env": dict(BLOCK.env),
        }
        assert "name" not in decoded  # name is a query param, not in the config

    def test_no_raw_plus_or_space_in_url(self):
        # A long, plus-prone payload forces base64 to contain '+'; it must be
        # percent-encoded, never left raw (which Cursor decodes to a space).
        block = mc.ServerBlock("uvx", tuple(f"--flag{i}=ÿÿ" for i in range(8)), {"K": "v" * 40})
        url = mc.build_deeplink(block, mc.CURSOR)
        config = url.split("config=", 1)[1]
        assert "+" not in config
        assert " " not in config

    def test_too_long_detected(self):
        assert mc.deeplink_too_long("x" * (mc.MAX_DEEPLINK_URL_LENGTH + 1)) is True
        assert mc.deeplink_too_long("cursor://short") is False


class TestBuildAddArgv:
    def test_claude_uses_add_json_with_type_and_user_scope(self):
        argv = mc.build_add_argv(mc.CLAUDE_CODE, BLOCK)
        assert argv[:4] == ["claude", "mcp", "add-json", "specflow"]
        assert json.loads(argv[4])["type"] == "stdio"
        assert argv[-2:] == ["-s", "user"]

    def test_gemini_translates_args_and_env_to_flags(self):
        argv = mc.build_add_argv(mc.GEMINI_CLI, BLOCK)
        assert argv[:5] == ["gemini", "mcp", "add", "specflow", "uvx"]
        assert "--refresh" in argv
        # env becomes repeated -e KEY=VALUE pairs
        assert "-e" in argv and "USER_EMAIL=user@x.com" in argv
        assert "WORKSPACE_COUNT=3" in argv
        assert argv[-2:] == ["-s", "user"]

    def test_deeplink_client_has_no_cli_add(self):
        with pytest.raises(ValueError):
            mc.build_add_argv(mc.CURSOR, BLOCK)


class TestRemoveAndVerify:
    def test_claude_removes_first(self):
        assert mc.build_remove_argv(mc.CLAUDE_CODE) == [
            "claude", "mcp", "remove", "specflow", "-s", "user"
        ]

    def test_gemini_no_remove(self):
        assert mc.build_remove_argv(mc.GEMINI_CLI) is None

    def test_claude_verify_argv_substitutes_name(self):
        assert mc.build_verify_argv(mc.CLAUDE_CODE) == ["claude", "mcp", "get", "specflow"]

    def test_unverifiable_clients_return_none(self):
        assert mc.build_verify_argv(mc.CURSOR) is None
        assert mc.build_verify_argv(mc.GEMINI_CLI) is None

    def test_verify_passed_checks_name_in_output(self):
        assert mc.verify_passed("specflow: uvx - ✔ Connected") is True
        assert mc.verify_passed("no such server") is False


class TestMergeBlock:
    def test_preserves_sibling_servers_and_keys(self):
        existing = {
            "mcpServers": {"other": {"command": "x"}},
            "someOtherTopLevel": 1,
        }
        merged = mc.merge_block(existing, BLOCK, mc.ConfigShape.MCP_SERVERS)
        assert merged["mcpServers"]["other"] == {"command": "x"}  # untouched
        assert merged["someOtherTopLevel"] == 1
        assert merged["mcpServers"]["specflow"]["command"] == "uvx"
        # original not mutated
        assert "specflow" not in existing["mcpServers"]

    def test_creates_key_when_absent(self):
        merged = mc.merge_block({}, BLOCK, mc.ConfigShape.MCP_SERVERS)
        assert merged["mcpServers"]["specflow"]["command"] == "uvx"

    def test_servers_shape_adds_type_stdio(self):
        merged = mc.merge_block({}, BLOCK, mc.ConfigShape.SERVERS)
        assert merged["servers"]["specflow"]["type"] == "stdio"

    def test_rejects_non_object_key(self):
        with pytest.raises(ValueError):
            mc.merge_block({"mcpServers": "oops"}, BLOCK, mc.ConfigShape.MCP_SERVERS)


class TestIsInstalled:
    def test_manual_always_available(self):
        assert mc.is_installed(mc.MANUAL, which=lambda _b: None) is True

    def test_cli_detected_via_which(self):
        assert mc.is_installed(mc.CLAUDE_CODE, which=lambda b: "/bin/" + b) is True
        assert mc.is_installed(mc.CLAUDE_CODE, which=lambda _b: None) is False

    def test_cursor_detected_via_config_dir(self, tmp_path):
        (tmp_path / ".cursor").mkdir()
        assert mc.is_installed(mc.CURSOR, which=lambda _b: None, home=tmp_path) is True

    def test_cursor_absent_when_no_binary_or_dir(self, tmp_path):
        assert mc.is_installed(mc.CURSOR, which=lambda _b: None, home=tmp_path) is False


class TestGlobalConfig:
    def test_path_is_under_home_specflow(self, tmp_path):
        assert mc.config_path(home=tmp_path) == tmp_path / ".specflow" / "config.json"

    def test_empty_when_absent(self, tmp_path):
        assert mc.saved_statuses(home=tmp_path) == {}
        assert mc.is_any_client_connected(home=tmp_path) is False

    def test_save_and_read_back_actual_status(self, tmp_path):
        mc.save_status("claude_code", mc.ClientStatus.VERIFIED, home=tmp_path)
        mc.save_status("cursor", mc.ClientStatus.ADDED_UNVERIFIED, home=tmp_path)
        assert mc.saved_statuses(home=tmp_path) == {
            "claude_code": mc.ClientStatus.VERIFIED,
            "cursor": mc.ClientStatus.ADDED_UNVERIFIED,
        }

    def test_save_overwrites_prior_status(self, tmp_path):
        mc.save_status("cursor", mc.ClientStatus.ADDED_UNVERIFIED, home=tmp_path)
        mc.save_status("cursor", mc.ClientStatus.FAILED, home=tmp_path)
        assert mc.saved_statuses(home=tmp_path)["cursor"] is mc.ClientStatus.FAILED

    def test_transient_statuses_not_persisted(self, tmp_path):
        mc.save_status("cursor", mc.ClientStatus.CONNECTING, home=tmp_path)
        mc.save_status("cursor", mc.ClientStatus.NOT_CONFIGURED, home=tmp_path)
        assert mc.saved_statuses(home=tmp_path) == {}

    def test_added_unverified_counts_as_acted_but_failed_does_not(self, tmp_path):
        mc.save_status("cursor", mc.ClientStatus.FAILED, home=tmp_path)
        assert mc.is_any_client_connected(home=tmp_path) is False  # bare failure still nags
        mc.save_status("cursor", mc.ClientStatus.ADDED_UNVERIFIED, home=tmp_path)
        assert mc.is_any_client_connected(home=tmp_path) is True

    def test_save_preserves_other_config_sections(self, tmp_path):
        # A future global setting living in the same file must not be clobbered.
        path = mc.config_path(home=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"backend_url": "http://x", "theme": "dark"}))
        mc.save_status("cursor", mc.ClientStatus.VERIFIED, home=tmp_path)
        data = json.loads(path.read_text())
        assert data["backend_url"] == "http://x"  # preserved
        assert data["theme"] == "dark"  # preserved
        assert data["clients"]["cursor"] == "verified"

    def test_forget_status_removes_client_preserving_others(self, tmp_path):
        mc.save_status("claude_code", mc.ClientStatus.VERIFIED, home=tmp_path)
        mc.save_status("cursor", mc.ClientStatus.ADDED_UNVERIFIED, home=tmp_path)
        mc.forget_status("claude_code", home=tmp_path)
        assert mc.saved_statuses(home=tmp_path) == {"cursor": mc.ClientStatus.ADDED_UNVERIFIED}
        # Forgetting an absent client is a no-op.
        mc.forget_status("nope", home=tmp_path)
        assert mc.saved_statuses(home=tmp_path) == {"cursor": mc.ClientStatus.ADDED_UNVERIFIED}

    def test_malformed_or_unknown_values_read_as_empty(self, tmp_path):
        path = mc.config_path(home=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert mc.saved_statuses(home=tmp_path) == {}
        path.write_text(json.dumps({"clients": {"cursor": "bogus_status"}}))
        assert mc.saved_statuses(home=tmp_path) == {}  # unknown value dropped, no crash


class TestConfigFingerprint:
    def test_equal_blocks_hash_equal(self):
        assert mc.config_fingerprint(BLOCK) == mc.config_fingerprint(BLOCK)

    def test_env_key_order_does_not_matter(self):
        a = mc.ServerBlock("uvx", ("x",), {"A": "1", "B": "2"})
        b = mc.ServerBlock("uvx", ("x",), {"B": "2", "A": "1"})
        assert mc.config_fingerprint(a) == mc.config_fingerprint(b)

    def test_changes_when_an_env_value_changes(self):
        base = mc.ServerBlock("uvx", ("x",), {"LLM_HIGH": "a/b"})
        changed = mc.ServerBlock("uvx", ("x",), {"LLM_HIGH": "c/d"})
        assert mc.config_fingerprint(base) != mc.config_fingerprint(changed)

    def test_changes_when_command_or_args_change(self):
        assert mc.config_fingerprint(BLOCK) != mc.config_fingerprint(
            mc.ServerBlock("uv", BLOCK.args, BLOCK.env)
        )
        assert mc.config_fingerprint(BLOCK) != mc.config_fingerprint(
            mc.ServerBlock(BLOCK.command, BLOCK.args + ("--extra",), BLOCK.env)
        )


class TestFingerprintPersistence:
    def test_empty_when_absent(self, tmp_path):
        assert mc.saved_fingerprints(home=tmp_path) == {}

    def test_round_trip(self, tmp_path):
        mc.save_fingerprint("claude_code", "abc", home=tmp_path)
        mc.save_fingerprint("cursor", "def", home=tmp_path)
        assert mc.saved_fingerprints(home=tmp_path) == {"claude_code": "abc", "cursor": "def"}

    def test_overwrites_prior_fingerprint(self, tmp_path):
        mc.save_fingerprint("cursor", "old", home=tmp_path)
        mc.save_fingerprint("cursor", "new", home=tmp_path)
        assert mc.saved_fingerprints(home=tmp_path)["cursor"] == "new"

    def test_keys_stored_sorted(self, tmp_path):
        mc.save_fingerprint("cursor", "1", home=tmp_path)
        mc.save_fingerprint("claude_code", "2", home=tmp_path)
        section = json.loads(mc.config_path(home=tmp_path).read_text())["client_configs"]
        assert list(section) == sorted(section)

    def test_non_string_values_skipped(self, tmp_path):
        path = mc.config_path(home=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"client_configs": {"cursor": 5, "claude_code": "ok"}}))
        assert mc.saved_fingerprints(home=tmp_path) == {"claude_code": "ok"}

    def test_forget_removes_target_preserving_others(self, tmp_path):
        mc.save_fingerprint("claude_code", "a", home=tmp_path)
        mc.save_fingerprint("cursor", "b", home=tmp_path)
        mc.forget_fingerprint("claude_code", home=tmp_path)
        assert mc.saved_fingerprints(home=tmp_path) == {"cursor": "b"}
        mc.forget_fingerprint("nope", home=tmp_path)  # absent → no-op
        assert mc.saved_fingerprints(home=tmp_path) == {"cursor": "b"}

    def test_fingerprint_and_status_sections_are_independent(self, tmp_path):
        # The two sibling sections must not clobber each other.
        mc.save_status("cursor", mc.ClientStatus.VERIFIED, home=tmp_path)
        mc.save_fingerprint("cursor", "fp", home=tmp_path)
        assert mc.saved_statuses(home=tmp_path) == {"cursor": mc.ClientStatus.VERIFIED}
        assert mc.saved_fingerprints(home=tmp_path) == {"cursor": "fp"}
        # And writing a status after a fingerprint preserves the fingerprint.
        mc.save_status("claude_code", mc.ClientStatus.CONNECTED, home=tmp_path)
        assert mc.saved_fingerprints(home=tmp_path) == {"cursor": "fp"}

    def test_save_preserves_unrelated_top_level_keys(self, tmp_path):
        path = mc.config_path(home=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"theme": "dark", "clients": {"cursor": "verified"}}))
        mc.save_fingerprint("cursor", "fp", home=tmp_path)
        data = json.loads(path.read_text())
        assert data["theme"] == "dark"
        assert data["clients"] == {"cursor": "verified"}
        assert data["client_configs"] == {"cursor": "fp"}


class TestIsStale:
    def test_missing_baseline_is_not_stale(self):
        assert mc.is_stale(None, "x") is False

    def test_matching_is_not_stale(self):
        assert mc.is_stale("x", "x") is False

    def test_differing_is_stale(self):
        assert mc.is_stale("x", "y") is True


class TestStaleConnectedIds:
    def test_connected_with_differing_fingerprint_is_stale(self, tmp_path):
        mc.save_status("cursor", mc.ClientStatus.VERIFIED, home=tmp_path)
        mc.save_fingerprint("cursor", "old", home=tmp_path)
        assert mc.stale_connected_ids("new", home=tmp_path) == ["cursor"]

    def test_connected_with_matching_fingerprint_is_not_stale(self, tmp_path):
        mc.save_status("cursor", mc.ClientStatus.VERIFIED, home=tmp_path)
        mc.save_fingerprint("cursor", "same", home=tmp_path)
        assert mc.stale_connected_ids("same", home=tmp_path) == []

    def test_legacy_connection_without_fingerprint_is_not_stale(self, tmp_path):
        mc.save_status("cursor", mc.ClientStatus.VERIFIED, home=tmp_path)  # no fingerprint recorded
        assert mc.stale_connected_ids("anything", home=tmp_path) == []

    def test_failed_client_is_never_stale(self, tmp_path):
        mc.save_status("cursor", mc.ClientStatus.FAILED, home=tmp_path)
        mc.save_fingerprint("cursor", "old", home=tmp_path)
        assert mc.stale_connected_ids("new", home=tmp_path) == []

    def test_result_is_sorted(self, tmp_path):
        for cid in ("cursor", "claude_code", "gemini"):
            mc.save_status(cid, mc.ClientStatus.VERIFIED, home=tmp_path)
            mc.save_fingerprint(cid, "old", home=tmp_path)
        assert mc.stale_connected_ids("new", home=tmp_path) == ["claude_code", "cursor", "gemini"]


class TestRowOverlay:
    def test_stale_connected_shows_orange_reconnect(self):
        assert mc.row_label(mc.ClientStatus.VERIFIED, stale=True) == mc.STALE_LABEL
        assert mc.row_style(mc.ClientStatus.VERIFIED, stale=True) == mc.STALE_STYLE

    def test_non_stale_connected_renders_normally(self):
        assert mc.row_label(mc.ClientStatus.VERIFIED) == mc.status_label(mc.ClientStatus.VERIFIED)
        assert mc.row_style(mc.ClientStatus.VERIFIED) == mc.status_style(mc.ClientStatus.VERIFIED)

    def test_manual_never_stale_stays_dim(self):
        assert mc.row_style(mc.ClientStatus.CONNECTED, stale=True, is_manual=True) == "dim"
        assert mc.row_label(mc.ClientStatus.CONNECTED, stale=True, is_manual=True) == (
            mc.status_label(mc.ClientStatus.CONNECTED)
        )

    def test_failed_ignores_stale_flag(self):
        assert mc.row_label(mc.ClientStatus.FAILED, stale=True) == (
            mc.status_label(mc.ClientStatus.FAILED)
        )
        assert mc.row_style(mc.ClientStatus.FAILED, stale=True) == (
            mc.status_style(mc.ClientStatus.FAILED)
        )

    def test_not_configured_ignores_stale_flag(self):
        assert mc.row_label(mc.ClientStatus.NOT_CONFIGURED, stale=True) == (
            mc.status_label(mc.ClientStatus.NOT_CONFIGURED)
        )


class TestRenderCliHint:
    def test_lists_registry_clients_from_one_source(self):
        hint = mc.render_cli_hint("/proj/.specflow-local/mcp-config.json")
        assert "claude mcp add-json specflow" in hint
        assert "gemini mcp add specflow" in hint
        assert "Cursor:" in hint
        assert "specflow tui" in hint  # the guided-setup pointer
        assert "/proj/.specflow-local/mcp-config.json" in hint


class TestStatus:
    def test_initial_not_installed_for_missing_cli(self):
        assert mc.initial_status(mc.CLAUDE_CODE, installed=False, saved=None) is (
            mc.ClientStatus.NOT_INSTALLED
        )

    def test_initial_manual_never_not_installed(self):
        assert mc.initial_status(mc.MANUAL, installed=False, saved=None) is (
            mc.ClientStatus.NOT_CONFIGURED
        )

    def test_initial_uses_saved_status_not_assumed_connected(self):
        # An unverified add must come back as exactly that, never "connected".
        assert mc.initial_status(
            mc.CURSOR, installed=True, saved=mc.ClientStatus.ADDED_UNVERIFIED
        ) is mc.ClientStatus.ADDED_UNVERIFIED

    def test_add_failed_when_not_ok(self):
        assert mc.status_after_add(mc.CLAUDE_CODE, add_ok=False) is mc.ClientStatus.FAILED

    def test_claude_verified_only_when_readback_names_server(self):
        assert mc.status_after_add(
            mc.CLAUDE_CODE, add_ok=True, verify_output="specflow: uvx ✔"
        ) is mc.ClientStatus.VERIFIED
        assert mc.status_after_add(
            mc.CLAUDE_CODE, add_ok=True, verify_output="nothing here"
        ) is mc.ClientStatus.FAILED

    def test_cursor_never_verified_caps_at_added_unverified(self):
        assert mc.status_after_add(mc.CURSOR, add_ok=True) is mc.ClientStatus.ADDED_UNVERIFIED

    def test_gemini_added_unverified(self):
        assert mc.status_after_add(mc.GEMINI_CLI, add_ok=True) is mc.ClientStatus.ADDED_UNVERIFIED

    def test_every_status_has_a_plain_label(self):
        for status in mc.ClientStatus:
            label = mc.status_label(status)
            assert label
            # human text, not the bare enum identifier
            assert label != status.value and label != status.name

    def test_every_status_has_a_style(self):
        for status in mc.ClientStatus:
            assert mc.status_style(status)
        assert "red" in mc.status_style(mc.ClientStatus.FAILED)
        assert "green" in mc.status_style(mc.ClientStatus.VERIFIED)

    def test_green_is_reserved_for_connected_states(self):
        green_states = {mc.ClientStatus.CONNECTED, mc.ClientStatus.VERIFIED}
        for status in mc.ClientStatus:
            has_green = "green" in mc.status_style(status)
            assert has_green is (status in green_states), status


class TestClientRows:
    def test_rows_cover_registry_with_flags(self, tmp_path):
        mc.save_status("claude_code", mc.ClientStatus.VERIFIED, home=tmp_path)
        rows = mc.client_rows(
            which=lambda b: "/bin/" + b if b == "claude" else None, home=tmp_path
        )
        by_id = {r.client.client_id: r for r in rows}
        assert set(by_id) == {c.client_id for c in mc.REGISTRY}
        assert by_id["claude_code"].installed is True
        assert by_id["claude_code"].saved is mc.ClientStatus.VERIFIED
        assert by_id["gemini"].installed is False
        assert by_id["gemini"].saved is None
        assert by_id["manual"].installed is True  # manual always available


class TestDescriptions:
    def test_every_client_has_a_description(self):
        for client in mc.REGISTRY:
            assert client.description


class TestSuccessBody:
    def test_verified_says_verified_and_lists_prompts(self):
        body = mc.success_body(mc.CLAUDE_CODE, mc.ClientStatus.VERIFIED)
        assert "verified" in body
        for prompt in mc.USAGE_PROMPTS:
            assert prompt in body
        assert mc.CLAUDE_CODE.restart_hint in body

    def test_unverified_does_not_claim_verified(self):
        body = mc.success_body(mc.CURSOR, mc.ClientStatus.ADDED_UNVERIFIED)
        assert "verified" not in body
        assert "specs/" in body


class TestRegistryGuard:
    def test_check_registry_rejects_duplicate_ids(self):
        dupe = (mc.CLAUDE_CODE, mc.CLAUDE_CODE)
        with pytest.raises(AssertionError, match="duplicate"):
            mc._check_registry(dupe)

    def test_check_registry_rejects_deeplink_without_config_placeholder(self):
        bad = mc.McpClient(
            client_id="bad",
            name="Bad",
            icon="x",
            strategy=mc.AddStrategy.DEEPLINK,
            deeplink_template="cursor://no-placeholder",
            file_target=mc.FileTarget("~/x.json", mc.ConfigShape.MCP_SERVERS),
        )
        with pytest.raises(AssertionError, match="config"):
            mc._check_registry((bad,))

    def test_shipped_registry_is_valid(self):
        mc._check_registry(mc.REGISTRY)  # does not raise
