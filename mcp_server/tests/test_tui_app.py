"""Smoke tests for the Textual app rendering and the CLI guarded import.

The full interactive app needs a TTY, so we exercise the pure renderable
builders (which use Rich) and the non-TTY plain-status path. Textual is
imported by tui.app; tests skip cleanly if the optional extra is absent.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from textual.widgets import Input

import pytest

pytest.importorskip("textual")

from rich.console import Console  # noqa: E402

from tui import app as tui_app  # noqa: E402


def _running_payload() -> dict:
    return {
        "generation_id": "gen_8f3abc21",
        "status": "running",
        "current_phase": "Generating code",
        "checkpoint": "generation_started",
        "workspace_count": 3,
        "total_tokens_used_display": "12.4M",
        "num_turns": 410,
        "progress": {
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 6, "total_phases": 9, "phase_name": "Auth API"},
            }
        },
    }


class TestBuildDashboard:
    def _render(self, renderable) -> str:
        console = Console(width=90, record=True)
        console.print(renderable)
        return console.export_text()

    def test_running_renders_pipeline_and_workspaces(self):
        out = self._render(
            tui_app.build_dashboard(_running_payload(), Path("/tmp/acme"), "gen_8f3abc21")
        )
        assert "Pipeline" in out
        assert "Generating code" in out
        assert "ws-01-1" in out
        assert "67%" in out

    def test_completed_renders_estimate(self):
        payload = {
            "generation_id": "gen_x",
            "status": "completed",
            "checkpoint": "estimation_done",
            "progress": {"workspace_phases": {}},
            "result": {
                "summary": {
                    "average_hours": 318,
                    "min_hours": 291,
                    "max_hours": 344,
                    "coefficient_of_variation": 0.08,
                    "variance_assessment": "low",
                    "risk_assessment": {"status": "Approved"},
                },
                "workspace_estimations": [],
            },
        }
        out = self._render(tui_app.build_dashboard(payload, Path("/tmp/acme"), "gen_x"))
        assert "P10Y estimate" in out
        assert "318" in out

    def test_none_payload_renders_waiting(self):
        out = self._render(tui_app.build_dashboard(None, Path("/tmp/x"), "gen_x"))
        assert "Waiting for status" in out


def _ws_usage_payload() -> dict:
    return {
        "generation_id": "gen_x",
        "status": "running",
        "checkpoint": "generation_started",
        "workspace_count": 2,
        "workspace_phases": {
            "ws-01-1": {
                "last_completed_phase": 3,
                "total_phases": 9,
                "phase_name": "Auth API",
                "models": ["claude-sonnet-4"],
                "usage": {
                    "num_turns": 12,
                    "input_tokens": 1_200_000,
                    "output_tokens": 240_000,
                    "cache_write_tokens": 5_000,
                    "cache_read_tokens": 800,
                    "total_tokens": 1_445_800,
                },
            },
        },
    }


class _Event:
    def __init__(self, **kw):
        self.timestamp = kw.get("timestamp", "2026-06-26T14:02:31+00:00")
        self.kind = kw.get("kind", "assistant_text")
        self.message = kw.get("message", "")
        self.tool_name = kw.get("tool_name")
        self.subagent_name = kw.get("subagent_name")


def _events_iter(events):
    async def _gen(generation_id, workspace_id):
        for e in events:
            yield e

    return _gen


class TestWorkspaceStatsPanel:
    def _render(self, renderable) -> str:
        console = Console(width=90, record=True)
        console.print(renderable)
        return console.export_text()

    def test_renders_usage(self):
        out = self._render(tui_app.build_workspace_stats(_ws_usage_payload(), "ws-01-1"))
        assert "claude-sonnet-4" in out
        assert "Auth API" in out
        assert "1.2M" in out  # input tokens, compact
        assert "12" in out  # turns

    def test_placeholder_when_workspace_absent(self):
        out = self._render(tui_app.build_workspace_stats(_ws_usage_payload(), "ws-99-9"))
        assert "not reported yet" in out

    def test_none_payload_is_placeholder(self):
        out = self._render(tui_app.build_workspace_stats(None, "ws-01-1"))
        assert "not reported yet" in out

    def test_phase_known_but_no_usage_shows_pending_hint(self):
        # Workspace reported (phase counters present) but no usage yet — e.g. KB
        # init still running. Show the "completes" hint, not a row of zeros.
        payload = {
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 0, "total_phases": 3, "phase_name": "Auth API"},
            }
        }
        out = self._render(tui_app.build_workspace_stats(payload, "ws-01-1"))
        assert "appear when this step completes" in out
        assert "Total tokens" not in out


class TestStreamRowText:
    def test_includes_time_kind_label_message(self):
        from tui import render

        row = render.StreamRow(time="14:02:31", kind="tool_use", label="Bash", message="ls -la")
        console = Console(width=90, record=True)
        console.print(tui_app.stream_row_text(row))
        out = console.export_text()
        assert "14:02:31" in out
        assert "tool_use" in out
        assert "Bash" in out
        assert "ls -la" in out


class TestPlainStatus:
    def test_includes_status_and_workspace(self):
        out = tui_app._plain_status(_running_payload(), "gen_8f3abc21")
        assert "running" in out
        assert "ws-01-1" in out
        assert "67%" in out

    def test_unavailable_payload(self):
        out = tui_app._plain_status(None, "gen_x")
        assert "unavailable" in out


class TestRunTuiNonTty:
    @pytest.mark.asyncio
    async def test_non_tty_prints_plain_status_without_app(self, capsys):
        args = SimpleNamespace(root_path="/tmp/proj", generation_id="gen_x", interval=3)
        with (
            patch("tui.app.sys.stdout.isatty", return_value=False),
            patch("tui.app.local_env.is_setup_complete", return_value=True),
            patch("tui.app.resolve_generation_id", return_value="gen_x"),
            patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())),
        ):
            rc = await tui_app.run_tui(args)
        assert rc == 0
        assert "running" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_non_tty_not_setup_points_to_init(self, capsys):
        args = SimpleNamespace(root_path="/tmp/proj", generation_id=None, interval=3)
        with (
            patch("tui.app.sys.stdout.isatty", return_value=False),
            patch("tui.app.local_env.is_setup_complete", return_value=False),
        ):
            rc = await tui_app.run_tui(args)
        assert rc == 1
        assert "specflow init" in capsys.readouterr().out


class TestRunTuiOutsideCheckout:
    @pytest.mark.asyncio
    async def test_interactive_launches_from_resolved_checkout(self, tmp_path):
        """A TTY launch from any folder uses the checkout resolved from the
        install's own location — the app is built with that root, not cwd."""
        args = SimpleNamespace(root_path="/tmp/some-other-folder", generation_id=None, interval=3)
        with (
            patch("tui.app.sys.stdout.isatty", return_value=True),
            patch("tui.app.local_env.resolve_repo_root", return_value=tmp_path),
            patch("tui.app.resolve_generation_id", return_value=None),
            patch("tui.app.SpecFlowTUI") as app_cls,
        ):
            app_cls.return_value.run_async = AsyncMock(return_value=None)
            rc = await tui_app.run_tui(args)
        assert rc == 0
        assert app_cls.call_args.kwargs["root"] == tmp_path

    @pytest.mark.asyncio
    async def test_interactive_no_checkout_refuses_visibly(self, capsys):
        """When no checkout can be resolved (non-editable install, no cwd match),
        print an actionable message and return non-zero — never flash the app."""
        args = SimpleNamespace(root_path="/tmp/not-a-checkout", generation_id=None, interval=3)
        with (
            patch("tui.app.sys.stdout.isatty", return_value=True),
            patch("tui.app.local_env.resolve_repo_root", return_value=None),
            patch("tui.app.SpecFlowTUI") as app_cls,
        ):
            rc = await tui_app.run_tui(args)
        assert rc == 1
        app_cls.assert_not_called()
        assert "checkout" in capsys.readouterr().err


def _gate_ready():
    """Patches that make the startup gate pass straight through to the app."""
    return (
        patch("tui.app.local_env.is_setup_complete", return_value=True),
        patch("tui.app.local_env.containers_running", return_value=True),
        patch("tui.app.local_env.backend_ready", new=AsyncMock(return_value=True)),
    )


class TestStartupGate:
    @pytest.mark.asyncio
    async def test_ready_gen_lands_on_dashboard(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.DashboardScreen)

    @pytest.mark.asyncio
    async def test_ready_no_gen_lands_on_sessions(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)

    @pytest.mark.asyncio
    async def test_containers_down_prompts_start(self):
        with (
            patch("tui.app.local_env.is_setup_complete", return_value=True),
            patch("tui.app.local_env.containers_running", return_value=False),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.StartContainersScreen)

    @pytest.mark.asyncio
    async def test_start_no_quits_app(self):
        with (
            patch("tui.app.local_env.is_setup_complete", return_value=True),
            patch("tui.app.local_env.containers_running", return_value=False),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("n")
                await pilot.pause()
                assert not app.is_running

    @pytest.mark.asyncio
    async def test_start_yes_starts_then_proceeds(self):
        with (
            patch("tui.app.local_env.is_setup_complete", return_value=True),
            patch("tui.app.local_env.containers_running", return_value=False),
            patch("tui.app.local_env.start_containers", new=AsyncMock(return_value=0)),
            patch("tui.app.local_env.wait_backend_ready", new=AsyncMock(return_value=True)),
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)


class TestOnboarding:
    """Drive the step-by-step onboarding wizard via the pilot.

    Assertions target the behavioural contract (which keys reach ``.env``, that
    the chosen provider's key is written and the other is not, masking, and
    per-step validation), not a fixed set of co-mounted widgets.
    """

    @staticmethod
    def _gate_patches(tmp_path, run_init):
        return (
            patch("tui.app.local_env.is_setup_complete", return_value=False),
            patch("tui.app.local_env.repo_root", return_value=tmp_path),
            patch("tui.app.local_env.run_init", new=run_init),
            patch("tui.app.local_env.containers_running", return_value=True),
            patch("tui.app.local_env.backend_ready", new=AsyncMock(return_value=True)),
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
        )

    @staticmethod
    async def _set(screen, key, value):
        screen.query_one(f"#onb-{key}", Input).value = value

    @pytest.mark.asyncio
    async def test_writes_chosen_provider_key_and_runs_init(self, tmp_path):
        run_init = AsyncMock(return_value=0)
        a, b, c, d, e, f = self._gate_patches(tmp_path, run_init)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                assert isinstance(screen, tui_app.OnboardingScreen)
                assert screen.current_step_id == "welcome"

                await pilot.press("ctrl+n")  # -> provider
                await pilot.pause()
                assert screen.current_step_id == "provider"
                await self._set(screen, "OPENROUTER_API_KEY", "or-key")

                await pilot.press("ctrl+n")  # -> github
                await pilot.pause()
                assert screen.current_step_id == "github"
                await self._set(screen, "GITHUB_TOKEN", "tok")

                await pilot.press("ctrl+n")  # -> compass
                await pilot.pause()
                assert screen.current_step_id == "compass"
                await self._set(screen, "P10Y_API_KEY", "p10y")

                await pilot.press("ctrl+n")  # -> review
                await pilot.pause()
                assert screen.current_step_id == "review"

                await pilot.press("ctrl+s")  # save & initialize
                await pilot.pause()

        run_init.assert_awaited_once()
        env = (tmp_path / ".env").read_text()
        assert "GITHUB_TOKEN=tok" in env
        assert "P10Y_API_KEY=p10y" in env
        assert "OPENROUTER_API_KEY=or-key" in env
        assert "ANTHROPIC_API_KEY=" not in env

    @pytest.mark.asyncio
    async def test_anthropic_path_writes_only_anthropic(self, tmp_path):
        run_init = AsyncMock(return_value=0)
        a, b, c, d, e, f = self._gate_patches(tmp_path, run_init)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                await pilot.press("ctrl+n")  # -> provider
                await pilot.pause()
                # Select the Anthropic radio (second option) and fill its key.
                buttons = list(screen.query("#onboard-provider RadioButton").results())
                buttons[1].value = True  # toggles selection, fires RadioSet.Changed
                await pilot.pause()
                await self._set(screen, "ANTHROPIC_API_KEY", "ant-key")
                await pilot.press("ctrl+n")  # -> github
                await pilot.pause()
                await self._set(screen, "GITHUB_TOKEN", "tok")
                await pilot.press("ctrl+n")  # -> compass
                await pilot.pause()
                await self._set(screen, "P10Y_API_KEY", "p10y")
                await pilot.press("ctrl+n")  # -> review
                await pilot.pause()
                await pilot.press("ctrl+s")
                await pilot.pause()
        run_init.assert_awaited_once()
        env = (tmp_path / ".env").read_text()
        assert "ANTHROPIC_API_KEY=ant-key" in env
        assert "OPENROUTER_API_KEY=" not in env

    @pytest.mark.asyncio
    async def test_validation_blocks_advance_without_required(self, tmp_path):
        run_init = AsyncMock(return_value=0)
        a, b, c, d, e, f = self._gate_patches(tmp_path, run_init)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                await pilot.press("ctrl+n")  # -> provider
                await pilot.pause()
                assert screen.current_step_id == "provider"
                # No key entered: Next must not advance past the provider step.
                await pilot.press("ctrl+n")
                await pilot.pause()
                assert screen.current_step_id == "provider"
        run_init.assert_not_awaited()
        assert not (tmp_path / ".env").exists()

    @pytest.mark.asyncio
    async def test_back_preserves_entered_value(self, tmp_path):
        # A non-masked field is re-shown with its value on revisit (masked fields
        # mount blank by design and keep the value in state, not the widget).
        run_init = AsyncMock(return_value=0)
        a, b, c, d, e, f = self._gate_patches(tmp_path, run_init)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                await pilot.press("ctrl+n")  # -> provider
                await pilot.pause()
                await self._set(screen, "OPENROUTER_API_KEY", "or-key")
                await pilot.press("ctrl+n")  # -> github
                await pilot.pause()
                await self._set(screen, "GITHUB_ORG", "my-org")
                await pilot.press("ctrl+b")  # back -> provider
                await pilot.pause()
                await pilot.press("ctrl+n")  # forward -> github
                await pilot.pause()
                assert screen.query_one("#onb-GITHUB_ORG", Input).value == "my-org"

    @pytest.mark.asyncio
    async def test_secret_field_is_masked(self, tmp_path):
        run_init = AsyncMock(return_value=0)
        a, b, c, d, e, f = self._gate_patches(tmp_path, run_init)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                # Advance to the GitHub step which carries a masked + a plain field.
                await pilot.press("ctrl+n")  # provider
                await pilot.pause()
                await self._set(screen, "OPENROUTER_API_KEY", "or-key")
                await pilot.press("ctrl+n")  # github
                await pilot.pause()
                assert screen.query_one("#onb-GITHUB_TOKEN", Input).password is True
                assert screen.query_one("#onb-GIT_USER_NAME", Input).password is False


class TestQuitBinding:
    @pytest.mark.asyncio
    async def test_q_exits_dashboard(self):
        a, b, c = _gate_ready()
        with (
            a,
            b,
            c,
            patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())),
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.is_running
                await pilot.press("q")
                await pilot.pause()
                assert not app.is_running

    @pytest.mark.asyncio
    async def test_q_exits_sessions_screen(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("q")
                await pilot.pause()
                assert not app.is_running


class TestAppNotifications:
    @pytest.mark.asyncio
    async def test_notifies_for_active_sessions_independent_of_screen(self):
        sessions = [
            {
                "generation_id": "gen_a",
                "status": "running",
                "checkpoint": "kb_init_done",
            },
            {
                "generation_id": "gen_b",
                "status": "running",
                "checkpoint": "generation_started",
            },
        ]
        app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)

        with (
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=sessions)),
            patch("tui.app.fire_milestones") as fire,
        ):
            await app.notify_active_sessions()

        assert fire.call_count == 2
        assert {"gen_a", "gen_b"} <= app._watched_generation_ids

    @pytest.mark.asyncio
    async def test_notifies_for_known_dashboard_run_not_in_sessions(self):
        app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
        app.watch_generation_id("gen_x")

        with (
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
            patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())) as poll,
            patch("tui.app.fire_milestones") as fire,
        ):
            await app.notify_active_sessions()

        poll.assert_awaited_once_with("gen_x")
        fire.assert_called_once()


class TestWorkspaceDrillIn:
    @pytest.mark.asyncio
    async def test_open_workspace_pushes_messages_screen(self):
        a, b, c = _gate_ready()
        with (
            a,
            b,
            c,
            patch("tui.app.poll_once", new=AsyncMock(return_value=_ws_usage_payload())),
            patch("tui.app.workspace_message_events", new=_events_iter([])),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()  # first refresh populates workspace ids
                await pilot.press("o")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.WorkspaceMessagesScreen)
                assert app.screen._workspace_id == "ws-01-1"

    @pytest.mark.asyncio
    async def test_arrow_keys_select_workspace(self):
        # Two workspaces: ↓ moves selection to the second, ↑ wraps back to the
        # first. The arrows must win over the scroll container (priority binding),
        # so selection drives which workspace the open action targets.
        payload = {
            "generation_id": "gen_x",
            "status": "running",
            "checkpoint": "generation_started",
            "workspace_count": 2,
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 1, "total_phases": 9, "phase_name": "A"},
                "ws-01-2": {"last_completed_phase": 2, "total_phases": 9, "phase_name": "B"},
            },
        }
        a, b, c = _gate_ready()
        with (
            a,
            b,
            c,
            patch("tui.app.poll_once", new=AsyncMock(return_value=payload)),
            patch("tui.app.workspace_message_events", new=_events_iter([])),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()  # first refresh populates workspace ids
                await pilot.press("down")
                await pilot.pause()
                await pilot.press("o")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.WorkspaceMessagesScreen)
                assert app.screen._workspace_id == "ws-01-2"
                await pilot.press("escape")
                await pilot.pause()
                # ↑ from the second wraps back to the first.
                await pilot.press("up")
                await pilot.pause()
                await pilot.press("o")
                await pilot.pause()
                assert app.screen._workspace_id == "ws-01-1"

    @pytest.mark.asyncio
    async def test_messages_screen_consumes_stream_and_back_returns(self):
        events = [
            _Event(message="planning the work"),
            _Event(kind="tool_use", tool_name="Bash", message="ls"),
        ]
        a, b, c = _gate_ready()
        with (
            a,
            b,
            c,
            patch("tui.app.poll_once", new=AsyncMock(return_value=_ws_usage_payload())),
            patch("tui.app.workspace_message_events", new=_events_iter(events)),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(tui_app.WorkspaceMessagesScreen("gen_x", "ws-01-1"))
                await pilot.pause()
                assert isinstance(app.screen, tui_app.WorkspaceMessagesScreen)
                # Worker consumed the (finite) stream without crashing the app.
                assert app.is_running
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.DashboardScreen)

    @pytest.mark.asyncio
    async def test_open_with_no_workspaces_notifies(self):
        # No workspace_phases → nothing to open; action must not crash.
        a, b, c = _gate_ready()
        with (
            a,
            b,
            c,
            patch("tui.app.poll_once", new=AsyncMock(return_value={"status": "pending"})),
            patch("tui.app.workspace_message_events", new=_events_iter([])),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("o")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.DashboardScreen)


class TestCliGuardedImport:
    @pytest.mark.asyncio
    async def test_missing_textual_prints_install_hint(self, capsys):
        import cli

        # Simulate the [tui] extra being absent: every `textual` module is
        # unimportable, so re-importing tui.app raises ImportError(name="textual...").
        with patch.dict(sys.modules):
            sys.modules.pop("tui.app", None)
            for name in list(sys.modules):
                if name == "textual" or name.startswith("textual."):
                    sys.modules[name] = None
            rc = await cli.cmd_tui(SimpleNamespace())
        assert rc == 1
        err = capsys.readouterr().err
        # The hint must point at re-installing the uv tool, not a project-venv
        # pip install that never touches the tool env this command runs from.
        assert "uv tool install" in err
        assert "textual" in err
        assert "pip install" not in err

    @pytest.mark.asyncio
    async def test_unrelated_import_error_propagates(self):
        import cli

        # A failure that is NOT the optional `textual` dep (here: the `tui`
        # package itself is unimportable) is a real bug — it must surface rather
        # than be masked by the "TUI isn't installed" hint.
        with patch.dict(sys.modules):
            sys.modules.pop("tui.app", None)
            sys.modules["tui"] = None
            with pytest.raises(ImportError):
                await cli.cmd_tui(SimpleNamespace())

    @pytest.mark.asyncio
    async def test_present_tui_delegates_to_run_tui(self):
        import cli

        with patch("tui.app.run_tui", new=AsyncMock(return_value=0)) as m:
            rc = await cli.cmd_tui(SimpleNamespace(root_path=None, generation_id=None, interval=3))
        assert rc == 0
        m.assert_awaited_once()


class TestDashboardCheckAction:
    def test_retry_greyed_out_unless_failed(self):
        # check_action → None greys the "retry" key in the footer (shown but the
        # key won't fire); True enables it. Greyed-out (not hidden) is the
        # intended UX so the action stays discoverable.
        screen = tui_app.DashboardScreen("gen_x")
        screen._payload = {"status": "running"}
        assert screen.check_action("retry", ()) is None
        screen._payload = {"status": "failed"}
        assert screen.check_action("retry", ()) is True

    def test_other_actions_always_enabled(self):
        screen = tui_app.DashboardScreen("gen_x")
        screen._payload = {"status": "running"}
        assert screen.check_action("clear", ()) is True


class TestConfirmScreenCountdown:
    @pytest.mark.asyncio
    async def test_confirm_disabled_until_countdown_elapses(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = tui_app.ConfirmScreen("Delete?", countdown=2)
                app.push_screen(screen)
                await pilot.pause()
                confirm = screen.query_one("#confirm")
                assert confirm.disabled is True
                assert "2s" in str(confirm.label)
                screen._tick()
                assert confirm.disabled is True
                assert "1s" in str(confirm.label)
                screen._tick()
                assert confirm.disabled is False
                assert str(confirm.label) == "Confirm"

    @pytest.mark.asyncio
    async def test_cancel_stops_countdown_timer(self):
        # Cancelling mid-countdown must stop the interval so no stray _tick
        # fires against the dismissed screen.
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = tui_app.ConfirmScreen("Clear?", countdown=10)
                app.push_screen(screen)
                await pilot.pause()
                assert screen._timer is not None
                screen.action_cancel()
                await pilot.pause()
                assert screen._timer is None


class TestDashboardActionFlows:
    """Retry/clear worker flows: confirmation gating and clear eligibility.

    ``_run_suspended`` (which suspends the app and blocks on ``input()``) is
    patched out so we exercise only the decision logic; ``actions.do_*`` are
    patched to assert which CLI handler the flow ultimately reaches.
    """

    @pytest.mark.asyncio
    async def test_retry_runs_action_when_confirmed(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                with (
                    patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)),
                    patch.object(screen, "_run_suspended", new=AsyncMock()) as run_susp,
                    # do_retry is async; force a sync mock so the flow's
                    # ``do_retry(root)`` yields a sentinel, not a live coroutine.
                    patch(
                        "tui.app.actions.do_retry", new=MagicMock(return_value="retry-coro")
                    ) as do_retry,
                ):
                    await screen._retry_flow()
                do_retry.assert_called_once_with(app.root)
                run_susp.assert_awaited_once_with("retry-coro")

    @pytest.mark.asyncio
    async def test_retry_skips_action_when_cancelled(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                with (
                    patch.object(app, "push_screen_wait", new=AsyncMock(return_value=False)),
                    patch.object(screen, "_run_suspended", new=AsyncMock()) as run_susp,
                    patch("tui.app.actions.do_retry") as do_retry,
                ):
                    await screen._retry_flow()
                do_retry.assert_not_called()
                run_susp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clear_runs_for_eligible_set_when_confirmed(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                screen._payload = _running_payload()  # workspace ws-01-1 → set 1
                with (
                    patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)) as psw,
                    patch.object(screen, "_run_suspended", new=AsyncMock()) as run_susp,
                    patch(
                        "tui.app.actions.do_clear_set", new=MagicMock(return_value="clear-coro")
                    ) as do_clear,
                    patch(
                        "tui.app.fetch_pool_status",
                        new=AsyncMock(return_value={"cleaning_sets": [{"set_number": 1}]}),
                    ),
                ):
                    await screen._clear_flow()
                do_clear.assert_called_once_with(1)
                run_susp.assert_awaited_once_with("clear-coro")
                assert isinstance(psw.await_args.args[0], tui_app.ConfirmScreen)

    @pytest.mark.asyncio
    async def test_clear_cancelled_does_not_run(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                screen._payload = _running_payload()
                with (
                    patch.object(app, "push_screen_wait", new=AsyncMock(return_value=False)),
                    patch.object(screen, "_run_suspended", new=AsyncMock()) as run_susp,
                    patch("tui.app.actions.do_clear_set") as do_clear,
                    patch(
                        "tui.app.fetch_pool_status",
                        new=AsyncMock(return_value={"cleaning_sets": [{"set_number": 1}]}),
                    ),
                ):
                    await screen._clear_flow()
                do_clear.assert_not_called()
                run_susp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clear_ineligible_set_shows_message_and_skips(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                screen._payload = _running_payload()  # set 1, not in cleaning_sets
                with (
                    patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)) as psw,
                    patch.object(screen, "_run_suspended", new=AsyncMock()) as run_susp,
                    patch("tui.app.actions.do_clear_set") as do_clear,
                    patch(
                        "tui.app.fetch_pool_status",
                        new=AsyncMock(return_value={"cleaning_sets": []}),
                    ),
                ):
                    await screen._clear_flow()
                do_clear.assert_not_called()
                run_susp.assert_not_awaited()
                assert isinstance(psw.await_args.args[0], tui_app.MessageScreen)

    @pytest.mark.asyncio
    async def test_clear_pool_fetch_error_shows_message_and_skips(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                screen._payload = _running_payload()
                with (
                    patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)) as psw,
                    patch.object(screen, "_run_suspended", new=AsyncMock()) as run_susp,
                    patch("tui.app.actions.do_clear_set") as do_clear,
                    patch(
                        "tui.app.fetch_pool_status",
                        new=AsyncMock(side_effect=RuntimeError("backend down")),
                    ),
                ):
                    await screen._clear_flow()
                do_clear.assert_not_called()
                run_susp.assert_not_awaited()
                assert isinstance(psw.await_args.args[0], tui_app.MessageScreen)

    @pytest.mark.asyncio
    async def test_clear_unavailable_when_pool_status_missing(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                with (
                    patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)) as psw,
                    patch.object(screen, "_run_suspended", new=AsyncMock()) as run_susp,
                    patch("tui.app.actions.do_clear_set") as do_clear,
                    patch("tui.app.fetch_pool_status", None),
                ):
                    await screen._clear_flow()
                do_clear.assert_not_called()
                run_susp.assert_not_awaited()
                assert isinstance(psw.await_args.args[0], tui_app.MessageScreen)
