"""Smoke tests for the Textual app rendering and the CLI guarded import.

The full interactive app needs a TTY, so we exercise the pure renderable
builders (which use Rich) and the non-TTY plain-status path. Textual is
imported by tui.app; tests skip cleanly if the optional extra is absent.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from textual.widgets import Input

import httpx
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

    def test_completed_renders_component_breakdown(self):
        payload = {
            "generation_id": "gen_x",
            "status": "completed",
            "checkpoint": "estimation_done",
            "progress": {"workspace_phases": {}},
            "result": {
                "summary": {"average_hours": 318},
                "workspace_estimations": [],
                "comparative_analysis": {
                    "component_comparison": {
                        "auth": {
                            "component_name": "auth",
                            "average": 40.0,
                            "variance_percentage": 12.0,
                        }
                    }
                },
            },
        }
        out = self._render(tui_app.build_dashboard(payload, Path("/tmp/acme"), "gen_x"))
        assert "auth" in out
        assert "40" in out
        assert "12%" in out

    def test_completed_renders_report_hint(self):
        # The report file lives inside the backend container, not on the host
        # filesystem the TUI runs on — so the hint is shown whenever a P10Y
        # result exists, and fetched over HTTP on demand (see action_open_report).
        payload = {
            "generation_id": "gen_x",
            "status": "completed",
            "checkpoint": "estimation_done",
            "progress": {"workspace_phases": {}},
            "result": {"summary": {"average_hours": 318}, "workspace_estimations": []},
        }
        out = self._render(tui_app.build_dashboard(payload, Path("/tmp/acme"), "gen_x"))
        assert "HTML report" in out
        assert "press h to open" in out.lower()

    def test_running_omits_report_hint(self):
        out = self._render(
            tui_app.build_dashboard(_running_payload(), Path("/tmp/acme"), "gen_8f3abc21")
        )
        assert "HTML report" not in out


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


class TestSessionLabel:
    def test_uses_pipeline_step_defs_for_checkpoint_label(self):
        label = tui_app._session_label(
            {
                "generation_id": "est-d67dcac6cbe5",
                "status": "running",
                "checkpoint": "contract_validated",
                "created_at": "2026-07-07T14:00:15.701099+00:00",
            }
        )

        assert "Contract validated" in label
        assert "est-d67dcac6cbe5" in label

    def test_unknown_checkpoint_falls_back_to_key(self):
        label = tui_app._session_label(
            {
                "generation_id": "est-d67dcac6cbe5",
                "status": "running",
                "checkpoint": "custom_checkpoint",
            }
        )

        assert "custom_checkpoint" in label


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
    async def test_ready_no_gen_lands_on_sessions_when_client_connected(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "tui.app.mcp_clients.is_any_client_connected", return_value=True
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)

    @pytest.mark.asyncio
    async def test_no_gen_no_client_shows_client_setup_first(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "tui.app.mcp_clients.is_any_client_connected", return_value=False
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.ClientSetupScreen)

    @pytest.mark.asyncio
    async def test_client_setup_skip_proceeds_to_sessions(self):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "tui.app.mcp_clients.is_any_client_connected", return_value=False
        ), patch("tui.mcp_clients.client_rows", return_value=[]):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.ClientSetupScreen)
                await pilot.press("s")  # skip
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
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()),
            patch("tui.app.mcp_clients.is_any_client_connected", return_value=True),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)

    @pytest.mark.asyncio
    async def test_containers_up_but_backend_not_ready_says_unhealthy_not_down(self):
        # Containers running + backend not ready → the gate must not claim the
        # containers aren't running; it shows the "backend isn't healthy" prompt.
        with (
            patch("tui.app.local_env.is_setup_complete", return_value=True),
            patch("tui.app.local_env.containers_running", return_value=True),
            patch("tui.app.local_env.backend_ready", new=AsyncMock(return_value=False)),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.StartContainersScreen)
                prompt = str(app.screen.query_one("#docker-prompt").render())
                assert "aren't running" not in prompt
                assert "isn't healthy" in prompt

    @pytest.mark.asyncio
    async def test_backend_not_ready_retry_skips_compose_up(self):
        # In the up-but-not-ready path, retrying (y) re-polls readiness without
        # re-running `docker compose up` (the containers are already up).
        start = AsyncMock(return_value=0)
        with (
            patch("tui.app.local_env.is_setup_complete", return_value=True),
            patch("tui.app.local_env.containers_running", return_value=True),
            patch("tui.app.local_env.backend_ready", new=AsyncMock(return_value=False)),
            patch("tui.app.local_env.start_containers", new=start),
            patch("tui.app.local_env.wait_backend_ready", new=AsyncMock(return_value=True)),
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
            patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()),
            patch("tui.app.mcp_clients.is_any_client_connected", return_value=True),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)
                start.assert_not_awaited()


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

                await pilot.press("ctrl+n")  # -> advanced (optional)
                await pilot.pause()
                assert screen.current_step_id == "advanced"

                await pilot.press("ctrl+n")  # skip advanced -> review
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
    async def test_langfuse_advanced_step_writes_all_three(self, tmp_path):
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
                await self._set(screen, "GITHUB_TOKEN", "tok")
                await pilot.press("ctrl+n")  # -> compass
                await pilot.pause()
                await self._set(screen, "P10Y_API_KEY", "p10y")
                await pilot.press("ctrl+n")  # -> advanced
                await pilot.pause()
                assert screen.current_step_id == "advanced"
                await self._set(screen, "LANGFUSE_PUBLIC_KEY", "pk-1")
                await self._set(screen, "LANGFUSE_SECRET_KEY", "sk-1")
                await self._set(screen, "LANGFUSE_BASE_URL", "https://lf.example")
                await pilot.press("ctrl+n")  # -> review
                await pilot.pause()
                assert screen.current_step_id == "review"
                await pilot.press("ctrl+s")
                await pilot.pause()
        run_init.assert_awaited_once()
        env = (tmp_path / ".env").read_text()
        assert "LANGFUSE_PUBLIC_KEY=pk-1" in env
        assert "LANGFUSE_SECRET_KEY=sk-1" in env
        assert "LANGFUSE_BASE_URL=https://lf.example" in env

    @pytest.mark.asyncio
    async def test_partial_langfuse_blocks_advance(self, tmp_path):
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
                await self._set(screen, "GITHUB_TOKEN", "tok")
                await pilot.press("ctrl+n")  # -> compass
                await pilot.pause()
                await self._set(screen, "P10Y_API_KEY", "p10y")
                await pilot.press("ctrl+n")  # -> advanced
                await pilot.pause()
                # Only the public key: partial LangFuse must block advancing.
                await self._set(screen, "LANGFUSE_PUBLIC_KEY", "pk-only")
                await pilot.press("ctrl+n")
                await pilot.pause()
                assert screen.current_step_id == "advanced"
        run_init.assert_not_awaited()

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
                await pilot.press("ctrl+n")  # -> advanced (optional)
                await pilot.pause()
                await pilot.press("ctrl+n")  # skip advanced -> review
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

    @pytest.mark.asyncio
    async def test_complete_env_skips_wizard_runs_init(self, tmp_path):
        # A pre-existing, complete .env means there is nothing to collect: the
        # gate runs init directly (RunInitScreen) with no wizard step, so init is
        # awaited without a single ctrl+n/ctrl+s press.
        run_init = AsyncMock(return_value=0)
        complete = {"OPENROUTER_API_KEY": "or", "GITHUB_TOKEN": "gh", "P10Y_API_KEY": "p1"}
        with (
            patch("tui.app.local_env.is_setup_complete", return_value=False),
            patch("tui.app.local_env.repo_root", return_value=tmp_path),
            patch("tui.app.load_env_secrets", return_value=complete),
            patch("tui.app.local_env.run_init", new=run_init),
            patch("tui.app.local_env.containers_running", return_value=True),
            patch("tui.app.local_env.backend_ready", new=AsyncMock(return_value=True)),
            patch("tui.app.mcp_clients.is_any_client_connected", return_value=True),
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
        ):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)
        run_init.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_incomplete_env_still_shows_wizard(self, tmp_path):
        run_init = AsyncMock(return_value=0)
        incomplete = {"GITHUB_TOKEN": "gh"}  # no LLM key, no P10Y key
        with (
            patch("tui.app.local_env.is_setup_complete", return_value=False),
            patch("tui.app.local_env.repo_root", return_value=tmp_path),
            patch("tui.app.load_env_secrets", return_value=incomplete),
            patch("tui.app.local_env.run_init", new=run_init),
            patch("tui.app.local_env.containers_running", return_value=True),
            patch("tui.app.local_env.backend_ready", new=AsyncMock(return_value=True)),
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
        ):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.OnboardingScreen)
        run_init.assert_not_awaited()


class TestSettingsScreen:
    """Settings is reachable from the Sessions landing screen and edits .env,
    including the optional Advanced LangFuse section."""

    @staticmethod
    def _land_on_sessions(tmp_path):
        a, b, c = _gate_ready()
        return (
            a,
            b,
            c,
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
            patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()),
            patch("tui.app.mcp_clients.is_any_client_connected", return_value=True),
        )

    @pytest.mark.asyncio
    async def test_s_from_sessions_opens_settings_with_langfuse(self, tmp_path):
        a, b, c, d, e, f = self._land_on_sessions(tmp_path)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)
                await pilot.press("s")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SettingsScreen)
                # The Advanced LangFuse rows are present.
                for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
                    assert app.screen.query_one(f"#secret-{key}", Input) is not None
                # Secret key masked, public key + host plain.
                assert app.screen.query_one("#secret-LANGFUSE_SECRET_KEY", Input).password is True
                assert app.screen.query_one("#secret-LANGFUSE_PUBLIC_KEY", Input).password is False
                # .env-backed sections warn that a restart is needed to take effect.
                from textual.widgets import Static
                section_text = " ".join(
                    str(s.render()) for s in app.screen.query(".settings-section").results(Static)
                )
                assert "requires backend restart" in section_text

    @pytest.mark.asyncio
    async def test_saving_langfuse_writes_env(self, tmp_path):
        a, b, c, d, e, f = self._land_on_sessions(tmp_path)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                screen = app.screen
                screen.query_one("#secret-LANGFUSE_PUBLIC_KEY", Input).value = "pk-x"
                screen.query_one("#secret-LANGFUSE_SECRET_KEY", Input).value = "sk-x"
                screen.query_one("#secret-LANGFUSE_BASE_URL", Input).value = "https://lf.x"
                await pilot.press("ctrl+s")
                await pilot.pause()
                # Save pops back to sessions.
                assert isinstance(app.screen, tui_app.SessionsScreen)
        env = (tmp_path / ".env").read_text()
        assert "LANGFUSE_PUBLIC_KEY=pk-x" in env
        assert "LANGFUSE_SECRET_KEY=sk-x" in env
        assert "LANGFUSE_BASE_URL=https://lf.x" in env

    @pytest.mark.asyncio
    async def test_partial_langfuse_save_is_blocked(self, tmp_path):
        a, b, c, d, e, f = self._land_on_sessions(tmp_path)
        with a, b, c, d, e, f:
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                screen = app.screen
                # Only public key set → save must refuse and stay on Settings.
                screen.query_one("#secret-LANGFUSE_PUBLIC_KEY", Input).value = "pk-only"
                await pilot.press("ctrl+s")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SettingsScreen)
        # Nothing written.
        assert not (tmp_path / ".env").exists() or "LANGFUSE_PUBLIC_KEY=pk-only" not in (
            tmp_path / ".env"
        ).read_text()


class TestQuitBinding:
    @pytest.mark.asyncio
    async def test_q_exits_dashboard(self):
        a, b, c = _gate_ready()
        with (
            a,
            b,
            c,
            patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())),
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()),
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
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()):
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
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()),
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
    async def test_workspace_screen_excludes_app_level_session_watcher(self):
        # The workspace drill-in polls /status for its stats panel, so the
        # app-wide session watcher must not also poll the same generation via
        # the list endpoint. Mixing the two payload shapes can replay desktop
        # "phase progressed" notifications.
        a, b, c = _gate_ready()
        fetch = AsyncMock(return_value=[])
        with (
            a,
            b,
            c,
            patch("tui.app.fetch_sessions", new=fetch),
            patch("tui.app.poll_once", new=AsyncMock(return_value=_ws_usage_payload())),
            patch("tui.app.workspace_message_events", new=_events_iter([])),
        ):
            app = tui_app.SpecFlowTUI(root=Path("/tmp/x"), generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(tui_app.WorkspaceMessagesScreen("gen_x", "ws-01-1"))
                await pilot.pause()
                fetch.reset_mock()

                await app.notify_active_sessions()

                fetch.assert_awaited_once_with(exclude={"gen_x"})

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

        # Simulate an incomplete install: `textual` (a base dependency) is
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

    def test_open_report_greyed_out_without_result(self):
        screen = tui_app.DashboardScreen("gen_x")
        screen._payload = {"status": "running"}
        assert screen.check_action("open_report", ()) is None

    def test_open_report_enabled_when_result_present(self):
        screen = tui_app.DashboardScreen("gen_x")
        screen._payload = {"status": "completed", "result": {"summary": {}}}
        assert screen.check_action("open_report", ()) is True


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


class TestDashboardOpenReport:
    """``h`` fetches the HTML report over HTTP (the backend runs in a container,
    so there's no shared filesystem path to open directly) and caches it locally
    before opening; a missing opener or missing report must never crash."""

    @pytest.mark.asyncio
    async def test_opens_report_via_platform_opener(self, tmp_path):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                with (
                    patch(
                        "tui.app.call_backend_endpoint_bytes",
                        new=AsyncMock(return_value=b"<html>report</html>"),
                    ),
                    patch("tui.app._platform_opener", return_value="open"),
                    patch("tui.app.shutil.which", return_value="/usr/bin/open"),
                    patch("tui.app.local_env.run_command", new=AsyncMock()) as run_cmd,
                ):
                    await screen._open_report()
                cache_path = tmp_path / ".specflow-local" / "reports" / "gen_x.html"
                assert cache_path.read_bytes() == b"<html>report</html>"
                run_cmd.assert_awaited_once_with(
                    ["open", str(cache_path)], cache_path.parent, timeout=10
                )

    @pytest.mark.asyncio
    async def test_falls_back_to_notify_when_no_opener_available(self, tmp_path):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                with (
                    patch(
                        "tui.app.call_backend_endpoint_bytes",
                        new=AsyncMock(return_value=b"<html></html>"),
                    ),
                    patch("tui.app._platform_opener", return_value=None),
                    patch("tui.app.local_env.run_command", new=AsyncMock()) as run_cmd,
                    patch.object(screen, "notify") as notify,
                ):
                    await screen._open_report()
                run_cmd.assert_not_awaited()
                notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_404_shows_no_report_available_message(self, tmp_path):
        request = httpx.Request("GET", "http://backend/api/v1/generation-sessions/gen_x/report.html")
        response = httpx.Response(404, request=request)
        not_found = httpx.HTTPStatusError("Not Found", request=request, response=response)
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                with (
                    patch(
                        "tui.app.call_backend_endpoint_bytes",
                        new=AsyncMock(side_effect=not_found),
                    ),
                    patch("tui.app.local_env.run_command", new=AsyncMock()) as run_cmd,
                    patch.object(screen, "notify") as notify,
                ):
                    await screen._open_report()
                run_cmd.assert_not_awaited()
                notify.assert_called_once()
                assert "no html report" in notify.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_open_report_no_op_when_no_result_yet(self, tmp_path):
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.poll_once", new=AsyncMock(return_value=_running_payload())):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id="gen_x", poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                screen._payload = {"status": "running"}
                with (
                    patch(
                        "tui.app.call_backend_endpoint_bytes", new=AsyncMock()
                    ) as call_backend,
                    patch.object(screen, "notify") as notify,
                ):
                    screen.action_open_report()
                call_backend.assert_not_awaited()
                notify.assert_called_once()


# ---------------------------------------------------------------------------
# ClientSetupScreen — registration of the MCP server with AI clients
# ---------------------------------------------------------------------------


from textual.widgets import ListView  # noqa: E402

from services import local_env  # noqa: E402
from tui import mcp_clients as mc  # noqa: E402

_BLOCK = mc.ServerBlock(
    command="uvx",
    args=("--from", "/abs/mcp_server", "specflow-mcp"),
    env={"USER_EMAIL": "u@x.com", "WORKSPACE_COUNT": "3"},
)


def _make_app(root):
    a, b, c = _gate_ready()
    return tui_app.SpecFlowTUI(root=root, generation_id=None, poll_interval=999), (a, b, c)


async def _push_client_screen(app):
    screen = tui_app.ClientSetupScreen()
    await app.push_screen(screen)
    return screen


class TestClientSetupScreen:
    @pytest.mark.asyncio
    async def test_lists_registry_and_preselects_first_installed(self, tmp_path):
        rows = [
            mc.ClientRow(mc.CLAUDE_CODE, installed=True, saved=None),
            mc.ClientRow(mc.GEMINI_CLI, installed=False, saved=None),
            mc.ClientRow(mc.CURSOR, installed=True, saved=None),
            mc.ClientRow(mc.MANUAL, installed=True, saved=None),
        ]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "tui.mcp_clients.client_rows", return_value=rows
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                lv = screen.query_one("#client-list", ListView)
                assert len(lv) == 4
                # First installed, non-manual client (Claude) pre-selected.
                assert lv.index == 0

    def test_row_label_is_colored_rich_text(self):
        from rich.text import Text

        screen = tui_app.ClientSetupScreen()
        row = mc.ClientRow(mc.CLAUDE_CODE, installed=True, saved=None)
        label = screen._row_label(row, mc.ClientStatus.VERIFIED)
        assert isinstance(label, Text)
        assert "verified" in label.plain
        assert "●" in label.plain  # the dot is present
        # BOTH the dot and the label carry the status (green) colour — the dot
        # must map to status, not just to "installed".
        green_spans = [s for s in label.spans if "green" in str(s.style)]
        assert len(green_spans) >= 2

    def test_manual_row_is_always_grey(self):
        screen = tui_app.ClientSetupScreen()
        row = mc.ClientRow(mc.MANUAL, installed=True, saved=None)
        label = screen._row_label(row, mc.ClientStatus.CONNECTED)
        # Even if somehow "connected", the copy row never goes green (unverifiable).
        assert not any("green" in str(s.style) for s in label.spans)

    @pytest.mark.asyncio
    async def test_show_config_pushes_message_screen(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "tui.app.mcp_clients.load_server_block", return_value=_BLOCK
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                screen._show_config(mc.MANUAL)
                await pilot.pause()
                assert isinstance(app.screen, tui_app.MessageScreen)

    @pytest.mark.asyncio
    async def test_claude_cli_reaches_verified(self, tmp_path):
        results = [
            local_env.CommandResult(0, "", False),  # remove (ignored)
            local_env.CommandResult(0, "Added", False),  # add-json
            local_env.CommandResult(0, "specflow: uvx ✔", False),  # verify get
        ]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "tui.app.local_env.run_command", new=AsyncMock(side_effect=results)
        ) as run_cmd:
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                log = screen.query_one("#client-log", tui_app.RichLog)
                status = await screen._connect_cli(mc.CLAUDE_CODE, _BLOCK, log)
        assert status is mc.ClientStatus.VERIFIED
        # remove → add → verify, in order; add used add-json.
        assert run_cmd.await_count == 3
        add_argv = run_cmd.await_args_list[1].args[0]
        assert add_argv[:3] == ["claude", "mcp", "add-json"]

    @pytest.mark.asyncio
    async def test_claude_add_failure_is_failed(self, tmp_path):
        results = [
            local_env.CommandResult(0, "", False),  # remove
            local_env.CommandResult(1, "boom", False),  # add fails
        ]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "tui.app.local_env.run_command", new=AsyncMock(side_effect=results)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                log = screen.query_one("#client-log", tui_app.RichLog)
                status = await screen._connect_cli(mc.CLAUDE_CODE, _BLOCK, log)
        assert status is mc.ClientStatus.FAILED

    @pytest.mark.asyncio
    async def test_cursor_file_merge_added_unverified(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ), patch("tui.app.local_env.run_command", new=AsyncMock(
            return_value=local_env.CommandResult(0, "", False)
        )):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                log = screen.query_one("#client-log", tui_app.RichLog)
                status = await screen._connect_cursor(mc.CURSOR, _BLOCK, log)
        assert status is mc.ClientStatus.ADDED_UNVERIFIED
        written = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
        assert written["mcpServers"]["specflow"]["command"] == "uvx"

    @pytest.mark.asyncio
    async def test_cursor_malformed_config_refused_untouched(self, tmp_path):
        cfg = tmp_path / ".cursor" / "mcp.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{not valid json")
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                log = screen.query_one("#client-log", tui_app.RichLog)
                # User declines the backup/overwrite confirmation.
                with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=False)):
                    status = await screen._connect_cursor(mc.CURSOR, _BLOCK, log)
        assert status is mc.ClientStatus.FAILED
        # Existing (malformed) file left exactly as it was — never clobbered.
        assert cfg.read_text() == "{not valid json"

    @pytest.mark.asyncio
    async def test_cursor_malformed_approved_backs_up_then_writes(self, tmp_path):
        cfg = tmp_path / ".cursor" / "mcp.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{bad json")
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ), patch("tui.app.local_env.run_command", new=AsyncMock(
            return_value=local_env.CommandResult(0, "", False)
        )):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                log = screen.query_one("#client-log", tui_app.RichLog)
                with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)):
                    status = await screen._connect_cursor(mc.CURSOR, _BLOCK, log)
        assert status is mc.ClientStatus.ADDED_UNVERIFIED
        assert json.loads(cfg.read_text())["mcpServers"]["specflow"]["command"] == "uvx"
        # The old (malformed) content was preserved in a backup, not lost.
        assert (tmp_path / ".cursor" / "mcp.json.bak").read_text() == "{bad json"

    @pytest.mark.asyncio
    async def test_cursor_backup_never_clobbers_existing_bak(self, tmp_path):
        cur = tmp_path / ".cursor"
        cur.mkdir(parents=True)
        (cur / "mcp.json").write_text("{bad json")
        (cur / "mcp.json.bak").write_text("ORIGINAL BACKUP")
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ), patch("tui.app.local_env.run_command", new=AsyncMock(
            return_value=local_env.CommandResult(0, "", False)
        )):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                log = screen.query_one("#client-log", tui_app.RichLog)
                with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)):
                    await screen._connect_cursor(mc.CURSOR, _BLOCK, log)
        assert (cur / "mcp.json.bak").read_text() == "ORIGINAL BACKUP"  # preserved
        assert (cur / "mcp.json.bak.1").read_text() == "{bad json"  # new backup rotated

    @pytest.mark.asyncio
    async def test_cursor_write_failure_is_failed_not_crash(self, tmp_path):
        # Make ~/.cursor a regular FILE so the mkdir/write raises OSError.
        (tmp_path / ".cursor").write_text("i am a file, not a directory")
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                log = screen.query_one("#client-log", tui_app.RichLog)
                status = await screen._connect_cursor(mc.CURSOR, _BLOCK, log)
        assert status is mc.ClientStatus.FAILED  # handled, worker did not crash

    @pytest.mark.asyncio
    async def test_connect_persists_actual_status_not_assumed_connected(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ), patch("tui.app.mcp_clients.load_server_block", return_value=_BLOCK), patch(
            "tui.app.local_env.run_command",
            new=AsyncMock(return_value=local_env.CommandResult(0, "", False)),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                # The success panel would block on push_screen_wait — stub it.
                with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)):
                    await screen._connect_flow(mc.CURSOR)
        # Cursor is unverifiable → saved as ADDED_UNVERIFIED, NOT "connected".
        assert mc.saved_statuses(home=tmp_path).get("cursor") is mc.ClientStatus.ADDED_UNVERIFIED

    @pytest.mark.asyncio
    async def test_inspect_confirm_marks_connected(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=True)):
                    await screen._inspect_flow(mc.CURSOR)
        assert mc.saved_statuses(home=tmp_path)["cursor"] is mc.ClientStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_inspect_reject_marks_failed(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()), patch(
            "pathlib.Path.home", return_value=tmp_path
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=False)):
                    await screen._inspect_flow(mc.CURSOR)
        assert mc.saved_statuses(home=tmp_path)["cursor"] is mc.ClientStatus.FAILED

    @pytest.mark.asyncio
    async def test_inspect_decide_later_leaves_status_untouched(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            mc.save_status("cursor", mc.ClientStatus.ADDED_UNVERIFIED, home=tmp_path)
            app, (a, b, c) = _make_app(tmp_path)
            with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    screen = await _push_client_screen(app)
                    await pilot.pause()
                    with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)):
                        await screen._inspect_flow(mc.CURSOR)
        # Dismissed without deciding → unchanged.
        assert mc.saved_statuses(home=tmp_path)["cursor"] is mc.ClientStatus.ADDED_UNVERIFIED

    @pytest.mark.asyncio
    async def test_verifiable_client_shows_verifying_while_probe_runs(self, tmp_path):
        # Before the background probe resolves, a verifiable client reads
        # "verifying…" — never an idle "press ↵ to connect" the user might click.
        rows = [mc.ClientRow(mc.CLAUDE_CODE, installed=True, saved=None)]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch(
            "tui.mcp_clients.client_rows", return_value=rows
        ), patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                status = screen._status["claude_code"]
        assert status is mc.ClientStatus.VERIFYING

    @pytest.mark.asyncio
    async def test_probe_marks_already_registered_claude_verified(self, tmp_path):
        # A user who already added specflow to Claude sees it connected — no re-add.
        rows = [mc.ClientRow(mc.CLAUDE_CODE, installed=True, saved=None)]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch(
            "tui.mcp_clients.client_rows", return_value=rows
        ), patch("pathlib.Path.home", return_value=tmp_path), patch(
            "tui.app.local_env.run_command",
            new=AsyncMock(return_value=local_env.CommandResult(0, "specflow: uvx ✔", False)),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                await pilot.pause()  # let the probe worker run
                status = screen._status["claude_code"]
        assert status is mc.ClientStatus.VERIFIED
        assert mc.saved_statuses(home=tmp_path)["claude_code"] is mc.ClientStatus.VERIFIED

    @pytest.mark.asyncio
    async def test_probe_clears_stale_claude_when_not_registered(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            mc.save_status("claude_code", mc.ClientStatus.VERIFIED, home=tmp_path)
        rows = [mc.ClientRow(mc.CLAUDE_CODE, installed=True, saved=mc.ClientStatus.VERIFIED)]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch(
            "tui.mcp_clients.client_rows", return_value=rows
        ), patch("pathlib.Path.home", return_value=tmp_path), patch(
            "tui.app.local_env.run_command",
            new=AsyncMock(return_value=local_env.CommandResult(1, "No such server", False)),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                await pilot.pause()
                status = screen._status["claude_code"]
        assert status is mc.ClientStatus.NOT_CONFIGURED
        assert "claude_code" not in mc.saved_statuses(home=tmp_path)

    @pytest.mark.asyncio
    async def test_verify_choice_screen_list_selects_with_keyboard(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch(
            "tui.app.mcp_clients.is_any_client_connected", return_value=True
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                # Enter on the highlighted first option ("Yes") → True.
                yes_result: list = []
                app.push_screen(tui_app.VerifyChoiceScreen("Cursor"), yes_result.append)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                # Arrow down to "No" then Enter → False.
                no_result: list = []
                app.push_screen(tui_app.VerifyChoiceScreen("Cursor"), no_result.append)
                await pilot.pause()
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
        assert yes_result == [True]
        assert no_result == [False]


class TestConfigDriftReconnect:
    """Config-drift: connect records a fingerprint; the setup screen flags a
    connected client whose baked config no longer matches; Settings routes there."""

    def test_row_label_orange_when_connected_but_config_drifted(self):
        from rich.text import Text

        screen = tui_app.ClientSetupScreen()
        screen._current_fp = "new"
        screen._saved_fp = {"cursor": "old"}
        row = mc.ClientRow(mc.CURSOR, installed=True, saved=mc.ClientStatus.ADDED_UNVERIFIED)
        label = screen._row_label(row, mc.ClientStatus.ADDED_UNVERIFIED)
        assert isinstance(label, Text)
        assert mc.STALE_LABEL in label.plain
        assert any("orange" in str(s.style) for s in label.spans)

    def test_row_label_not_orange_when_fingerprint_matches(self):
        screen = tui_app.ClientSetupScreen()
        screen._current_fp = "same"
        screen._saved_fp = {"cursor": "same"}
        row = mc.ClientRow(mc.CURSOR, installed=True, saved=mc.ClientStatus.ADDED_UNVERIFIED)
        label = screen._row_label(row, mc.ClientStatus.ADDED_UNVERIFIED)
        assert mc.STALE_LABEL not in label.plain

    @staticmethod
    def _render(renderable) -> str:
        console = Console(width=100, record=True)
        console.print(renderable)
        return console.export_text()

    def test_tiers_panel_shows_models_defaults_purposes_and_caveat(self):
        screen = tui_app.ClientSetupScreen()
        block = mc.ServerBlock("uvx", ("x",), {"LLM_HIGH": "anthropic/opus", "WORKSPACE_COUNT": "3"})
        text = self._render(screen._tiers_panel(block))
        assert "Model tiers" in text  # panel title
        assert "anthropic/opus" in text  # configured high-tier model
        assert "default" in text  # an unset tier reads as the backend default
        assert "planning" in text  # high-tier purpose
        assert "code generation" in text  # medium-tier purpose
        assert "press m to change" in text
        assert "next run" in text

    def test_tiers_panel_all_default_when_block_missing(self):
        text = self._render(tui_app.ClientSetupScreen()._tiers_panel(None))
        assert text.count("default") >= 3  # every tier falls back to default

    @pytest.mark.asyncio
    async def test_m_opens_tier_settings(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(
            tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await _push_client_screen(app)
                await pilot.pause()
                await pilot.press("m")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SettingsScreen)

    @pytest.mark.asyncio
    async def test_connect_flow_records_config_fingerprint(self, tmp_path):
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(
            tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()
        ), patch("pathlib.Path.home", return_value=tmp_path), patch(
            "tui.app.mcp_clients.load_server_block", return_value=_BLOCK
        ), patch(
            "tui.app.local_env.run_command",
            new=AsyncMock(return_value=local_env.CommandResult(0, "", False)),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                with patch.object(app, "push_screen_wait", new=AsyncMock(return_value=None)):
                    await screen._connect_flow(mc.CURSOR)
        # The block just baked in is recorded as cursor's drift baseline.
        assert mc.saved_fingerprints(home=tmp_path).get("cursor") == mc.config_fingerprint(_BLOCK)

    @pytest.mark.asyncio
    async def test_probe_present_does_not_touch_fingerprint(self, tmp_path):
        # A present read-back confirms only presence, not env — the recorded
        # fingerprint must be left as-is so drift can still surface.
        with patch("pathlib.Path.home", return_value=tmp_path):
            mc.save_status("claude_code", mc.ClientStatus.VERIFIED, home=tmp_path)
            mc.save_fingerprint("claude_code", "old-fp", home=tmp_path)
        rows = [mc.ClientRow(mc.CLAUDE_CODE, installed=True, saved=mc.ClientStatus.VERIFIED)]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch(
            "tui.mcp_clients.client_rows", return_value=rows
        ), patch("pathlib.Path.home", return_value=tmp_path), patch(
            "tui.app.local_env.run_command",
            new=AsyncMock(return_value=local_env.CommandResult(0, "specflow: uvx ✔", False)),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await _push_client_screen(app)
                await pilot.pause()
                await pilot.pause()  # let the probe worker run
        assert mc.saved_fingerprints(home=tmp_path).get("claude_code") == "old-fp"

    @pytest.mark.asyncio
    async def test_probe_absent_forgets_fingerprint(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            mc.save_status("claude_code", mc.ClientStatus.VERIFIED, home=tmp_path)
            mc.save_fingerprint("claude_code", "old-fp", home=tmp_path)
        rows = [mc.ClientRow(mc.CLAUDE_CODE, installed=True, saved=mc.ClientStatus.VERIFIED)]
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch(
            "tui.mcp_clients.client_rows", return_value=rows
        ), patch("pathlib.Path.home", return_value=tmp_path), patch(
            "tui.app.local_env.run_command",
            new=AsyncMock(return_value=local_env.CommandResult(1, "No such server", False)),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await _push_client_screen(app)
                await pilot.pause()
                await pilot.pause()
        assert "claude_code" not in mc.saved_fingerprints(home=tmp_path)

    @staticmethod
    def _write_block(tmp_path, workspace_count):
        from tui import config as tui_config

        path = tui_config.config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "specflow": {
                            "command": "uvx",
                            "args": ["--from", "x", "specflow-mcp"],
                            "env": {"WORKSPACE_COUNT": workspace_count},
                        }
                    }
                }
            )
        )

    @staticmethod
    def _land_on_sessions():
        a, b, c = _gate_ready()
        return (
            a,
            b,
            c,
            patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])),
            patch.object(tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()),
            patch("tui.app.mcp_clients.is_any_client_connected", return_value=True),
        )

    @pytest.mark.asyncio
    async def test_settings_save_routes_to_connect_screen_on_drift(self, tmp_path):
        # A connected client baked in the current block; editing a runtime field
        # makes it stale, so save warns and routes to the connect screen.
        self._write_block(tmp_path, "3")
        with patch("pathlib.Path.home", return_value=tmp_path):
            baseline = mc.config_fingerprint(mc.load_server_block(tmp_path))
            mc.save_status("cursor", mc.ClientStatus.VERIFIED, home=tmp_path)
            mc.save_fingerprint("cursor", baseline, home=tmp_path)
        a, b, c, d, e, f = self._land_on_sessions()
        with a, b, c, d, e, f, patch("pathlib.Path.home", return_value=tmp_path):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                app.screen.query_one("#field-WORKSPACE_COUNT", Input).value = "5"
                await pilot.press("ctrl+s")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.ClientSetupScreen)

    @pytest.mark.asyncio
    async def test_settings_save_returns_to_sessions_when_no_connected_client(self, tmp_path):
        # Same runtime edit, but nothing connected → no drift, plain pop to sessions.
        self._write_block(tmp_path, "3")
        a, b, c, d, e, f = self._land_on_sessions()
        with a, b, c, d, e, f, patch("pathlib.Path.home", return_value=tmp_path):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                app.screen.query_one("#field-WORKSPACE_COUNT", Input).value = "5"
                await pilot.press("ctrl+s")
                await pilot.pause()
                assert isinstance(app.screen, tui_app.SessionsScreen)


class TestModelValidationUI:
    """Model-tier validity feedback: pure glyph mapping, the validated tiers box,
    and the live markers on the connect screen and in Settings."""

    @staticmethod
    def _render(renderable) -> str:
        console = Console(width=100, record=True)
        console.print(renderable)
        return console.export_text()

    @staticmethod
    def _response(tiers):
        return {
            "provider": "openrouter",
            "catalog_available": True,
            "all_valid": all(m["status"] == "valid" for t in tiers for m in t["models"]),
            "has_blocking_tier": any(m["status"] == "invalid" for t in tiers for m in t["models"]),
            "tiers": tiers,
        }

    def test_model_mark_maps_statuses(self):
        assert tui_app._model_mark("valid") == ("✓", "green")
        assert tui_app._model_mark("invalid") == ("✗", "red")
        assert tui_app._model_mark("unverified")[1] == "dim"
        assert tui_app._model_mark(None) == tui_app._NEUTRAL_MARK

    def test_tier_mark_aggregates_worst_first(self):
        assert tui_app._tier_mark([{"status": "valid"}, {"status": "invalid"}]) == ("✗", "red")
        assert tui_app._tier_mark([{"status": "valid"}, {"status": "valid"}]) == ("✓", "green")
        assert tui_app._tier_mark([{"status": "valid"}, {"status": "unverified"}]) == (
            tui_app._NEUTRAL_MARK
        )
        assert tui_app._tier_mark([]) == tui_app._NEUTRAL_MARK

    def test_tier_marker_text_words(self):
        assert tui_app._tier_marker_text([]).plain == ""
        assert "unsupported" in tui_app._tier_marker_text([{"status": "invalid"}]).plain
        assert "available" in tui_app._tier_marker_text([{"status": "valid"}]).plain
        # Unverified-only: a glyph but no word we can't stand behind.
        assert tui_app._tier_marker_text([{"status": "unverified"}]).plain == "•"

    def test_tiers_panel_with_validation_shows_marks_and_notes(self):
        block = mc.ServerBlock("uvx", ("x",), {"LLM_HIGH": "anthropic/bad"})
        resp = self._response(
            [
                {
                    "tier": "LLM_HIGH",
                    "models": [
                        {"configured": "anthropic/bad", "status": "invalid",
                         "suggestion": "anthropic/good"}
                    ],
                }
            ]
        )
        text = self._render(tui_app.ClientSetupScreen()._tiers_panel(block, resp))
        assert "✗" in text
        assert "not available on openrouter" in text
        assert "did you mean" in text
        assert "anthropic/good" in text

    @pytest.mark.asyncio
    async def test_connect_screen_marks_invalid_model(self, tmp_path):
        block = mc.ServerBlock("uvx", ("x",), {"LLM_HIGH": "anthropic/bad"})
        resp = self._response(
            [{"tier": "LLM_HIGH", "models": [{"configured": "anthropic/bad", "status": "invalid",
                                              "suggestion": "anthropic/good"}]}]
        )
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(
            tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()
        ), patch("tui.app.mcp_clients.load_server_block", return_value=block), patch(
            "tui.app.validate_models.request_model_validation_for",
            new=AsyncMock(return_value=resp),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                await pilot.pause()  # let the validation worker run + re-render
        # The worker fetched and stored the result (the box re-render runs on the
        # same path); the box built from it shows the ✗ and the suggestion.
        assert screen._validation == resp
        text = self._render(screen._tiers_panel(block, screen._validation))
        assert "✗" in text
        assert "anthropic/good" in text  # suggestion surfaced

    @pytest.mark.asyncio
    async def test_connect_screen_neutral_when_validation_fails(self, tmp_path):
        # Backend unreachable → worker swallows the error, box stays free of ✓/✗.
        block = mc.ServerBlock("uvx", ("x",), {"LLM_HIGH": "anthropic/x"})
        app, (a, b, c) = _make_app(tmp_path)
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(
            tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()
        ), patch("tui.app.mcp_clients.load_server_block", return_value=block), patch(
            "tui.app.validate_models.request_model_validation_for",
            new=AsyncMock(side_effect=RuntimeError("no backend")),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await _push_client_screen(app)
                await pilot.pause()
                await pilot.pause()
        # Worker swallowed the failure: no validation stored, box stays neutral.
        assert screen._validation is None
        text = self._render(screen._tiers_panel(block, screen._validation))
        assert "✗" not in text and "✓" not in text

    @pytest.mark.asyncio
    async def test_settings_tier_markers_reflect_validation(self, tmp_path):
        resp = self._response(
            [
                {"tier": "LLM_HIGH", "models": [{"configured": "x/bad", "status": "invalid"}]},
                {"tier": "LLM_MEDIUM", "models": [{"configured": "x/ok", "status": "valid"}]},
                {"tier": "LLM_LOW", "models": [{"configured": "x/ok2", "status": "valid"}]},
            ]
        )
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(
            tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()
        ), patch("tui.app.mcp_clients.is_any_client_connected", return_value=True), patch(
            "tui.app.validate_models.request_model_validation_for", new=AsyncMock(return_value=resp)
        ):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                await pilot.pause()  # on_mount validation worker runs
                screen = app.screen
                high = self._render(screen.query_one("#tierstatus-LLM_HIGH", tui_app.Static).render())
                medium = self._render(
                    screen.query_one("#tierstatus-LLM_MEDIUM", tui_app.Static).render()
                )
        assert "✗" in high and "unsupported" in high
        assert "✓" in medium and "available" in medium

    @pytest.mark.asyncio
    async def test_settings_tier_marker_stays_on_screen(self, tmp_path):
        # Regression: the marker must render within the viewport, not be pushed
        # off the right edge by a non-flexing input (it was, before the CSS fix).
        resp = self._response(
            [{"tier": t, "models": [{"configured": "x", "status": "invalid"}]}
             for t in ("LLM_HIGH", "LLM_MEDIUM", "LLM_LOW")]
        )
        width = 100
        a, b, c = _gate_ready()
        with a, b, c, patch("tui.app.fetch_sessions", new=AsyncMock(return_value=[])), patch.object(
            tui_app.ClientSetupScreen, "_probe_verifiable", new=AsyncMock()
        ), patch("tui.app.mcp_clients.is_any_client_connected", return_value=True), patch(
            "tui.app.validate_models.request_model_validation_for", new=AsyncMock(return_value=resp)
        ):
            app = tui_app.SpecFlowTUI(root=tmp_path, generation_id=None, poll_interval=999)
            async with app.run_test(size=(width, 40)) as pilot:
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                await pilot.pause()
                marker = app.screen.query_one("#tierstatus-LLM_HIGH", tui_app.Static)
                region = marker.region
        assert region.width > 0
        assert region.right <= width  # fully within the viewport
