"""Tests for the CLI (cli.py) covering:

- Config resolution + localhost guard (FR-13)
- run-generation performs expected backend call (FR-4)
- check-status, retry-generation, download-outputs subcommands (FR-4, FR-11)
- sessions command composition (FR-12)
- clear-workspace --set issues 3 clears (FR-5)
- Capacity message rendering with 2 cleaning sets (FR-6)
- format_grace pure-unit tests (FR-6)
- Header parity (no key in local mode, FR-13)
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import services.session as session_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_project_root():
    """Isolate _project_root global between tests."""
    saved = session_mod._project_root
    session_mod._project_root = None
    yield
    session_mod._project_root = saved


@pytest.fixture()
def tmp_project(tmp_path):
    """A minimal project with specs/ and required docs/ artefacts."""
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "app.md").write_text("spec content")

    analysis_dir = tmp_path / "docs" / "analysis"
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "specification_completeness.md").write_text("# Part F\n\nLOCAL_ONLY\n")

    planning_dir = tmp_path / "docs" / "planning"
    planning_dir.mkdir(parents=True)
    (planning_dir / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n## Phase 1\n- task\n")

    return tmp_path


# ---------------------------------------------------------------------------
# format_grace — pure unit tests
# ---------------------------------------------------------------------------


class TestFormatGrace:
    def test_zero_seconds(self):
        from services.cli_service import format_grace
        assert format_grace(0) == "0min"

    def test_negative_seconds(self):
        from services.cli_service import format_grace
        assert format_grace(-1) == "0min"

    def test_minutes_only(self):
        from services.cli_service import format_grace
        assert format_grace(26 * 60) == "26min"

    def test_hours_only(self):
        from services.cli_service import format_grace
        assert format_grace(2 * 3600) == "2h 0min"

    def test_hours_and_minutes(self):
        from services.cli_service import format_grace
        assert format_grace(90 * 60) == "1h 30min"

    def test_59_seconds_rounds_to_0_min(self):
        from services.cli_service import format_grace
        assert format_grace(59) == "0min"

    def test_one_hour_exactly(self):
        from services.cli_service import format_grace
        assert format_grace(3600) == "1h 0min"


# ---------------------------------------------------------------------------
# render_capacity_message
# ---------------------------------------------------------------------------


class TestRenderCapacityMessage:
    def test_two_cleaning_sets(self):
        from services.cli_service import render_capacity_message
        cleaning_sets = [
            {"set_number": 1, "remaining_grace_seconds": 5400},
            {"set_number": 2, "remaining_grace_seconds": 1560},
        ]
        msg = render_capacity_message(cleaning_sets)
        assert "No workspaces available" in msg
        assert "Set 1 (left 1h 30min)" in msg
        assert "Set 2 (left 26min)" in msg
        assert "clear-workspace --set 1" in msg

    def test_single_set(self):
        from services.cli_service import render_capacity_message
        cleaning_sets = [{"set_number": 3, "remaining_grace_seconds": 7200}]
        msg = render_capacity_message(cleaning_sets)
        assert "Set 3 (left 2h 0min)" in msg
        assert "clear-workspace --set 3" in msg

    def test_empty_list_still_renders(self):
        from services.cli_service import render_capacity_message
        msg = render_capacity_message([])
        assert "No workspaces available" in msg


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestResolveBackendConfig:
    def test_flag_takes_priority_over_env(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        monkeypatch.setenv("BACKEND_URL", "http://env-url:8000")
        url, _, _ = resolve_backend_config("http://flag-url:8000", None, None, tmp_path)
        assert url == "http://flag-url:8000"

    def test_env_takes_priority_over_mcp_config(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        # Write mcp-config.json
        cfg_dir = tmp_path / ".specflow-local"
        cfg_dir.mkdir()
        (cfg_dir / "mcp-config.json").write_text(
            json.dumps({"mcpServers": {"specflow": {"env": {"BACKEND_URL": "http://config-url"}}}})
        )
        monkeypatch.setenv("BACKEND_URL", "http://env-url:8000")
        url, _, _ = resolve_backend_config(None, None, None, tmp_path)
        assert url == "http://env-url:8000"

    def test_mcp_config_fallback(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        monkeypatch.delenv("BACKEND_URL", raising=False)
        cfg_dir = tmp_path / ".specflow-local"
        cfg_dir.mkdir()
        (cfg_dir / "mcp-config.json").write_text(
            json.dumps({"mcpServers": {"specflow": {"env": {"BACKEND_URL": "http://config-url:9000"}}}})
        )
        url, _, _ = resolve_backend_config(None, None, None, tmp_path)
        assert url == "http://config-url:9000"

    def test_defaults_to_localhost(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        monkeypatch.delenv("BACKEND_URL", raising=False)
        url, _, _ = resolve_backend_config(None, None, None, tmp_path)
        assert url == "http://127.0.0.1:8000"

    def test_workspace_count_from_flag(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        _, _, wc = resolve_backend_config(None, None, 2, tmp_path)
        assert wc == 2

    def test_workspace_count_from_env(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        monkeypatch.setenv("WORKSPACE_COUNT", "3")
        _, _, wc = resolve_backend_config(None, None, None, tmp_path)
        assert wc == 3

    def test_workspace_count_from_mcp_config(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        monkeypatch.delenv("WORKSPACE_COUNT", raising=False)
        cfg_dir = tmp_path / ".specflow-local"
        cfg_dir.mkdir()
        (cfg_dir / "mcp-config.json").write_text(
            json.dumps({"mcpServers": {"specflow": {"env": {"WORKSPACE_COUNT": "2"}}}})
        )
        _, _, wc = resolve_backend_config(None, None, None, tmp_path)
        assert wc == 2

    def test_invalid_workspace_count_env_ignored(self, tmp_path, monkeypatch):
        from cli import resolve_backend_config
        monkeypatch.setenv("WORKSPACE_COUNT", "99")
        _, _, wc = resolve_backend_config(None, None, None, tmp_path)
        assert wc is None


# ---------------------------------------------------------------------------
# Localhost guard
# ---------------------------------------------------------------------------


class TestLocalhostGuard:
    def test_localhost_passes(self):
        from cli import check_localhost_guard
        check_localhost_guard("http://localhost:8000", force=False)  # no exception

    def test_127_0_0_1_passes(self):
        from cli import check_localhost_guard
        check_localhost_guard("http://127.0.0.1:8000", force=False)

    def test_remote_url_raises_sys_exit(self):
        from cli import check_localhost_guard
        with pytest.raises(SystemExit) as exc_info:
            check_localhost_guard("https://specflow.example.com", force=False)
        assert exc_info.value.code != 0

    def test_remote_url_with_force_passes(self):
        from cli import check_localhost_guard
        check_localhost_guard("https://specflow.example.com", force=True)  # no exception

    def test_non_localhost_exit_code_nonzero(self):
        from cli import check_localhost_guard
        with pytest.raises(SystemExit) as exc_info:
            check_localhost_guard("http://192.168.1.100:8000", force=False)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Header parity (no API key in local mode)
# ---------------------------------------------------------------------------


class TestHeaderParity:
    def test_no_api_key_in_local_mode(self, monkeypatch):
        """CLI never sends X-API-Key when SPECFLOW_API_KEY is not set."""
        from services.specflow_backend import SpecFlowBackendService
        monkeypatch.delenv("SPECFLOW_API_KEY", raising=False)
        monkeypatch.setenv("USER_EMAIL", "local@example.com")
        svc = SpecFlowBackendService()
        headers = svc._get_headers()
        assert "X-API-Key" not in headers
        assert headers.get("X-User-Email") == "local@example.com"


# ---------------------------------------------------------------------------
# cmd_check_status
# ---------------------------------------------------------------------------


class TestCheckStatus:
    @pytest.mark.asyncio
    async def test_calls_status_endpoint(self, tmp_project):
        from cli import cmd_check_status
        from services.session import write_session
        write_session("gen-abc", tmp_project)

        args = SimpleNamespace(root_path=str(tmp_project), command="check-status")

        with patch(
            "services.specflow_backend.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep:
            mock_ep.return_value = json.dumps({"status": "running", "checkpoint": "kb_init_done"})
            code = await cmd_check_status(args)

        assert code == 0
        mock_ep.assert_called_once()
        call_kwargs = mock_ep.call_args
        assert "gen-abc" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_no_session_prints_message(self, tmp_path, capsys):
        from cli import cmd_check_status
        args = SimpleNamespace(root_path=str(tmp_path), command="check-status")
        code = await cmd_check_status(args)
        out = capsys.readouterr().out
        assert code == 0
        assert "run-generation" in out.lower() or "no active" in out.lower()


# ---------------------------------------------------------------------------
# cmd_retry_generation
# ---------------------------------------------------------------------------


class TestRetryGeneration:
    @pytest.mark.asyncio
    async def test_calls_retry_endpoint(self, tmp_project):
        from cli import cmd_retry_generation
        from services.session import write_session
        write_session("gen-xyz", tmp_project)

        args = SimpleNamespace(root_path=str(tmp_project), command="retry-generation")

        # check_status_safe returns failed → proceed to retry POST
        with patch(
            "services.tool_helpers.check_status_safe",
            new_callable=AsyncMock,
        ) as mock_status, patch(
            "services.specflow_backend.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep:
            mock_status.return_value = {"status": "failed"}
            mock_ep.return_value = json.dumps({"status": "running", "retry_count": 1})
            code = await cmd_retry_generation(args)

        assert code == 0
        # Exactly one backend call: the retry POST
        assert mock_ep.call_count == 1
        assert mock_status.call_count == 1

    @pytest.mark.asyncio
    async def test_blocks_when_already_running(self, tmp_project, capsys):
        from cli import cmd_retry_generation
        from services.session import write_session
        write_session("gen-running", tmp_project)

        args = SimpleNamespace(root_path=str(tmp_project), command="retry-generation")

        with patch(
            "services.tool_helpers.check_status_safe",
            new_callable=AsyncMock,
        ) as mock_cs:
            mock_cs.return_value = {"status": "running"}
            code = await cmd_retry_generation(args)

        assert code == 1


# ---------------------------------------------------------------------------
# cmd_download_outputs
# ---------------------------------------------------------------------------


class TestDownloadOutputs:
    @pytest.mark.asyncio
    async def test_calls_outputs_endpoint(self, tmp_project):
        from cli import cmd_download_outputs
        from services.session import write_session
        import io
        import tarfile

        write_session("gen-dl", tmp_project)

        # Build a minimal tarball
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"content"
            info = tarfile.TarInfo(name="gen-dl/ws-01-1/output.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        archive_bytes = buf.getvalue()

        args = SimpleNamespace(
            root_path=str(tmp_project),
            command="download-outputs",
            generation_id=None,
            outputs_dir="docs",
        )

        with patch(
            "services.specflow_backend.SpecFlowBackendService.call_backend_bytes",
            new_callable=AsyncMock,
        ) as mock_bytes:
            mock_bytes.return_value = archive_bytes
            code = await cmd_download_outputs(args)

        assert code == 0

    @pytest.mark.asyncio
    async def test_prints_absolute_destination(self, tmp_project, capsys):
        from cli import cmd_download_outputs
        from services.session import write_session
        import io
        import tarfile

        write_session("gen-dest", tmp_project)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"x"
            info = tarfile.TarInfo(name="gen-dest/f.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        archive_bytes = buf.getvalue()

        args = SimpleNamespace(
            root_path=str(tmp_project),
            command="download-outputs",
            generation_id=None,
            outputs_dir="docs",
        )

        with patch(
            "services.specflow_backend.SpecFlowBackendService.call_backend_bytes",
            new_callable=AsyncMock,
        ) as mock_bytes:
            mock_bytes.return_value = archive_bytes
            await cmd_download_outputs(args)

        out = capsys.readouterr().out
        # Absolute destination must appear in output
        assert str(tmp_project / "docs") in out

    @pytest.mark.asyncio
    async def test_no_generation_id_returns_error(self, tmp_path, capsys):
        from cli import cmd_download_outputs
        args = SimpleNamespace(
            root_path=str(tmp_path),
            command="download-outputs",
            generation_id=None,
            outputs_dir="docs",
        )
        code = await cmd_download_outputs(args)
        assert code == 1


# ---------------------------------------------------------------------------
# cmd_sessions
# ---------------------------------------------------------------------------


class TestSessions:
    @pytest.mark.asyncio
    async def test_composes_auth_me_and_status(self, capsys):
        from services.cli_service import fetch_sessions

        auth_resp = json.dumps({
            "active_generation_sessions": [
                {"generation_id": "gen-001"},
                {"generation_id": "gen-002"},
            ]
        })
        status_resp_1 = json.dumps({"status": "running", "checkpoint": "generation_started"})
        status_resp_2 = json.dumps({"status": "running", "checkpoint": "kb_init_done"})

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep:
            mock_ep.side_effect = [auth_resp, status_resp_1, status_resp_2]
            sessions = await fetch_sessions()

        assert len(sessions) == 2
        assert sessions[0]["generation_id"] == "gen-001"
        assert sessions[0]["status"] == "running"
        assert sessions[1]["generation_id"] == "gen-002"
        assert mock_ep.call_count == 3  # 1 auth/me + 2 status calls

    @pytest.mark.asyncio
    async def test_empty_active_sessions(self):
        from services.cli_service import fetch_sessions

        auth_resp = json.dumps({"active_generation_sessions": []})

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep:
            mock_ep.return_value = auth_resp
            sessions = await fetch_sessions()

        assert sessions == []

    @pytest.mark.asyncio
    async def test_cmd_sessions_renders_table(self, capsys):
        from cli import cmd_sessions
        args = SimpleNamespace(command="sessions")

        auth_resp = json.dumps({
            "active_generation_sessions": [{"generation_id": "gen-table"}]
        })
        status_resp = json.dumps({"status": "running", "checkpoint": "generation_started"})

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep:
            mock_ep.side_effect = [auth_resp, status_resp]
            code = await cmd_sessions(args)

        out = capsys.readouterr().out
        assert code == 0
        assert "gen-table" in out
        assert "running" in out

    @pytest.mark.asyncio
    async def test_cmd_sessions_no_active(self, capsys):
        from cli import cmd_sessions
        args = SimpleNamespace(command="sessions")

        auth_resp = json.dumps({"active_generation_sessions": []})

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep:
            mock_ep.return_value = auth_resp
            code = await cmd_sessions(args)

        out = capsys.readouterr().out
        assert code == 0
        assert "no active" in out.lower()


# ---------------------------------------------------------------------------
# sessions --watch: desktop notification on terminal state
# ---------------------------------------------------------------------------


class TestSessionsWatch:
    @pytest.mark.asyncio
    async def test_notifies_on_completed(self, capsys):
        """--watch fires notify_desktop when a session reaches 'completed'."""
        from cli import cmd_sessions
        from unittest.mock import patch, AsyncMock
        import asyncio

        auth_resp = json.dumps({
            "active_generation_sessions": [{"generation_id": "gen-watch-01"}]
        })
        status_resp = json.dumps({"status": "completed", "checkpoint": "outputs_archived"})

        async def fake_sleep(_):
            raise asyncio.CancelledError

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep, patch(
            "tui.poller.notify_desktop",
        ) as mock_notify, patch(
            "asyncio.sleep",
            side_effect=fake_sleep,
        ):
            mock_ep.side_effect = [auth_resp, status_resp]
            args = SimpleNamespace(command="sessions", watch=True, interval=1)
            code = await cmd_sessions(args)

        assert code == 0
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert "completed" in call_args.kwargs.get("title", "") or "completed" in str(call_args)

    @pytest.mark.asyncio
    async def test_notifies_on_failed(self, capsys):
        """--watch fires notify_desktop when a session reaches 'failed'."""
        from cli import cmd_sessions
        import asyncio

        auth_resp = json.dumps({
            "active_generation_sessions": [{"generation_id": "gen-fail-01"}]
        })
        status_resp = json.dumps({"status": "failed", "checkpoint": "generation_started"})

        async def fake_sleep(_):
            raise asyncio.CancelledError

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep, patch(
            "tui.poller.notify_desktop",
        ) as mock_notify, patch(
            "asyncio.sleep",
            side_effect=fake_sleep,
        ):
            mock_ep.side_effect = [auth_resp, status_resp]
            args = SimpleNamespace(command="sessions", watch=True, interval=1)
            await cmd_sessions(args)

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert "failed" in call_args.kwargs.get("title", "") or "failed" in str(call_args)

    @pytest.mark.asyncio
    async def test_notifies_when_run_is_freshly_queued(self, capsys):
        """--watch fires a 'started' notification for a freshly queued run."""
        from cli import cmd_sessions
        import asyncio

        auth_resp = json.dumps({
            "active_generation_sessions": [{"generation_id": "gen-running-02"}]
        })
        status_resp = json.dumps({"status": "pending", "checkpoint": ""})

        async def fake_sleep(_):
            raise asyncio.CancelledError

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep, patch(
            "tui.poller.notify_desktop",
        ) as mock_notify, patch(
            "asyncio.sleep",
            side_effect=fake_sleep,
        ):
            mock_ep.side_effect = [auth_resp, status_resp]
            args = SimpleNamespace(command="sessions", watch=True, interval=1)
            await cmd_sessions(args)

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert "started" in call_args.kwargs.get("title", "") or "started" in str(call_args)

    @pytest.mark.asyncio
    async def test_does_not_notify_when_attaching_to_running(self, capsys):
        """--watch stays silent when first observing an already-running session."""
        from cli import cmd_sessions
        import asyncio

        auth_resp = json.dumps({
            "active_generation_sessions": [{"generation_id": "gen-running-03"}]
        })
        status_resp = json.dumps({"status": "running", "checkpoint": "generation_started"})

        async def fake_sleep(_):
            raise asyncio.CancelledError

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep, patch(
            "tui.poller.notify_desktop",
        ) as mock_notify, patch(
            "asyncio.sleep",
            side_effect=fake_sleep,
        ):
            mock_ep.side_effect = [auth_resp, status_resp]
            args = SimpleNamespace(command="sessions", watch=True, interval=1)
            await cmd_sessions(args)

        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_notifies_only_once_per_session(self, capsys):
        """--watch does not fire duplicate notifications for the same generation_id."""
        from cli import cmd_sessions
        import asyncio

        # Two poll cycles — same session completed in both
        auth_resp = json.dumps({
            "active_generation_sessions": [{"generation_id": "gen-dedup-01"}]
        })
        status_resp = json.dumps({"status": "completed", "checkpoint": "outputs_archived"})

        poll_count = 0

        async def fake_sleep(_):
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 2:
                raise asyncio.CancelledError

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep, patch(
            "tui.poller.notify_desktop",
        ) as mock_notify, patch(
            "asyncio.sleep",
            side_effect=fake_sleep,
        ):
            # Auth + status returned for each poll cycle
            mock_ep.side_effect = [auth_resp, status_resp, auth_resp, status_resp]
            args = SimpleNamespace(command="sessions", watch=True, interval=1)
            await cmd_sessions(args)

        assert mock_notify.call_count == 1

    @pytest.mark.asyncio
    async def test_notifies_on_checkpoint_and_workspace_phase_progress(self, capsys):
        """--watch reuses MilestoneTracker for checkpoint and workspace phase updates."""
        from cli import cmd_sessions
        import asyncio

        auth_resp = json.dumps({
            "active_generation_sessions": [{"generation_id": "gen-progress-01"}]
        })
        first_status = json.dumps({
            "status": "running",
            "checkpoint": "kb_init_done",
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 1, "phase_name": "Auth"},
            },
        })
        second_status = json.dumps({
            "status": "running",
            "checkpoint": "generation_started",
            "current_phase": "Generating",
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 2, "phase_name": "Payments"},
            },
        })

        poll_count = 0

        async def fake_sleep(_):
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 2:
                raise asyncio.CancelledError

        with patch(
            "services.cli_service.call_backend_endpoint",
            new_callable=AsyncMock,
        ) as mock_ep, patch(
            "tui.poller.notify_desktop",
        ) as mock_notify, patch(
            "asyncio.sleep",
            side_effect=fake_sleep,
        ):
            mock_ep.side_effect = [auth_resp, first_status, auth_resp, second_status]
            args = SimpleNamespace(command="sessions", watch=True, interval=1)
            await cmd_sessions(args)

        # First poll (already-running) is a silent baseline; the second poll
        # fires one checkpoint-advance and one workspace-phase notification.
        assert mock_notify.call_count == 2
        messages = " ".join(str(call) for call in mock_notify.call_args_list)
        assert "Generating" in messages
        assert "Payments" in messages


class TestNotifyDesktop:
    def test_uses_plyer_before_platform_fallback(self, monkeypatch):
        """Plyer is the primary cross-platform notification backend."""
        from services.cli_service import notify_desktop
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock

        monkeypatch.setattr(sys, "platform", "darwin")
        notify = SimpleNamespace(notify=Mock())
        monkeypatch.setitem(sys.modules, "plyer", SimpleNamespace(notification=notify))
        with patch("services.cli_service._prepare_macos_plyer_notification", return_value=None) as prep, patch(
            "subprocess.run"
        ) as run:
            notify_desktop(title="Test", message="msg")

        prep.assert_called_once()
        notify.notify.assert_called_once_with(
            title="Test",
            message="msg",
            app_name="SpecFlow",
            timeout=10,
        )
        run.assert_not_called()

    def test_macos_fallback_after_plyer_failure(self, monkeypatch):
        """macOS osascript fallback runs (with escaped args) after plyer fails."""
        from services.cli_service import notify_desktop
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock

        monkeypatch.setattr(sys, "platform", "darwin")
        notify = SimpleNamespace(notify=Mock(side_effect=RuntimeError("boom")))
        monkeypatch.setitem(sys.modules, "plyer", SimpleNamespace(notification=notify))
        with patch(
            "services.cli_service._prepare_macos_plyer_notification",
            return_value=None,
        ), patch(
            "subprocess.run",
        ) as run:
            run.return_value.returncode = 0
            run.return_value.stderr = b""
            notify_desktop(title='Quote "title"', message='Quote "msg"')

        run.assert_called_once()
        args = run.call_args.args[0]
        assert args[:2] == ["osascript", "-e"]
        # Quotes in the message/title are JSON-escaped, not injected raw.
        assert '\\"title\\"' in args[2]
        assert '\\"msg\\"' in args[2]

    def test_plyer_no_suitable_implementation_is_captured(self, monkeypatch, capsys):
        """Plyer backend warnings must not paint over the TUI; fallback still runs."""
        from services.cli_service import notify_desktop
        import logging
        import sys
        from types import SimpleNamespace

        def noisy_notify(**_kwargs):
            logging.getLogger("plyer").warning("Plyer - no suitable implementation found!")
            print("stdout noise")

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setitem(
            sys.modules,
            "plyer",
            SimpleNamespace(notification=SimpleNamespace(notify=noisy_notify)),
        )
        with patch(
            "services.cli_service._prepare_macos_plyer_notification",
            return_value=None,
        ), patch(
            "subprocess.run",
        ) as run:
            run.return_value.returncode = 0
            run.return_value.stderr = b""
            notify_desktop(title="Test", message="msg")

        # Plyer's chatter is captured/discarded — nothing leaks to the terminal —
        # and the "no suitable implementation" signal triggers the fallback.
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        run.assert_called_once()

    def test_silently_ignores_missing_plyer(self, monkeypatch):
        """notify_desktop never raises even when plyer is unavailable."""
        from services.cli_service import notify_desktop
        import sys

        monkeypatch.setattr(sys, "platform", "linux")
        # Simulate plyer not installed
        monkeypatch.setitem(sys.modules, "plyer", None)
        monkeypatch.setitem(sys.modules, "plyer.notification", None)
        with patch("subprocess.run"):
            notify_desktop(title="Test", message="msg")

    def test_silently_ignores_subprocess_failure(self, monkeypatch):
        """notify_desktop never raises on subprocess failure."""
        from services.cli_service import notify_desktop
        import sys

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setitem(sys.modules, "plyer", None)
        with patch("subprocess.run", side_effect=OSError("not found")):
            notify_desktop(title="Test", message="msg")


# ---------------------------------------------------------------------------
# cmd_clear_workspace (set-integral, 3 members)
# ---------------------------------------------------------------------------


class TestClearWorkspace:
    @pytest.mark.asyncio
    async def test_issues_3_clears_for_set(self):
        from cli import cmd_clear_workspace
        from services.cli_service import workspace_ids_for_set

        args = SimpleNamespace(command="clear-workspace", set=1, yes=True)

        with patch(
            "services.cli_service.SpecFlowBackendService.call_backend",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.return_value = '{"status": "ok"}'
            code = await cmd_clear_workspace(args)

        assert code == 0
        assert mock_call.call_count == 3
        called_ids = [call.kwargs.get("endpoint", "") for call in mock_call.call_args_list]
        for ws_id in workspace_ids_for_set(1):
            assert f"/api/v1/workspace/{ws_id}/clear" in called_ids

    @pytest.mark.asyncio
    async def test_set_2_uses_correct_ids(self):
        from cli import cmd_clear_workspace
        from services.cli_service import workspace_ids_for_set

        args = SimpleNamespace(command="clear-workspace", set=2, yes=True)

        with patch(
            "services.cli_service.SpecFlowBackendService.call_backend",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.return_value = '{"status": "ok"}'
            await cmd_clear_workspace(args)

        called_ids = [call.kwargs.get("endpoint", "") for call in mock_call.call_args_list]
        for ws_id in workspace_ids_for_set(2):
            assert f"/api/v1/workspace/{ws_id}/clear" in called_ids

    @pytest.mark.asyncio
    async def test_backend_error_returns_nonzero(self):
        from cli import cmd_clear_workspace

        args = SimpleNamespace(command="clear-workspace", set=1, yes=True)

        with patch(
            "services.cli_service.SpecFlowBackendService.call_backend",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.side_effect = Exception("connection refused")
            code = await cmd_clear_workspace(args)

        assert code == 1

    @pytest.mark.asyncio
    async def test_yes_flag_skips_confirmation(self):
        from cli import cmd_clear_workspace
        args = SimpleNamespace(command="clear-workspace", set=1, yes=True)

        with patch(
            "services.cli_service.SpecFlowBackendService.call_backend",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.return_value = '{"status": "ok"}'
            # Should not prompt — if input() were called it would raise EOFError
            with patch("builtins.input", side_effect=EOFError("should not prompt")):
                code = await cmd_clear_workspace(args)

        assert code == 0


# ---------------------------------------------------------------------------
# workspace_ids_for_set convention
# ---------------------------------------------------------------------------


class TestWorkspaceIdsForSet:
    def test_set_1_has_3_members(self):
        from services.cli_service import workspace_ids_for_set
        ids = workspace_ids_for_set(1)
        assert len(ids) == 3

    def test_set_1_names(self):
        from services.cli_service import workspace_ids_for_set
        ids = workspace_ids_for_set(1)
        assert ids == ["ws-01-1", "ws-01-2", "ws-01-3"]

    def test_set_2_names(self):
        from services.cli_service import workspace_ids_for_set
        ids = workspace_ids_for_set(2)
        assert ids == ["ws-02-1", "ws-02-2", "ws-02-3"]


# ---------------------------------------------------------------------------
# run-generation: pre-run notice
# ---------------------------------------------------------------------------


class TestRunGenerationPreRunNotice:
    @pytest.mark.asyncio
    async def test_pre_run_notice_in_output(self, tmp_project, capsys):
        """run-generation must print the pre-run notice (FR-8, LV4.2)."""
        from cli import cmd_run_generation

        args = SimpleNamespace(
            root_path=str(tmp_project),
            command="run-generation",
            spec_dir="specs",
            outputs_dir="docs",
            src_dir="src",
            workspace_count=None,
        )

        with patch(
            "services.generation_orchestrator.GenerationOrchestrator.run_generation",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.return_value = {"generation_id": "gen-notice-test", "status": "pending"}
            with patch("services.session.write_session"):
                await cmd_run_generation(args)

        out = capsys.readouterr().out
        assert "outputs" in out.lower() or "archived" in out.lower() or "nothing is lost" in out.lower()


# ---------------------------------------------------------------------------
# cmd_init — IDE-registration hint is derived from the client registry
# ---------------------------------------------------------------------------


class TestCmdInitHint:
    @pytest.mark.asyncio
    async def test_prints_registry_derived_hint_on_success(self, tmp_path, capsys):
        from cli import cmd_init

        args = SimpleNamespace(
            root_path=str(tmp_path),
            max_parallel_runs=None,
            skip_build=False,
            reset_local_db=False,
            provide_own_repos=None,
            dry_run=False,
        )
        with patch("cli.local_env.repo_root", return_value=tmp_path), patch(
            "cli.local_env.env_exists", return_value=True
        ), patch(
            "cli.local_env.run_init", new_callable=AsyncMock, return_value=0
        ), patch(
            "cli.local_env.mcp_config_path",
            return_value=tmp_path / ".specflow-local" / "mcp-config.json",
        ):
            code = await cmd_init(args)

        out = capsys.readouterr().out
        assert code == 0
        # Lines come from mcp_clients.render_cli_hint (single source of truth).
        assert "claude mcp add-json specflow" in out
        assert "gemini mcp add specflow" in out
        assert "specflow tui" in out

    @pytest.mark.asyncio
    async def test_dry_run_skips_hint(self, tmp_path, capsys):
        from cli import cmd_init

        args = SimpleNamespace(
            root_path=str(tmp_path),
            max_parallel_runs=None,
            skip_build=False,
            reset_local_db=False,
            provide_own_repos=None,
            dry_run=True,
        )
        with patch("cli.local_env.repo_root", return_value=tmp_path), patch(
            "cli.local_env.env_exists", return_value=True
        ), patch("cli.local_env.run_init", new_callable=AsyncMock, return_value=0):
            code = await cmd_init(args)

        out = capsys.readouterr().out
        assert code == 0
        assert "claude mcp add-json" not in out
