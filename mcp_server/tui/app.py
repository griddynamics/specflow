"""Textual application — the local mission-control screen for a run.

This is the only module that imports Textual; ``cli.cmd_tui`` imports it lazily
so the base install carries no TUI dependency. All data comes from the pure
formatters in ``render.py`` and the shared service layer via ``poller``/
``actions``; this module is rendering + key handling only.

On launch the app runs a startup gate (``SpecFlowTUI._startup_gate``): first-run
onboarding if unconfigured, then a Docker-start prompt if the stack is down,
before reaching the dashboard.

Screens:
  * OnboardingScreen      — first-run: a step-by-step wizard that walks the user
    through each credential (what it is, how to get it, where to paste it),
    writes ``.env``, and runs init. Step content lives in ``tui.onboarding``.
  * StartContainersScreen — start the Docker stack (or quit) when it's down.
  * DashboardScreen — live pipeline, per-workspace bars, tokens/cost, activity
    tail, and in-app actions (retry / clear / settings).
  * SessionsScreen  — picker across active generations.
  * SettingsScreen  — edits runtime settings (mcp-config.json) and secrets (.env).

Non-interactive terminals (CI/SSH/pipes) never construct the app: ``run_tui``
detects a non-TTY and prints a plain-text status instead, so the TUI is never
the only way to see a run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
)

from cli import resolve_backend_config
from services import local_env, validate_models
from services.llm_tiers import LLM_TIER_KEYS
from services.retry import retry_generation_core
from services.session import resolve_generation_id, set_project_root
from services.specflow_backend import call_backend_endpoint, call_backend_endpoint_bytes
from tui import actions, activity, mcp_clients, onboarding, render
from tui.config import (
    EDITABLE_KEYS,
    ENV_SECRET_KEYS,
    LANGFUSE_KEYS,
    MASKED_KEYS,
    langfuse_partial_error,
    load_env,
    load_env_secrets,
    save_env,
    save_env_secrets,
)
from tui.constants import CHECKPOINT_STEPS, DEFAULT_POLL_INTERVAL, STATUS_PILLS, TERMINAL_STATUSES
from tui.poller import MilestoneTracker, fire_milestones, poll_once
from tui.render import format_tokens
from tui.stream import workspace_message_events

try:
    from services.cli_service import fetch_pool_status, fetch_sessions
except Exception:  # pragma: no cover - service import is always present in practice
    fetch_pool_status = fetch_sessions = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Rich renderable builders (consume render.py pure structures)
# ---------------------------------------------------------------------------


def _header_panel(payload: dict[str, Any], generation_id: str, project: str) -> Panel:
    text, style = render.status_pill(payload.get("status"))
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
    grid.add_row(
        Text(f"Project  {project}", style="bold"),
        Text(text, style=style),
    )
    line2 = Text()
    line2.append(f"Generation  {generation_id[:18]}…   ")
    tokens = render.tokens_summary(payload)
    if tokens:
        line2.append(tokens, style="cyan")
    grid.add_row(line2, Text(payload.get("current_phase") or "", style="dim"))
    return Panel(grid, title="SpecFlow", border_style=style)


def _pipeline_panel(payload: dict[str, Any]) -> Panel:
    body = Text()
    for step in render.pipeline_steps(payload):
        style = {
            "done": "green",
            "active": "bold yellow",
            "pending": "dim",
            # Not applicable to this run — greyed out and struck through.
            "skipped": "dim strike",
        }.get(step.state.value, "")
        body.append(f"  {step.symbol} ", style=style)
        body.append(step.label, style=style)
        if step.state.value == "skipped":
            # Note stays dim but un-struck so the reason reads cleanly.
            body.append("  (not needed for this run)", style="dim")
        body.append("\n")
    return Panel(body, title="Pipeline", border_style="blue")


def _workspace_badge(bar: render.WorkspaceBar, flagged: set[str]) -> Text:
    """Bare state word for the workspace list; details live in the drill-in."""
    badge = render.agent_state_badge(bar.agent_state)
    if badge is not None:
        text, style = badge
        return Text(text, style=style)
    if bar.workspace_id in flagged:
        return Text("⚠", style="yellow")
    return Text("")


def _workspaces_panel(payload: dict[str, Any], selected_ws_id: str | None = None) -> Panel:
    bars = render.workspace_bars(payload)
    if not bars:
        return Panel(
            Text("Workspace progress not reported yet.", style="dim"),
            title="Workspaces",
            border_style="blue",
        )
    flagged = render.workspaces_with_events(payload)
    table = Table.grid(padding=(0, 1))
    table.add_column()  # selection marker
    table.add_column()  # ws id
    table.add_column()  # phase n/n
    table.add_column()  # phase name
    table.add_column()  # bar
    table.add_column(justify="right")  # pct
    table.add_column()  # agent-state badge / warning marker
    for bar in bars:
        selected = bar.workspace_id == selected_ws_id
        marker = "▶" if selected else " "
        id_style = "bold reverse" if selected else "bold"
        table.add_row(
            Text(marker, style="yellow"),
            Text(bar.workspace_id, style=id_style),
            Text(bar.phase_label, style="cyan"),
            Text(bar.phase_name[:32], style="dim"),
            Text(render.progress_bar(bar.fraction), style="green"),
            Text(f"{bar.percent}%"),
            _workspace_badge(bar, flagged),
        )
    count = payload.get("workspace_count")
    title = f"Workspaces · {count} variants" if count else "Workspaces"
    subtitle = "↑/↓ select · o or ↵ open live stream"
    return Panel(table, title=title, subtitle=subtitle, border_style="blue")


def _estimate_panel(payload: dict[str, Any]) -> Panel | None:
    panel = render.estimate_panel(payload)
    if panel is None:
        return None
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left")

    def fmt_h(v: float | None) -> str:
        return f"{v:.0f} h" if v is not None else "—"

    grid.add_row("Average", fmt_h(panel.average_hours))
    cv = panel.coefficient_of_variation
    variance = panel.variance_assessment or "—"
    grid.add_row("Variance", f"{variance}" + (f"  (CV {cv:.2f})" if cv is not None else ""))
    if panel.min_hours is not None and panel.max_hours is not None:
        grid.add_row("Range", f"{panel.min_hours:.0f}–{panel.max_hours:.0f} h")
    if panel.total_buffer_pct is not None or panel.final_estimate is not None:
        buf = f"+{panel.total_buffer_pct:.0f}%" if panel.total_buffer_pct is not None else ""
        final = f"  → {panel.final_estimate:.0f} h" if panel.final_estimate is not None else ""
        grid.add_row("Buffer", f"{buf}{final}".strip())
    if panel.per_workspace:
        variants = " · ".join(f"{n} {h:.0f}h" for n, h in panel.per_workspace)
        grid.add_row("Variants", variants)
    if panel.total_usd_cost is not None:
        grid.add_row("Total spend", f"${panel.total_usd_cost:.2f}")

    renderables: list[RenderableType] = [grid]
    if panel.component_comparison:
        breakdown = Table(box=None, padding=(0, 2))
        breakdown.add_column("Component", justify="left")
        breakdown.add_column("Avg hours", justify="right")
        breakdown.add_column("Variance", justify="right")
        for row in panel.component_comparison:
            breakdown.add_row(
                row.component_name,
                f"{row.average_hours:.0f} h",
                f"{row.variance_percentage:.0f}%",
            )
        renderables.append(breakdown)
    renderables.append(Text("HTML report available — press h to open", style="dim"))

    return Panel(Group(*renderables), title="P10Y estimate", border_style="green")


def _activity_panel(root: Path, payload: dict[str, Any]) -> Panel | None:
    bars = render.workspace_bars(payload)
    ws_id = bars[0].workspace_id if bars else None
    lines = activity.recent_activity(root, ws_id)
    if not lines:
        return None
    body = Text("\n".join(lines), style="dim")
    title = f"Recent activity · {ws_id}" if ws_id else "Recent activity"
    return Panel(body, title=title, border_style="grey50")


# The dashboard shows only the newest few events; the full list lives on the
# Events screen (`e`).
_DASHBOARD_EVENT_ROWS = 5


def _event_row_text(row: render.AgentErrorEventRow) -> Text:
    line = Text()
    if row.time:
        line.append(f"{row.time} ", style="dim")
    line.append(row.workspace_id, style="bold")
    if row.phase_label:
        line.append(f" {row.phase_label}", style="cyan")
    line.append(" — ")
    line.append(row.message, style="yellow")
    return line


def _agent_events_panel(payload: dict[str, Any]) -> Panel | None:
    """Yellow warnings panel mirroring the Error panel; shown whenever events exist."""
    rows = render.agent_error_event_rows(payload)
    if not rows:
        return None
    recent = rows[-_DASHBOARD_EVENT_ROWS:]
    body = Text("\n").join(_event_row_text(row) for row in recent)
    subtitle = (
        f"showing {len(recent)} of {len(rows)} · press e for all"
        if len(rows) > len(recent)
        else "press e for all"
    )
    return Panel(body, title="Agent warnings", subtitle=subtitle, border_style="yellow")


def build_dashboard(
    payload: dict[str, Any] | None,
    root: Path,
    generation_id: str,
    selected_ws_id: str | None = None,
) -> RenderableType:
    """Assemble the full dashboard renderable from a status payload."""
    if not payload:
        return Panel(
            Text("Waiting for status… (backend unreachable or no data yet)", style="yellow"),
            title="SpecFlow",
        )
    project = root.name
    panels: list[RenderableType] = [
        _header_panel(payload, generation_id, project),
        _pipeline_panel(payload),
        _workspaces_panel(payload, selected_ws_id),
    ]
    estimate = _estimate_panel(payload)
    if estimate is not None:
        panels.append(estimate)
    act = _activity_panel(root, payload)
    if act is not None:
        panels.append(act)
    warnings_panel = _agent_events_panel(payload)
    if warnings_panel is not None:
        panels.append(warnings_panel)
    if payload.get("error"):
        panels.append(
            Panel(Text(str(payload["error"]), style="red"), title="Error", border_style="red")
        )
    return Group(*panels)


def stream_row_text(row: render.StreamRow) -> Text:
    """Assemble one live-feed row as a Rich Text line.

    Mechanical adapter only: the row shape and per-kind styles are owned by
    ``render`` (the presentation SSOT); this just maps them onto a Rich ``Text``.
    """
    line = Text()
    if row.time:
        line.append(f"{row.time} ", style="dim")
    line.append(f"{row.kind:<14}", style=render.kind_style(row.kind))
    if row.label:
        line.append(f" [{row.label}]", style="magenta")
    if row.message:
        line.append(f"  {row.message}")
    return line


def build_workspace_warnings(payload: dict[str, Any] | None, workspace_id: str) -> Panel | None:
    """Yellow per-workspace warnings block for the drill-in screen.

    Lists THIS workspace's agent error/warning events with model switches
    pinned first (a silently swapped model is the one thing the user must not
    miss). None when the workspace has no events — the widget stays hidden.
    """
    rows = render.workspace_warning_rows(payload or {}, workspace_id)
    if not rows:
        return None
    body = Text("\n").join(_event_row_text(row) for row in rows)
    return Panel(body, title=f"Warnings · {workspace_id}", border_style="yellow")


def build_workspace_stats(payload: dict[str, Any] | None, workspace_id: str) -> Panel:
    """Build the per-workspace stats panel for the drill-in screen."""
    stats = render.workspace_stats(payload or {}, workspace_id)
    title = f"Stats · {workspace_id}"
    if stats is None:
        return Panel(
            Text("Workspace stats not reported yet.", style="dim"),
            title=title,
            border_style="blue",
        )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left")
    if stats.models:
        grid.add_row("Model(s)", ", ".join(stats.models))
    phase = f"{stats.phase_label}".strip()
    if stats.phase_name:
        phase += f"  {stats.phase_name}"
    grid.add_row("Phase", phase)
    grid.add_row("Progress", f"{render.progress_bar(stats.fraction)}  {stats.percent}%")
    if stats.has_usage:
        grid.add_row("Turns", str(stats.num_turns) if stats.num_turns is not None else "—")
        grid.add_row(
            "Tokens in/out",
            f"{format_tokens(stats.input_tokens)} / {format_tokens(stats.output_tokens)}",
        )
        grid.add_row(
            "Cache write/read",
            f"{format_tokens(stats.cache_write_tokens)} / {format_tokens(stats.cache_read_tokens)}",
        )
        grid.add_row("Total tokens", format_tokens(stats.total_tokens))
    else:
        # Usage is recorded only when an agent step finishes (the SDK reports
        # cumulative tokens in its terminal message), so a step still in flight
        # (notably the single long KB-init query) has nothing to show yet.
        grid.add_row("Turns / tokens", Text("appear when this step completes", style="dim italic"))
    return Panel(grid, title=title, border_style="blue")


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class _SpecFlowScreen(Screen):
    """Base screen with a reliable quit action.

    Binding ``q``/``ctrl+c`` to a screen-level ``action_quit`` that calls
    ``app.exit()`` guarantees the keypress resolves and stops ``run_async`` —
    relying on the default app-level resolution proved unreliable behind a
    focused scroll container. Subclasses add their own bindings; Textual merges
    BINDINGS across the class hierarchy.
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", "quit", show=False),
    ]

    def action_quit(self) -> None:
        self.app.exit()


class MessageScreen(ModalScreen[None]):
    """Informational popup — dismiss with Esc or Enter."""

    BINDINGS = [
        Binding("escape", "dismiss", "ESC to close"),
        Binding("enter", "dismiss", "close", show=False),
    ]

    def __init__(self, title: str, body: str, *, markup: bool = True) -> None:
        super().__init__()
        self._title = title
        self._body = body
        # Disable markup when the body contains literal brackets (e.g. JSON
        # arrays) that Rich would otherwise try to parse as style tags.
        self._markup = markup

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-panel"):
            yield Static(self._title, classes="modal-title")
            yield Static(self._body, classes="modal-body", markup=self._markup)
        yield Footer()  # same bottom-bar style as the main screens

    def action_dismiss(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Confirm / cancel dialog with an optional countdown before Confirm is enabled."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "confirm", "Confirm", show=False),
    ]

    def __init__(self, message: str, countdown: int = 0) -> None:
        super().__init__()
        self._message = message
        self._remaining = max(0, countdown)
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-panel"):
            yield Static(self._message, classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button(self._confirm_label(), id="confirm", variant="primary")

    def _confirm_label(self) -> str:
        if self._remaining > 0:
            return f"Confirm ({self._remaining}s)"
        return "Confirm"

    def on_mount(self) -> None:
        confirm = self.query_one("#confirm", Button)
        confirm.disabled = self._remaining > 0
        if self._remaining > 0:
            self._timer = self.set_interval(1, self._tick)

    def _tick(self) -> None:
        self._remaining -= 1
        confirm = self.query_one("#confirm", Button)
        if self._remaining > 0:
            confirm.label = self._confirm_label()
        else:
            confirm.label = "Confirm"
            confirm.disabled = False
            self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self._stop_timer()
            self.dismiss(False)
        elif event.button.id == "confirm" and not event.button.disabled:
            self._stop_timer()
            self.dismiss(True)

    def action_cancel(self) -> None:
        self._stop_timer()
        self.dismiss(False)

    def action_confirm(self) -> None:
        confirm = self.query_one("#confirm", Button)
        if not confirm.disabled:
            self._stop_timer()
            self.dismiss(True)


class VerifyChoiceScreen(ModalScreen[bool | None]):
    """Manual-inspection prompt for a client we cannot read back (Cursor/Gemini).

    Returns ``True`` (the user confirmed it works), ``False`` (it doesn't), or
    ``None`` (dismissed without deciding — the status is left unchanged).
    """

    BINDINGS = [Binding("escape", "decide_later", "ESC to close")]

    def __init__(self, client_name: str) -> None:
        super().__init__()
        self._client_name = client_name

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-panel"):
            yield Static("Did it work?", classes="modal-title")
            yield Static(
                f"Open {self._client_name} and check whether SpecFlow's tools "
                "show up. We can't detect this automatically — pick what you see.",
                classes="modal-body",
            )
            # Arrow-navigable list, matching the other screens (↑/↓ + ↵).
            yield ListView(
                ListItem(Label("Yes — it's connected"), id="opt-yes"),
                ListItem(Label("No — not working"), id="opt-no"),
                id="verify-choices",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#verify-choices", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.id == "opt-yes")

    def action_decide_later(self) -> None:
        self.dismiss(None)


class DashboardScreen(_SpecFlowScreen):
    """Live dashboard for a single generation."""

    BINDINGS = [
        Binding("r", "retry", "retry"),
        Binding("x", "cancel", "cancel"),
        Binding("w", "clear", "clear ws"),
        Binding("s", "settings", "settings"),
        Binding("b", "sessions", "sessions"),
        Binding("c", "connect_client", "Add MCP to AI tool"),
        Binding("o", "open_workspace", "open ws"),
        Binding("enter", "open_workspace", "open ws", show=False),
        Binding("e", "open_events", "events"),
        Binding("h", "open_report", "open report"),
        # Priority so the workspace selection wins over the scroll container's
        # own up/down handling (otherwise arrows just scroll the dashboard).
        # PageUp/PageDown/Home/End and the mouse wheel still scroll the body.
        Binding("up", "prev_workspace", "prev ws", show=False, priority=True),
        Binding("down", "next_workspace", "next ws", show=False, priority=True),
        Binding("[", "prev_workspace", "prev ws", show=False),
        Binding("]", "next_workspace", "next ws", show=False),
    ]

    def __init__(self, generation_id: str) -> None:
        super().__init__()
        self._generation_id = generation_id
        self._poll_timer: Timer | None = None
        self._workspace_ids: list[str] = []
        self._selected_index = 0
        self._payload: dict[str, Any] | None = None

    @property
    def generation_id(self) -> str:
        return self._generation_id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(id="dashboard-body")
        yield Footer()

    def on_mount(self) -> None:
        # call_later so the first (async) refresh is awaited by the event loop;
        # set_interval drives subsequent polls. The dashboard owns the poll for
        # its own run; the app tick excludes this run to avoid a duplicate poll.
        if isinstance(self.app, SpecFlowTUI):
            self.app.watch_generation_id(self._generation_id)
        self.call_later(self.refresh_status)
        self._poll_timer = self.set_interval(self.app.poll_interval, self.refresh_status)

    @property
    def _selected_ws_id(self) -> str | None:
        if not self._workspace_ids:
            return None
        idx = max(0, min(self._selected_index, len(self._workspace_ids) - 1))
        return self._workspace_ids[idx]

    async def refresh_status(self) -> None:
        payload = await poll_once(self._generation_id)
        self._payload = payload
        # Feed notifications through the shared per-run tracker on the app so a
        # milestone fires regardless of which screen is visible.
        if isinstance(self.app, SpecFlowTUI):
            self.app.process_payload(self._generation_id, payload)
        # Track selectable workspace ids so [ / ] / open act on live data.
        self._workspace_ids = [b.workspace_id for b in render.workspace_bars(payload or {})]
        if self._workspace_ids:
            self._selected_index = max(0, min(self._selected_index, len(self._workspace_ids) - 1))
        try:
            body = self.query_one("#dashboard-body", Static)
        except NoMatches:
            return
        body.update(
            build_dashboard(payload, self.app.root, self._generation_id, self._selected_ws_id)
        )
        self.refresh_bindings()
        # A finished run will not change again — stop polling it. Conversely, a
        # run that left a terminal state (e.g. failed → retry → pending) must
        # resume polling, so re-arm the timer if it was previously stopped.
        if payload and (payload.get("status") or "").lower() in TERMINAL_STATUSES:
            if self._poll_timer is not None:
                self._poll_timer.stop()
                self._poll_timer = None
        elif self._poll_timer is None:
            self._poll_timer = self.set_interval(self.app.poll_interval, self.refresh_status)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "retry":
            status = ((self._payload or {}).get("status") or "").lower()
            return True if status == "failed" else None
        if action == "cancel":
            status = ((self._payload or {}).get("status") or "").lower()
            return True if status in {"pending", "initializing", "running"} else None
        if action == "open_report":
            return True if render.estimate_panel(self._payload) is not None else None
        return True

    def _move_selection(self, delta: int) -> None:
        if not self._workspace_ids:
            return
        self._selected_index = (self._selected_index + delta) % len(self._workspace_ids)
        self.call_later(self.refresh_status)

    def action_prev_workspace(self) -> None:
        self._move_selection(-1)

    def action_next_workspace(self) -> None:
        self._move_selection(1)

    def action_open_workspace(self) -> None:
        ws_id = self._selected_ws_id
        if ws_id is None:
            self.notify("No workspace to open yet.", severity="information")
            return
        self.app.push_screen(WorkspaceMessagesScreen(self._generation_id, ws_id))

    def action_open_events(self) -> None:
        self.app.push_screen(EventsScreen(self._generation_id))

    def action_open_report(self) -> None:
        if render.estimate_panel(self._payload) is None:
            self.notify("No HTML report available yet.", severity="information")
            return
        self.run_worker(self._open_report(), exclusive=True)

    async def _open_report(self) -> None:
        """Fetch the HTML report from the backend, cache it locally, then open it.

        The backend runs in a container (local quickstart); its ``artifact_path``
        is a container-internal filesystem path the TUI process can't read
        directly, so the report is fetched over HTTP rather than opened in place.
        """
        try:
            html_bytes = await call_backend_endpoint_bytes(
                endpoint=f"/api/v1/generation-sessions/{self._generation_id}/report.html",
                timeout_seconds=30,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                self.notify("No HTML report available for this run.", severity="information")
            else:
                self.notify(f"Couldn't fetch report: {exc}", severity="warning")
            return
        except Exception as exc:
            self.notify(f"Couldn't fetch report: {exc}", severity="warning")
            return

        cache_path = self.app.root / ".specflow-local" / "reports" / f"{self._generation_id}.html"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(html_bytes)

        opener = _platform_opener()
        if opener is None or not shutil.which(opener):
            self.notify(f"Report saved at {cache_path}", severity="information")
            return
        try:
            await local_env.run_command([opener, str(cache_path)], cache_path.parent, timeout=10)
        except OSError as exc:
            self.notify(f"Couldn't open report automatically: {exc}", severity="warning")

    async def _run_suspended(self, coro) -> None:
        """Run a CLI action with the TUI suspended, then refresh."""
        with self.app.suspend():
            await coro
            input("\nPress Enter to return to the dashboard… ")
        await self.refresh_status()

    def action_retry(self) -> None:
        self.run_worker(self._retry_flow(), exclusive=True)

    async def _retry_flow(self) -> None:
        ok = await self.app.push_screen_wait(
            ConfirmScreen(
                "Retry this failed generation? It resumes from the last "
                "checkpoint on the same workspaces.",
                countdown=0,
            )
        )
        if not ok:
            return
        self.notify("Retrying generation…", severity="information")
        try:
            await retry_generation_core(self._generation_id)
        except Exception as exc:  # noqa: BLE001 - surface the backend's reason to the user
            await self.app.push_screen_wait(MessageScreen("Retry failed", str(exc)))
            return
        self.notify("Retry queued. Resuming from the last checkpoint.", severity="information")
        await self.refresh_status()

    def action_cancel(self) -> None:
        self.run_worker(self._cancel_flow(), exclusive=True)

    async def _cancel_flow(self) -> None:
        """Cancel this specific run via the backend DELETE, then refresh in place.

        Targets ``self._generation_id`` directly (the dashboard may show any session
        opened from the list). No terminal suspend — cancellation produces no output.
        """
        ok = await self.app.push_screen_wait(
            ConfirmScreen(
                "Cancel this generation? It stops all agents immediately and "
                "cannot be resumed (the workspace is kept).",
                countdown=0,
            )
        )
        if not ok:
            return
        try:
            await call_backend_endpoint(
                endpoint=f"/api/v1/generation-sessions/{self._generation_id}",
                method="DELETE",
                timeout_seconds=30,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                self.notify(
                    "Generation already finished — nothing to cancel.",
                    severity="information",
                )
            else:
                self.notify(f"Couldn't cancel: {exc}", severity="warning")
            return
        except Exception as exc:  # noqa: BLE001 - surface any failure to the user
            self.notify(f"Couldn't cancel: {exc}", severity="warning")
            return
        self.notify("Generation cancelled.", severity="information")
        await self.refresh_status()

    def action_clear(self) -> None:
        self.run_worker(self._clear_flow(), exclusive=True)

    async def _clear_flow(self) -> None:
        if fetch_pool_status is None:
            await self.app.push_screen_wait(
                MessageScreen("Clear workspace", "Workspace status unavailable.")
            )
            return
        set_no = render.run_set_number(self._payload)
        try:
            pool = await fetch_pool_status()
        except Exception:  # noqa: BLE001 - clear eligibility is best-effort
            await self.app.push_screen_wait(
                MessageScreen(
                    "Clear workspace",
                    "Couldn't reach the server to check workspace state.",
                )
            )
            return
        cleaning = {
            entry.get("set_number")
            for entry in pool.get("cleaning_sets") or []
            if entry.get("set_number") is not None
        }
        if not render.clear_ws_eligible(set_no, cleaning):
            await self.app.push_screen_wait(
                MessageScreen(
                    "Clear workspace",
                    render.clear_ws_ineligible_message(self._payload),
                )
            )
            return
        ok = await self.app.push_screen_wait(
            ConfirmScreen(
                f"Clear workspace set {set_no}? This permanently resets the "
                "workspaces and cannot be undone.",
                countdown=10,
            )
        )
        if ok:
            await self._run_suspended(actions.do_clear_set(set_no))

    def action_settings(self) -> None:
        self.app.push_screen(SettingsScreen())

    def action_sessions(self) -> None:
        self.app.push_screen(SessionsScreen())

    def action_connect_client(self) -> None:
        self.app.push_screen(ClientSetupScreen())


class WorkspaceMessagesScreen(_SpecFlowScreen):
    """Workspace drill-in: live SDK message stream (top) + stats (bottom).

    The live feed is fed by a best-effort SSE worker (``tui.stream``); the stats
    panel reuses the same ``/status`` poll the dashboard uses. Neither path can
    crash the screen — a dropped stream just shows an "ended" line and stats fall
    back to a placeholder.
    """

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("b", "back", "back"),
    ]

    def __init__(self, generation_id: str, workspace_id: str) -> None:
        super().__init__()
        self._generation_id = generation_id
        self._workspace_id = workspace_id
        self._got_event = False

    @property
    def generation_id(self) -> str:
        return self._generation_id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="ws-stream", wrap=True, markup=False, highlight=False, max_lines=2000)
            yield Static(id="ws-warnings")
            yield Static(id="ws-stats")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#ws-stream", RichLog)
        log.border_title = f"Live messages · {self._workspace_id}"
        log.write(Text("Waiting for live messages…", style="dim"))
        self.query_one("#ws-warnings", Static).display = False
        self.call_later(self.refresh_stats)
        self.set_interval(self.app.poll_interval, self.refresh_stats)
        self.run_worker(self._stream_worker(), exclusive=True, group="ws-stream")

    async def refresh_stats(self) -> None:
        payload = await poll_once(self._generation_id)
        if isinstance(self.app, SpecFlowTUI):
            self.app.process_payload(self._generation_id, payload)
        warnings_widget = self.query_one("#ws-warnings", Static)
        warnings_panel = build_workspace_warnings(payload, self._workspace_id)
        warnings_widget.display = warnings_panel is not None
        if warnings_panel is not None:
            warnings_widget.update(warnings_panel)
        self.query_one("#ws-stats", Static).update(
            build_workspace_stats(payload, self._workspace_id)
        )

    async def _stream_worker(self) -> None:
        log = self.query_one("#ws-stream", RichLog)
        async for event in workspace_message_events(self._generation_id, self._workspace_id):
            self._got_event = True
            log.write(stream_row_text(render.stream_row(event)))
        log.write(Text("— live message stream ended —", style="dim"))

    def action_back(self) -> None:
        self.app.pop_screen()


class EventsScreen(_SpecFlowScreen):
    """Full scrollable log of agent error/warning events for one generation.

    The dashboard's "Agent warnings" panel shows only the newest few; this
    screen lists everything the /status payload carries, refreshed on the same
    poll cadence as the dashboard.
    """

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("b", "back", "back"),
    ]

    def __init__(self, generation_id: str) -> None:
        super().__init__()
        self._generation_id = generation_id
        self._rendered_count = 0

    @property
    def generation_id(self) -> str:
        return self._generation_id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="events-log", wrap=True, markup=False, highlight=False, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#events-log", RichLog)
        log.border_title = f"Agent events · {self._generation_id[:18]}…"
        log.write(Text("Loading events…", style="dim"))
        self.call_later(self.refresh_events)
        self.set_interval(self.app.poll_interval, self.refresh_events)

    async def refresh_events(self) -> None:
        payload = await poll_once(self._generation_id)
        if isinstance(self.app, SpecFlowTUI):
            self.app.process_payload(self._generation_id, payload)
        rows = render.agent_error_event_rows(payload)
        log = self.query_one("#events-log", RichLog)
        if not rows:
            if self._rendered_count == 0:
                log.clear()
                log.write(Text("No agent errors or warnings recorded.", style="dim"))
                self._rendered_count = -1  # placeholder written; skip rewrites
            return
        if len(rows) == self._rendered_count:
            return  # nothing new — avoid churning the scroll position
        log.clear()
        for row in rows:
            log.write(_event_row_text(row))
        self._rendered_count = len(rows)

    def action_back(self) -> None:
        self.app.pop_screen()


class _SessionItem(ListItem):
    """A sessions-list row carrying its generation id."""

    def __init__(self, generation_id: str, label: str) -> None:
        super().__init__(Label(label))
        self.generation_id = generation_id


def _session_label(s: dict) -> str:
    """Format a session dict as a fixed-width sessions-list label.

    Columns: status-symbol  generation-id  date  checkpoint-name
    """
    gid = s.get("generation_id", "")
    status_key = (s.get("status") or "unknown").lower()
    pill_text, _ = STATUS_PILLS.get(status_key, STATUS_PILLS["unknown"])
    # pill_text is like "✓ COMPLETED" — take the leading symbol only
    symbol = pill_text.split()[0] if pill_text else "?"

    # Human-readable checkpoint label from the steps mirror
    checkpoint_key = s.get("checkpoint", "")
    checkpoint_label = next(
        (step.label for step in CHECKPOINT_STEPS if step.completed_at == checkpoint_key),
        checkpoint_key,
    )

    # Date from ISO created_at ("2026-06-30T14:23:45+00:00" → "Jun 30 14:23")
    created_at_raw = s.get("created_at", "")
    date_str = ""
    if created_at_raw:
        try:
            dt = datetime.fromisoformat(created_at_raw).astimezone(timezone.utc)
            date_str = dt.strftime("%b %d %H:%M")
        except ValueError:
            date_str = created_at_raw[:16]

    # Cancellations are always user-initiated; spell that out in the list.
    status_word = "CANCELLED BY USER" if status_key == "cancelled" else status_key.upper()
    status_col = f"{symbol} {status_word:<17}"
    date_col = f"{date_str:<14}" if date_str else " " * 14
    return f"{status_col}  {gid[:22]:<24}  {date_col}  {checkpoint_label}"


class SessionsScreen(_SpecFlowScreen):
    """Picker across all recent generation sessions (active and completed)."""

    BINDINGS = [
        Binding("r", "reload", "reload"),
        Binding("c", "connect_client", "Add MCP to AI tool"),
        Binding("s", "settings", "settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "SpecFlow · sessions   (↑/↓ select · ↵ open · r reload · s settings)",
            id="sessions-title",
        )
        yield ListView(id="sessions-list")
        yield Footer()

    def on_mount(self) -> None:
        self.call_later(self.reload_sessions)

    async def reload_sessions(self) -> None:
        listview = self.query_one("#sessions-list", ListView)
        await listview.clear()
        if fetch_sessions is None:
            return
        try:
            sessions = await fetch_sessions()
        except Exception as exc:  # noqa: BLE001 - listing is best-effort
            self.notify(f"Could not list sessions: {exc}", severity="warning")
            return
        if not sessions:
            await listview.append(ListItem(Label("No generation sessions found.")))
            return
        for s in sessions:
            gid = s.get("generation_id", "")
            if not gid:
                continue
            await listview.append(_SessionItem(gid, _session_label(s)))

    def action_reload(self) -> None:
        self.call_later(self.reload_sessions)

    def action_connect_client(self) -> None:
        self.app.push_screen(ClientSetupScreen())

    def action_settings(self) -> None:
        self.app.push_screen(SettingsScreen())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, _SessionItem):
            self.app.push_screen(DashboardScreen(item.generation_id))


async def _run_init_streamed(app: "SpecFlowTUI", log: RichLog) -> int:
    """Resolve the repo root and run ``specflow-init.sh`` streaming into ``log``.

    Shared by ``OnboardingScreen`` (wizard tail) and ``RunInitScreen`` (no-wizard
    path when ``.env`` is already complete). Returns the init exit code; each
    caller owns its own success/failure UI.
    """
    log.display = True
    repo = local_env.repo_root(app.root) or app.root
    return await local_env.run_init(repo, local_env.InitFlags(), on_line=log.write)


def _platform_opener() -> str | None:
    """The OS command that hands a URL/deeplink to the registered handler."""
    if sys.platform == "darwin":
        return "open"
    if sys.platform.startswith("linux"):
        return "xdg-open"
    return None  # Windows deeplink open is out of v1 scope (file-merge still runs)


class _ClientItem(ListItem):
    """A client row carrying its ``client_id`` and an updatable Rich label."""

    def __init__(self, client_id: str, text: Text) -> None:
        self._label = Label(text)
        super().__init__(self._label)
        self.client_id = client_id

    def set_text(self, text: Text) -> None:
        self._label.update(text)


# Per-model validation glyphs (shared by the connect-screen tiers box and
# Settings). Symbol-first so state reads at a glance; green only for a model the
# provider catalog confirms, red only for a confidently-absent one, neutral when
# the catalog can't be checked. Maps the backend TierValidationStatus values.
_MODEL_MARKS: dict[str, tuple[str, str]] = {
    "valid": ("✓", "green"),
    "invalid": ("✗", "red"),
    "unverified": ("•", "dim"),
}
_NEUTRAL_MARK: tuple[str, str] = ("•", "dim")


def _model_mark(status: str | None) -> tuple[str, str]:
    """(symbol, style) for one model's validation status."""
    return _MODEL_MARKS.get(status or "", _NEUTRAL_MARK)


def _tier_mark(models: list[dict]) -> tuple[str, str]:
    """(symbol, style) for a whole tier: red if any model is invalid, green if
    all are valid, else neutral (some/all unverified — catalog unavailable)."""
    statuses = [m.get("status") for m in models]
    if "invalid" in statuses:
        return _MODEL_MARKS["invalid"]
    if statuses and all(s == "valid" for s in statuses):
        return _MODEL_MARKS["valid"]
    return _NEUTRAL_MARK


def _tier_marker_text(models: list[dict]) -> Text:
    """Short 'glyph + word' marker for a Settings tier field (blank when no models
    or only unverified — we never assert availability we can't confirm)."""
    if not models:
        return Text("")
    symbol, style = _tier_mark(models)
    if any(m.get("status") == "invalid" for m in models):
        word = "unsupported"
    elif all(m.get("status") == "valid" for m in models):
        word = "available"
    else:
        word = ""
    return Text(f"{symbol} {word}".strip(), style=style)


class ClientSetupScreen(_SpecFlowScreen):
    """Register the SpecFlow MCP server with the user's AI client(s).

    A thin renderer over ``tui.mcp_clients``: it lists the registry with
    detection badges, runs the right add mechanism per client (CLI add /
    deeplink+file-merge / copy), and reports an honest status — never a false
    "verified" for a client we cannot read back (Cursor). Replaces the
    misleading "No active generation sessions." landing on first run.
    """

    BINDINGS = [
        # Screen-specific actions are documented in the header hint (see compose);
        # keep them working but out of the footer. The footer carries navigation.
        Binding("d", "show_config", "raw config", show=False),
        Binding("v", "recheck", "re-scan", show=False),
        Binding("m", "edit_tiers", "model tiers", show=False),
        Binding("s", "skip", "skip", show=False),
        Binding("escape", "skip", "return", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[mcp_clients.ClientRow] = []
        self._status: dict[str, mcp_clients.ClientStatus] = {}
        # Baked-config drift: the live block's fingerprint vs the one recorded
        # for each client at connect time. None disables the overlay (unreadable
        # block); recomputed each _populate so a Settings edit is reflected.
        self._current_fp: str | None = None
        self._saved_fp: dict[str, str] = {}
        # Last model-validation response (parsed) for the tiers box, or None
        # before/without a result. Recomputed by a background worker each populate.
        self._validation: dict | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        # Screen-specific action hints live here in the header; the footer carries
        # navigation (esc return) so the two don't duplicate each other.
        yield Static(
            "[b]Connect SpecFlow to your AI tool[/b]   "
            "[dim]↑/↓ select · ↵ connect · m model tiers · d raw config · v re-scan[/dim]",
            id="client-setup-title",
        )
        yield ListView(id="client-list")
        yield Static(id="client-detail")
        log = RichLog(id="client-log", highlight=False, markup=False, wrap=True)
        log.display = False
        yield log
        # Persistent box: the tiers a connect would bake in, what each drives,
        # how to change them, and the caveat that a change only reaches the next run.
        yield Static(id="client-tiers")
        yield Footer()

    def on_mount(self) -> None:
        self.call_later(self._populate)

    # -- rendering ---------------------------------------------------------

    async def _populate(self) -> None:
        self._rows = mcp_clients.client_rows()
        # Recompute the drift baseline: the live block's fingerprint and each
        # client's recorded one. A missing/malformed block disables the overlay
        # (fail-safe) rather than crashing the screen.
        try:
            block: mcp_clients.ServerBlock | None = mcp_clients.load_server_block(self.app.root)
            self._current_fp = mcp_clients.config_fingerprint(block)
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            block = None
            self._current_fp = None
        self._saved_fp = mcp_clients.saved_fingerprints()
        # Render the tiers box immediately (neutral glyphs), then validate the
        # configured models against the provider catalog in the background.
        self._validation = None
        self.query_one("#client-tiers", Static).update(self._tiers_panel(block))
        self.run_worker(self._validate_models(block), group="validate")
        self._status = {
            r.client.client_id: mcp_clients.initial_status(
                r.client, installed=r.installed, saved=r.saved
            )
            for r in self._rows
        }
        # Clients we can read back get probed on mount; show "verifying…" up front
        # so the row never looks like an idle "press ↵ to connect" the user clicks.
        for row in self._rows:
            cid = row.client.client_id
            if (
                row.installed
                and row.client.can_verify
                and self._status[cid] is mcp_clients.ClientStatus.NOT_CONFIGURED
            ):
                self._status[cid] = mcp_clients.ClientStatus.VERIFYING
        listview = self.query_one("#client-list", ListView)
        await listview.clear()
        for row in self._rows:
            cid = row.client.client_id
            await listview.append(_ClientItem(cid, self._row_label(row, self._status[cid])))
        # Pre-select the first installed, non-manual client so Enter just works.
        idx = next(
            (
                i
                for i, r in enumerate(self._rows)
                if r.installed and r.client.strategy is not mcp_clients.AddStrategy.MANUAL_COPY
            ),
            0,
        )
        listview.index = idx
        self._render_detail()
        # Reconcile clients we can read back (Claude): a user who already has the
        # server registered is shown connected without re-adding, and a stale
        # saved "connected" is cleared if it's gone. Runs in the background so it
        # never blocks the initial render.
        self.run_worker(self._probe_verifiable(), group="probe")

    async def _probe_verifiable(self) -> None:
        for row in self._rows:
            client = row.client
            if not row.installed or not client.can_verify:
                continue
            argv = mcp_clients.build_verify_argv(client)
            if argv is None:
                continue
            result = await local_env.run_command(argv, self.app.root, timeout=15)
            present = result.ok and mcp_clients.verify_passed(result.output)
            current = self._status.get(client.client_id)
            if present:
                if current is not mcp_clients.ClientStatus.VERIFIED:
                    self._set_status(client.client_id, mcp_clients.ClientStatus.VERIFIED)
                    mcp_clients.save_status(client.client_id, mcp_clients.ClientStatus.VERIFIED)
            else:
                # Not registered now — resolve the "verifying…" placeholder to a
                # plain "press ↵ to connect", and forget any stale saved connection
                # (status and its drift baseline) so a dropped client starts clean.
                if current in (mcp_clients.ClientStatus.VERIFIED, mcp_clients.ClientStatus.CONNECTED):
                    mcp_clients.forget_status(client.client_id)
                    mcp_clients.forget_fingerprint(client.client_id)
                    self._saved_fp.pop(client.client_id, None)
                self._set_status(client.client_id, mcp_clients.ClientStatus.NOT_CONFIGURED)
        self._render_detail()

    def _is_stale(self, client_id: str, status: mcp_clients.ClientStatus) -> bool:
        """True if this client is connected but its baked config has drifted."""
        return (
            self._current_fp is not None
            and status in mcp_clients._ACTED_STATUSES
            and mcp_clients.is_stale(self._saved_fp.get(client_id), self._current_fp)
        )

    def _row_label(self, row: mcp_clients.ClientRow, status: mcp_clients.ClientStatus) -> Text:
        # The "Other / copy config" row can never be verified (no read-back), so
        # it stays grey; a connected client whose config drifted goes orange. Both
        # rules live in mcp_clients.row_label/row_style so the colour and the words
        # can never disagree. Shape (●/○) still marks installed vs not.
        is_manual = row.client.strategy is mcp_clients.AddStrategy.MANUAL_COPY
        stale = self._is_stale(row.client.client_id, status)
        style = mcp_clients.row_style(status, stale=stale, is_manual=is_manual)
        line = Text()
        line.append(f"{row.client.icon} ", style="bold cyan")
        line.append(f"{row.client.name:<20} ", style="bold")
        badge = "●  " if row.installed else "○  "
        line.append(badge, style=style)
        line.append(mcp_clients.row_label(status, stale=stale, is_manual=is_manual), style=style)
        return line

    # What each tier drives (mirrors the backend WORKFLOW_TIER_MAP) so the user
    # knows which model matters for which stage. A blank tier = backend default.
    _TIER_PURPOSE: dict[str, str] = {
        "LLM_HIGH": "planning & knowledge-base init",
        "LLM_MEDIUM": "code generation",
        "LLM_LOW": "simple steps (md→JSON, spec indexing)",
    }

    def _model_cell(self, value: str | None, models: list[dict]) -> Text:
        """The model(s) cell: raw config text, each model tinted by its validity.

        A blank tier reads as the backend default. When validation is present,
        each configured model is coloured green/red/dim per its catalog status.
        """
        if not value:
            return Text("default", style="italic dim")
        status_by_model = {m.get("configured"): m.get("status") for m in models}
        cell = Text()
        for i, token in enumerate([t.strip() for t in value.split(",") if t.strip()]):
            if i:
                cell.append(", ", style="dim")
            status = status_by_model.get(token)
            _, style = _model_mark(status) if status else ("", "")
            cell.append(token, style=style)
        return cell

    def _invalid_notes(self, validation: dict | None) -> Text | None:
        """One red line per confidently-invalid model, with a suggestion if any."""
        if not validation:
            return None
        invalid = validate_models.invalid_models(validation)
        if not invalid:
            return None
        provider = validation.get("provider", "the active provider")
        note = Text()
        for i, model in enumerate(invalid):
            if i:
                note.append("\n")
            tier = str(model.get("tier", "")).removeprefix("LLM_").lower()
            note.append("✗ ", style="red")
            note.append(f"{tier}: ", style="bold red")
            note.append(f"'{model.get('configured', '?')}' not available on {provider}", style="red")
            if suggestion := model.get("suggestion"):
                note.append(f" — did you mean '{suggestion}'?", style="red")
        return note

    def _tiers_panel(
        self, block: mcp_clients.ServerBlock | None, validation: dict | None = None
    ) -> Panel:
        # A bordered box showing the model tiers a connect would bake in, a per-tier
        # validity glyph (✓/✗/•), what each drives, the `m` affordance, and the
        # standing caveat that a change reaches the sandbox only from the next run.
        env = block.env if block is not None else {}
        by_tier = {t.get("tier"): t.get("models", []) for t in (validation or {}).get("tiers", [])}
        table = Table.grid(padding=(0, 1))
        table.add_column()  # validity glyph
        table.add_column(style="bold cyan")  # tier
        table.add_column()  # model(s)
        table.add_column(style="dim")  # purpose
        for key in LLM_TIER_KEYS:
            models = by_tier.get(key, [])
            symbol, sym_style = _tier_mark(models) if models else ("", "")
            table.add_row(
                Text(symbol, style=sym_style),
                key.removeprefix("LLM_").lower(),
                self._model_cell(env.get(key), models),
                self._TIER_PURPOSE.get(key, ""),
            )
        footer = Text(
            "press m to change · a change takes effect from the next run "
            "(a generation already in progress keeps its models)",
            style="dim italic",
        )
        parts: list[RenderableType] = [table]
        if (notes := self._invalid_notes(validation)) is not None:
            parts += [Text(), notes]
        parts += [Text(), footer]
        return Panel(
            Group(*parts),
            title="Model tiers",
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    async def _validate_models(self, block: mcp_clients.ServerBlock | None) -> None:
        """Validate the configured tier models against the provider catalog, then
        re-render the box with per-model glyphs. Fully best-effort: any failure
        (no backend, unreachable catalog) leaves the box neutral — never red."""
        if block is None:
            return
        tier_values = {key: block.env.get(key, "") for key in LLM_TIER_KEYS}
        try:
            response = await validate_models.request_model_validation_for(tier_values)
        except Exception:  # noqa: BLE001 - infra failure must stay silent here
            return
        self._validation = response
        try:
            self.query_one("#client-tiers", Static).update(self._tiers_panel(block, response))
        except NoMatches:
            pass  # screen dismissed before validation returned

    def _selected_client(self) -> mcp_clients.McpClient | None:
        listview = self.query_one("#client-list", ListView)
        item = listview.highlighted_child
        if not isinstance(item, _ClientItem):
            return None
        return self._client_by_id(item.client_id)

    def _client_by_id(self, client_id: str) -> mcp_clients.McpClient | None:
        return next((r.client for r in self._rows if r.client.client_id == client_id), None)

    def _row_by_id(self, client_id: str) -> mcp_clients.ClientRow | None:
        return next((r for r in self._rows if r.client.client_id == client_id), None)

    def _render_detail(self) -> None:
        client = self._selected_client()
        if client is None:
            return
        row = self._row_by_id(client.client_id)
        detail = Text()
        detail.append(client.description)  # inherits the themed muted colour
        if row is not None and not row.installed and client.strategy is not (
            mcp_clients.AddStrategy.MANUAL_COPY
        ):
            detail.append(
                "\nNot installed — install it, then press v to re-scan.", style="red"
            )
        if client.caveat:
            detail.append(f"\n{client.caveat}", style="yellow")
        self.query_one("#client-detail", Static).update(detail)

    def _set_status(self, client_id: str, status: mcp_clients.ClientStatus) -> None:
        self._status[client_id] = status
        row = self._row_by_id(client_id)
        listview = self.query_one("#client-list", ListView)
        for item in listview.query(_ClientItem):
            if item.client_id == client_id and row is not None:
                item.set_text(self._row_label(row, status))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._render_detail()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_connect()

    # -- actions -----------------------------------------------------------

    def action_connect(self) -> None:
        client = self._selected_client()
        if client is None:
            return
        cid = client.client_id
        if client.strategy is mcp_clients.AddStrategy.MANUAL_COPY:
            self._show_config(client)
            return
        # An "added — confirm" client: pressing ↵ asks the user what they found
        # when they inspected it, rather than blindly re-adding — unless its
        # config has drifted, in which case ↵ means "reconnect" (re-bake), so we
        # fall through to the connect flow to rewrite it with the current block.
        status = self._status.get(cid)
        if status is mcp_clients.ClientStatus.ADDED_UNVERIFIED and not self._is_stale(cid, status):
            self.run_worker(self._inspect_flow(client), exclusive=True)
            return
        row = self._row_by_id(cid)
        if row is not None and not row.installed:
            self.notify(f"{client.name} isn't installed — install it, then press v.", severity="warning")
            return
        self.run_worker(self._connect_flow(client), exclusive=True)

    def action_recheck(self) -> None:
        self.call_later(self._rescan)

    def action_edit_tiers(self) -> None:
        # Let the user set LLM model tiers (and other runtime settings) *before*
        # connecting, so tools bake the intended config rather than the defaults.
        # Re-populate on return so any edit is reflected (and any resulting drift
        # on already-connected clients shows immediately).
        self.run_worker(self._edit_tiers_flow(), exclusive=True)

    async def _edit_tiers_flow(self) -> None:
        await self.app.push_screen_wait(SettingsScreen())
        await self._populate()

    async def _rescan(self) -> None:
        await self._populate()
        self.notify("Re-scanned installed clients.", severity="information")

    def action_skip(self) -> None:
        self.dismiss(None)

    def action_show_config(self) -> None:
        client = self._selected_client()
        if client is None:
            return
        self._show_config(client)

    def _load_block(self) -> mcp_clients.ServerBlock | None:
        try:
            return mcp_clients.load_server_block(self.app.root)
        except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
            self.notify(f"Couldn't read the SpecFlow config: {exc}", severity="error")
            return None

    def _show_config(self, client: mcp_clients.McpClient) -> None:
        block = self._load_block()
        if block is None:
            return
        config = mcp_clients.render_config_file(block, client.config_shape)
        body = f"Paste this into your MCP client's config:\n\n{config}"
        # markup=False: the JSON body has literal [ ] arrays Rich must not parse.
        self.app.push_screen(MessageScreen(f"{client.name} — MCP config", body, markup=False))

    async def _connect_flow(self, client: mcp_clients.McpClient) -> None:
        cid = client.client_id
        if client.strategy is mcp_clients.AddStrategy.MANUAL_COPY:
            self._show_config(client)
            return
        block = self._load_block()
        if block is None:
            return
        self._set_status(cid, mcp_clients.ClientStatus.CONNECTING)
        log = self.query_one("#client-log", RichLog)
        log.display = True
        log.clear()

        if client.strategy is mcp_clients.AddStrategy.DEEPLINK:
            status = await self._connect_cursor(client, block, log)
        else:
            status = await self._connect_cli(client, block, log)

        connected = status in (
            mcp_clients.ClientStatus.VERIFIED,
            mcp_clients.ClientStatus.ADDED_UNVERIFIED,
        )
        # Record the block we just baked in as this client's drift baseline before
        # rendering, so a reconnect immediately clears the stale overlay (rather
        # than staying orange until the next populate re-reads the same block).
        if connected:
            fingerprint = mcp_clients.config_fingerprint(block)
            mcp_clients.save_fingerprint(cid, fingerprint)
            self._saved_fp[cid] = fingerprint
        self._set_status(cid, status)
        # Persist the real outcome — including "added (unverified)" — so next time
        # the screen shows actual state, never an assumed connection.
        mcp_clients.save_status(cid, status)
        if connected:
            await self.app.push_screen_wait(
                MessageScreen(f"Connected · {client.name}", mcp_clients.success_body(client, status))
            )
        elif status is mcp_clients.ClientStatus.FAILED:
            self.notify(f"Couldn't connect {client.name} — see the log.", severity="error")

    async def _inspect_flow(self, client: mcp_clients.McpClient) -> None:
        """Ask the user what they found when they inspected an unverified client.

        SpecFlow can't read Cursor/Gemini state back, so the user closes the loop:
        confirm it works (→ connected) or report it doesn't (→ failed, re-addable).
        """
        choice = await self.app.push_screen_wait(VerifyChoiceScreen(client.name))
        if choice is None:
            return  # dismissed without deciding — leave the status untouched
        new_status = (
            mcp_clients.ClientStatus.CONNECTED if choice else mcp_clients.ClientStatus.FAILED
        )
        self._set_status(client.client_id, new_status)
        mcp_clients.save_status(client.client_id, new_status)
        if choice:
            self.notify(f"{client.name} marked connected.", severity="information")
        else:
            self.notify(
                f"{client.name} marked not working — press ↵ to add it again.",
                severity="warning",
            )

    async def _connect_cli(
        self, client: mcp_clients.McpClient, block: mcp_clients.ServerBlock, log: RichLog
    ) -> mcp_clients.ClientStatus:
        root = self.app.root
        remove = mcp_clients.build_remove_argv(client)
        if remove is not None:
            # Idempotent upsert: clear any prior entry first (Claude errors on collision).
            await local_env.run_command(remove, root, on_line=log.write, timeout=30)
        add_res = await local_env.run_command(
            mcp_clients.build_add_argv(client, block), root, on_line=log.write, timeout=30
        )
        verify_output: str | None = None
        if add_res.ok and client.can_verify:
            verify_argv = mcp_clients.build_verify_argv(client)
            if verify_argv is not None:
                verify_res = await local_env.run_command(
                    verify_argv, root, on_line=log.write, timeout=30
                )
                verify_output = verify_res.output
        return mcp_clients.status_after_add(
            client, add_ok=add_res.ok, verify_output=verify_output
        )

    async def _connect_cursor(
        self, client: mcp_clients.McpClient, block: mcp_clients.ServerBlock, log: RichLog
    ) -> mcp_clients.ClientStatus:
        target = client.file_target
        if target is None or sys.platform not in target.platforms:
            log.write("This client isn't supported on your platform.\n")
            return mcp_clients.ClientStatus.FAILED
        path = target.resolved_path()
        existing, ok_to_write = await self._read_existing_config(path, log)
        if not ok_to_write:
            return mcp_clients.ClientStatus.FAILED
        merged = mcp_clients.merge_block(existing, block, target.key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(merged, indent=2))
        except OSError as exc:
            log.write(f"Couldn't write {path}: {exc}\n")
            return mcp_clients.ClientStatus.FAILED
        log.write(f"Wrote {path}\n")
        # The write succeeded, so our server block is on disk (merge_block put it
        # there). Cursor itself stays unverifiable — status_after_add caps it at
        # ADDED_UNVERIFIED regardless, so there's nothing to read back.
        # Fire the deeplink as a convenience so Cursor offers an approval prompt.
        # Best-effort only: the file write above is the real registration, so a
        # missing opener binary must never fail (or crash) the connect.
        url = mcp_clients.build_deeplink(block, client)
        opener = _platform_opener()
        if opener is not None and shutil.which(opener) and not mcp_clients.deeplink_too_long(url):
            try:
                await local_env.run_command([opener, url], path.parent, on_line=log.write, timeout=10)
            except OSError as exc:
                log.write(f"(couldn't open Cursor automatically: {exc})\n")
        return mcp_clients.status_after_add(client, add_ok=True)

    async def _read_existing_config(
        self, path: Path, log: RichLog
    ) -> tuple[dict, bool]:
        """Read an existing JSON config, refusing to clobber a malformed one.

        Returns ``(existing, ok_to_write)``. A malformed file is never silently
        overwritten — the user must approve a ``.bak`` backup first.
        """
        if not path.exists():
            return {}, True
        try:
            return json.loads(path.read_text()), True
        except json.JSONDecodeError:
            approved = await self.app.push_screen_wait(
                ConfirmScreen(
                    f"{path} isn't valid JSON. Back it up to .bak and overwrite?",
                    countdown=0,
                )
            )
            if not approved:
                log.write("Left your existing config untouched.\n")
                return {}, False
            # Never clobber a previous backup — pick the first free .bak name.
            backup = path.parent / f"{path.name}.bak"
            counter = 1
            while backup.exists():
                backup = path.parent / f"{path.name}.bak.{counter}"
                counter += 1
            backup.write_text(path.read_text())
            log.write(f"Backed up your config to {backup}\n")
            return {}, True


class OnboardingScreen(_SpecFlowScreen):
    """First-run setup as a guided, step-by-step wizard.

    Walks the user through one credential at a time — each step explains what it
    is, why it's needed, and how to obtain it (with the exact URL) — then a final
    review step writes ``.env`` (seeded from ``.env.quickstart.example`` when
    absent) and runs ``specflow init`` in a worker, streaming output into a log
    pane. On success it dismisses ``True`` so the startup gate proceeds.

    Step content and validation live in the pure ``tui.onboarding`` module. This
    screen is a single screen that swaps its body per step, persisting entered
    values into ``self._values`` so navigating Back/Next never loses input.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "save & init"),
        Binding("ctrl+n", "next", "next"),
        Binding("ctrl+b", "back", "back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._index = 0
        self._chosen_provider = onboarding.PROVIDER_STEP.default_choice
        self._values: dict[str, str] = {}

    @property
    def current_step(self) -> onboarding.Step:
        return onboarding.STEPS[self._index]

    @property
    def current_step_id(self) -> str:
        return self.current_step.step_id

    # -- compose / per-step rendering --------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="onboard-title")
        yield VerticalScroll(id="onboard-body")
        with Horizontal(id="onboard-nav"):
            yield Button("Back", id="onboard-back")
            yield Button("Next", id="onboard-next", variant="primary")
            yield Button("Save & Initialize", id="onboard-go", variant="primary")
        log = RichLog(id="onboard-log", highlight=False, markup=False, wrap=True)
        log.display = False
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self.call_later(self._mount_step)

    async def _mount_step(self) -> None:
        step = self.current_step
        total = len(onboarding.STEPS)
        title = self.query_one("#onboard-title", Static)
        title.update(f"SpecFlow · setup   (step {self._index + 1}/{total})   {step.title}")

        body = self.query_one("#onboard-body", VerticalScroll)
        await body.remove_children()

        widgets: list[Any] = []
        if step.why:
            widgets.append(Static(step.why, classes="onboard-why"))

        if step.kind is onboarding.StepKind.CHOICE:
            widgets.extend(self._choice_widgets(step))
        elif step.kind is onboarding.StepKind.FIELDS:
            widgets.extend(self._howto_widgets(step.how_to))
            widgets.extend(self._field_rows(step.fields))
        elif step.kind is onboarding.StepKind.REVIEW:
            widgets.append(self._review_widget())
        else:  # INFO
            widgets.extend(self._howto_widgets(step.how_to))

        await body.mount(*widgets)
        if step.kind is onboarding.StepKind.CHOICE:
            await self._render_choice_detail()
        self._update_nav()

    def _howto_widgets(self, how_to: tuple[str, ...]) -> list[Any]:
        return [Static(line, classes="onboard-howto") for line in how_to]

    def _field_rows(self, fields: tuple[onboarding.Field, ...]) -> list[Any]:
        rows: list[Any] = []
        for f in fields:
            mark = " *" if f.required else ""
            stored = self._values.get(f.key, "")
            rows.append(
                Horizontal(
                    Label(f"{f.label}{mark}", classes="settings-label"),
                    Input(
                        value="" if f.masked else stored,
                        password=f.masked,
                        placeholder=f.hint,
                        id=f"onb-{f.key}",
                    ),
                    classes="settings-row",
                )
            )
        return rows

    def _choice_widgets(self, step: onboarding.Step) -> list[Any]:
        radio = RadioSet(
            *(
                RadioButton(c.label, value=(c.option_id == self._chosen_provider))
                for c in step.choices
            ),
            id="onboard-provider",
        )
        # The selected provider's how-to + key field are (re)built into the
        # detail container by _render_choice_detail on mount and on selection.
        return [radio, VerticalScroll(id="onboard-choice-detail")]

    def _review_widget(self) -> Any:
        lines: list[str] = []
        chosen = onboarding.provider_field(self._chosen_provider)
        secrets = onboarding.collected_secrets(self._values, self._chosen_provider)
        field_by_key = {f.key: f for s in onboarding.STEPS for f in s.fields}
        field_by_key[chosen.key] = chosen
        for key in [chosen.key, "GITHUB_TOKEN", "GITHUB_ORG", "GIT_USER_NAME", "P10Y_API_KEY"]:
            f = field_by_key[key]
            if f.masked:
                shown = "•••• set" if secrets.get(key) else "— (missing)"
            else:
                shown = secrets.get(key) or "— (auto)"
            lines.append(f"  {f.label:<26} {shown}")
        # Advanced/optional LangFuse — only recapped when the user provided it.
        if any(secrets.get(key) for key in LANGFUSE_KEYS):
            for key in LANGFUSE_KEYS:
                f = field_by_key[key]
                shown = ("•••• set" if secrets.get(key) else "— (missing)") if f.masked else (
                    secrets.get(key) or "— (missing)"
                )
                lines.append(f"  {f.label:<26} {shown}")
        return Static("\n".join(lines), classes="onboard-howto")

    # -- navigation --------------------------------------------------------

    def _capture(self) -> None:
        """Persist whatever inputs are mounted now into ``self._values``.

        A blank masked field means "keep the stored value" (mirrors the Settings
        convention) so navigating Back/Next through a secret never wipes it — a
        masked field is always mounted blank, never prefilled with the secret.
        The chosen provider is owned solely by ``on_radio_set_changed``.
        """
        for inp in self.query("#onboard-body Input").results(Input):
            key = (inp.id or "")[len("onb-") :]
            if not key:
                continue
            value = inp.value
            if key in MASKED_KEYS and value == "" and self._values.get(key):
                continue
            self._values[key] = value

    def _update_nav(self) -> None:
        is_review = self.current_step.kind is onboarding.StepKind.REVIEW
        self.query_one("#onboard-back", Button).disabled = self._index == 0
        self.query_one("#onboard-next", Button).display = not is_review
        self.query_one("#onboard-go", Button).display = is_review

    def action_back(self) -> None:
        if self._index == 0:
            return
        self._capture()
        self._index -= 1
        self.call_later(self._mount_step)

    def action_next(self) -> None:
        self._capture()
        error = onboarding.validate_step(self.current_step, self._values, self._chosen_provider)
        if error:
            self.notify(error, severity="error")
            return
        if self._index < len(onboarding.STEPS) - 1:
            self._index += 1
            self.call_later(self._mount_step)

    def action_save(self) -> None:
        if self.current_step.kind is onboarding.StepKind.REVIEW:
            self._save_and_init()
        else:
            self.action_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "onboard-next":
            self.action_next()
        elif event.button.id == "onboard-back":
            self.action_back()
        elif event.button.id == "onboard-go":
            self._save_and_init()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._chosen_provider = onboarding.PROVIDER_STEP.choices[event.index].option_id
        self.call_later(self._render_choice_detail)

    async def _render_choice_detail(self) -> None:
        try:
            detail = self.query_one("#onboard-choice-detail", VerticalScroll)
        except NoMatches:
            return
        await detail.remove_children()
        choice = next(
            c for c in onboarding.PROVIDER_STEP.choices if c.option_id == self._chosen_provider
        )
        widgets: list[Any] = [Static(choice.why, classes="onboard-why")]
        widgets.extend(self._howto_widgets(choice.how_to))
        widgets.extend(self._field_rows((choice.field,)))
        await detail.mount(*widgets)

    # -- save + init (unchanged behaviour) ---------------------------------

    def _save_and_init(self) -> None:
        self._capture()
        error = onboarding.validate_all(self._values, self._chosen_provider)
        if error:
            self.notify(error, severity="error")
            return
        non_empty = onboarding.collected_secrets(self._values, self._chosen_provider)
        save_env_secrets(self.app.root, non_empty)
        self.query_one("#onboard-go", Button).disabled = True
        self.run_worker(self._run_init(), exclusive=True)

    async def _run_init(self) -> None:
        log = self.query_one("#onboard-log", RichLog)
        rc = await _run_init_streamed(self.app, log)
        if rc == 0:
            self.dismiss(True)
        else:
            self.notify("Setup failed — review the log and try again.", severity="error")
            self.query_one("#onboard-go", Button).disabled = False


class StartContainersScreen(_SpecFlowScreen):
    """Docker gate: start the stack (or wait out an unhealthy backend), or quit.

    Two modes, selected by ``containers_up``:
      * ``False`` (default) — the containers aren't running. ``y`` runs
        ``docker compose up -d`` then waits for the backend to report ready.
      * ``True`` — the containers ARE running but ``/health/ready`` isn't 200
        yet. ``y`` only re-polls readiness (no redundant ``compose up``); this is
        typically a config problem (e.g. an LLM provider/key mismatch), not a
        stopped container, so we must not claim "containers aren't running".

    ``y`` dismisses ``True`` once the backend is ready; ``n`` dismisses ``False``
    so the gate exits the app.
    """

    BINDINGS = [
        Binding("y", "yes", "start"),
        Binding("n", "no", "quit"),
    ]

    def __init__(self, *, containers_up: bool = False) -> None:
        super().__init__()
        self._containers_up = containers_up

    def compose(self) -> ComposeResult:
        yield Header()
        if self._containers_up:
            prompt = (
                "Containers are running but the backend isn't healthy yet.\n\n"
                "Retry the health check?   [y] retry    [n] quit"
            )
        else:
            prompt = "The SpecFlow containers aren't running.\n\n" "Start them now?   [y] start    [n] quit"
        yield Static(prompt, id="docker-prompt")
        log = RichLog(id="docker-log", highlight=False, markup=False, wrap=True)
        log.display = False
        yield log
        yield Footer()

    def action_no(self) -> None:
        self.dismiss(False)

    def action_yes(self) -> None:
        self.run_worker(self._start(), exclusive=True)

    async def _start(self) -> None:
        log = self.query_one("#docker-log", RichLog)
        log.display = True
        # Containers already up → skip the redundant `compose up` and just re-poll.
        if not self._containers_up:
            await local_env.start_containers(self.app.root, on_line=log.write)
        backend_url = self.app.backend_url
        ok = await local_env.wait_backend_ready(
            backend_url,
            on_attempt=lambda i: log.write(f"waiting for backend to become ready… ({i})\n"),
        )
        if ok:
            self.dismiss(True)
        else:
            self.notify(
                "Backend didn't become healthy — check `docker compose logs` "
                "(e.g. an LLM provider/key mismatch).",
                severity="error",
            )


class RunInitScreen(_SpecFlowScreen):
    """Runs ``specflow init`` off an already-complete ``.env`` — no wizard.

    Used when a fresh checkout has a hand-created ``.env`` with all required
    secrets but no mcp-config yet: init still must write mcp-config and provision
    workspace repos, but the onboarding wizard has nothing left to collect. Auto-
    runs on mount, streams init output into a log pane, then dismisses ``True`` on
    success so the startup gate proceeds; ``False`` on failure so the gate exits.
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Found an existing .env — running `specflow init`…",
            id="runinit-prompt",
        )
        yield RichLog(id="runinit-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._run(), exclusive=True)

    async def _run(self) -> None:
        log = self.query_one("#runinit-log", RichLog)
        rc = await _run_init_streamed(self.app, log)
        if rc == 0:
            self.dismiss(True)
        else:
            self.notify(
                "Setup failed — review the log, fix .env, and try again.",
                severity="error",
            )
            self.dismiss(False)


class SettingsScreen(Screen):
    """Editor for runtime settings (mcp-config.json) and secrets (.env).

    Three sections: runtime settings (mcp-config.json), core secrets (.env), and
    an Advanced section for optional LangFuse tracing (.env). All secret keys
    share one row-builder and one save loop, so a new secret is added in exactly
    one place (its key list) and inherits the masking + blank-means-keep rules.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "cancel", "cancel"),
    ]

    def _secret_rows(self, keys: list[str], secrets: dict[str, str]) -> ComposeResult:
        """Yield a labelled Input row per secret key (masked keys mount blank)."""
        for key in keys:
            masked = key in MASKED_KEYS
            has = bool(secrets.get(key))
            with Horizontal(classes="settings-row"):
                yield Label(f"{key:<22}", classes="settings-label")
                yield Input(
                    value="" if masked else str(secrets.get(key, "")),
                    password=masked,
                    placeholder="•••• set (blank = keep)" if masked and has else "",
                    id=f"secret-{key}",
                )

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("SpecFlow · settings   (ctrl+s save · esc cancel)", id="settings-title")
        env = load_env(self.app.root)
        secrets = load_env_secrets(self.app.root)
        with VerticalScroll(id="settings-form"):
            yield Static("Runtime (mcp-config.json)", classes="settings-section")
            for key in EDITABLE_KEYS:
                with Horizontal(classes="settings-row"):
                    yield Label(f"{key:<22}", classes="settings-label")
                    yield Input(value=str(env.get(key, "")), id=f"field-{key}")
                    # Tier fields carry a live validity marker (✓/✗) updated on
                    # mount and when the field is submitted (Enter).
                    if key in LLM_TIER_KEYS:
                        yield Static(id=f"tierstatus-{key}", classes="tier-status")
            yield Static("Secrets (.env — requires backend restart)", classes="settings-section")
            yield from self._secret_rows(ENV_SECRET_KEYS, secrets)
            yield Static(
                "Advanced · LangFuse tracing (optional, all three or none "
                "— requires backend restart)",
                classes="settings-section",
            )
            yield from self._secret_rows(LANGFUSE_KEYS, secrets)
        yield Footer()

    def _secret_input(self, key: str) -> str:
        return self.query_one(f"#secret-{key}", Input).value.strip()

    def on_mount(self) -> None:
        # Validate the stored tier models once the fields exist, so their ✓/✗
        # markers reflect reality the moment the screen opens.
        self.run_worker(self._validate_tier_inputs(), group="tiervalidate", exclusive=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Re-validate when a tier field is submitted (Enter) — "when changing".
        if event.input.id in {f"field-{key}" for key in LLM_TIER_KEYS}:
            self.run_worker(self._validate_tier_inputs(), group="tiervalidate", exclusive=True)

    async def _validate_tier_inputs(self) -> None:
        """Validate the current tier-field values against the provider catalog and
        update each field's marker. Best-effort: any failure clears to neutral."""
        tier_values = {
            key: self.query_one(f"#field-{key}", Input).value.strip() for key in LLM_TIER_KEYS
        }
        try:
            response = await validate_models.request_model_validation_for(tier_values)
        except Exception:  # noqa: BLE001 - infra failure must stay silent here
            return
        by_tier = {t.get("tier"): t.get("models", []) for t in response.get("tiers", [])}
        for key in LLM_TIER_KEYS:
            try:
                marker = self.query_one(f"#tierstatus-{key}", Static)
            except NoMatches:
                continue
            marker.update(_tier_marker_text(by_tier.get(key, [])))

    def _block_fingerprint(self) -> str | None:
        """Fingerprint of the live server block, or None if it can't be read.

        Guarded so a first run (no config yet) or a malformed file disables the
        drift warning rather than crashing the save.
        """
        try:
            return mcp_clients.config_fingerprint(mcp_clients.load_server_block(self.app.root))
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            return None

    def action_save(self) -> None:
        # Reject a partially-filled LangFuse set before writing anything. Uses the
        # effective post-save state (a blank masked key keeps its stored value),
        # so "all three already set, none edited" is not flagged as partial.
        stored = load_env_secrets(self.app.root)
        effective_langfuse = {
            key: (stored.get(key, "") if (key in MASKED_KEYS and self._secret_input(key) == "")
                  else self._secret_input(key))
            for key in LANGFUSE_KEYS
        }
        partial = langfuse_partial_error(effective_langfuse)
        if partial:
            self.notify(partial, severity="error")
            return

        # Fingerprint the block before the write so we can tell whether the
        # runtime config actually changed (and thus whether connected tools drift).
        old_fp = self._block_fingerprint()
        new_env = {
            key: self.query_one(f"#field-{key}", Input).value.strip() for key in EDITABLE_KEYS
        }
        path = save_env(self.app.root, new_env)

        # Blank masked field means "keep stored" so a secret is never wiped by
        # editing the other fields.
        secret_updates: dict[str, str] = {}
        for key in ENV_SECRET_KEYS + LANGFUSE_KEYS:
            value = self._secret_input(key)
            if key in MASKED_KEYS and value == "":
                continue
            secret_updates[key] = value
        if secret_updates:
            save_env_secrets(self.app.root, secret_updates)

        # A runtime (mcp-config.json) change does NOT reach an already-connected
        # AI tool — its config was baked in at connect time. Find the connected
        # tools now on stale config so we can steer the user to reconnect them.
        new_fp = self._block_fingerprint()
        env_changed = old_fp is not None and new_fp is not None and old_fp != new_fp
        stale = mcp_clients.stale_connected_ids(new_fp) if env_changed and new_fp else []

        # The backend reads .env only at startup, so a changed secret is not live
        # until it restarts. Flag it explicitly rather than letting the user
        # wonder why nothing changed (and never point them at the file by hand —
        # this screen is the only place .env should be edited).
        secret_changed = any(value != stored.get(key, "") for key, value in secret_updates.items())
        if secret_changed:
            self.notify(
                f"Saved to {path}. Requires restart of the backend to take effect.",
                severity="warning",
            )
        elif not stale:
            self.notify(f"Saved settings to {path}", severity="information")

        self.app.pop_screen()
        if stale:
            # Warn and route to the connect screen so the affected tools (shown
            # orange there) can be reconnected. If we returned onto a connect
            # screen (Settings was opened from it), it re-populates itself — don't
            # stack a second one.
            self.notify(
                f"{len(stale)} connected AI tool(s) still use the old config — "
                "reconnect them below.",
                severity="warning",
            )
            if not isinstance(self.app.screen, ClientSetupScreen):
                self.app.push_screen(ClientSetupScreen())

    def action_cancel(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class SpecFlowTUI(App):
    """Top-level SpecFlow terminal app."""

    CSS = """
    #dashboard-body { padding: 0 1; }
    #sessions-title, #settings-title, #onboard-title { padding: 1 2; text-style: bold; }
    #docker-prompt, #runinit-prompt { padding: 2 3; }
    .settings-section { padding: 1 2 0 2; text-style: bold; color: $accent; }
    .settings-row { height: 3; padding: 0 2; }
    .settings-label { width: 20; content-align: left middle; }
    /* Tier rows also carry a validity marker; the input must flex (1fr) so the
       fixed-width marker stays on-screen instead of being pushed off the right. */
    .settings-row Input { width: 1fr; }
    .tier-status { width: 16; content-align: left middle; padding: 0 1; }
    #ws-stream { height: 2fr; border: round $primary; padding: 0 1; }
    #ws-stats { height: 1fr; padding: 0 1; }
    #onboard-body { height: 1fr; padding: 0 2; }
    #onboard-choice-detail { height: auto; padding: 1 0 0 0; }
    #onboard-nav { height: 3; padding: 0 2; align-horizontal: left; }
    #onboard-back, #onboard-next, #onboard-go { margin: 0 1 0 0; }
    .onboard-why { padding: 1 2; }
    .onboard-howto { padding: 0 2 0 4; color: $text-muted; }
    #onboard-log, #docker-log, #runinit-log { height: 1fr; border: round $primary; margin: 1 2; }
    #client-setup-title { padding: 1 2; text-style: bold; }
    #client-detail { padding: 1 2; color: $text-muted; }
    #client-log { height: 1fr; border: round $primary; margin: 1 2; }
    #client-tiers { height: auto; margin: 0 2 1 2; }
    .modal-panel {
        width: 64;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    .modal-title { text-style: bold; padding-bottom: 1; }
    .modal-body { padding-bottom: 1; }
    .modal-hint { color: $text-muted; text-style: dim; }
    .modal-buttons { height: 3; align: center middle; }
    #verify-choices { height: auto; margin-top: 1; }
    """

    TITLE = "SpecFlow"

    def __init__(self, root: Path, generation_id: str | None, poll_interval: int) -> None:
        super().__init__()
        self.root = root
        self.generation_id = generation_id
        self.poll_interval = poll_interval
        self._watched_generation_ids: set[str] = set()
        self._notification_trackers: dict[str, MilestoneTracker] = {}
        self._finished_generation_ids: set[str] = set()

    @property
    def backend_url(self) -> str:
        """Resolved backend URL (flag → env → mcp-config → default)."""
        return resolve_backend_config(None, None, None, self.root)[0]

    def on_mount(self) -> None:
        # Run the gating sequence in an exclusive worker so blocking subprocess
        # work inside the gate screens never freezes the render loop.
        self.run_worker(self._startup_gate(), exclusive=True)

    async def _startup_gate(self) -> None:
        # (a) Setup check — collect env + run init if not configured yet.
        if not local_env.is_setup_complete(self.root):
            if local_env.repo_root(self.root) is None:
                self.notify(
                    "Not inside a SpecFlow checkout — run `specflow init` from the repo.",
                    severity="error",
                )
                self.exit()
                return
            # An already-complete .env means there's nothing to collect — run init
            # directly (it still writes mcp-config + provisions) without the wizard.
            if onboarding.env_satisfies_requirements(load_env_secrets(self.root)):
                gate_screen: _SpecFlowScreen = RunInitScreen()
            else:
                gate_screen = OnboardingScreen()
            if not await self.push_screen_wait(gate_screen):
                self.exit()
                return

        # (b) Docker check — offer to start the stack, or quit.
        running = await asyncio.to_thread(local_env.containers_running, self.root)
        if not running:
            if not await self.push_screen_wait(StartContainersScreen()):
                self.exit()
                return
        elif not await local_env.backend_ready(self.backend_url):
            # Containers up but not ready yet — wait it out with an accurate prompt
            # (do not claim the containers aren't running; they are).
            if not await self.push_screen_wait(StartContainersScreen(containers_up=True)):
                self.exit()
                return

        # (b2) First-run client setup — connect SpecFlow's MCP server to the
        # user's AI tool instead of dropping them on the empty Sessions list.
        # Only when not resuming a run and no client has been connected yet;
        # the screen is skippable and re-openable later via `c`.
        if not self.generation_id and not mcp_clients.is_any_client_connected():
            await self.push_screen_wait(ClientSetupScreen())

        # (c) Proceed to the existing app.
        if self.generation_id:
            self.watch_generation_id(self.generation_id)
        self.call_later(self.notify_active_sessions)
        self.set_interval(self.poll_interval, self.notify_active_sessions)
        if self.generation_id:
            self.push_screen(DashboardScreen(self.generation_id))
        else:
            self.push_screen(SessionsScreen())

    def watch_generation_id(self, generation_id: str | None) -> None:
        if generation_id and generation_id not in self._finished_generation_ids:
            self._watched_generation_ids.add(generation_id)
            self._notification_trackers.setdefault(generation_id, MilestoneTracker(generation_id))

    def process_payload(self, generation_id: str, payload: dict[str, Any] | None) -> None:
        """Run a status payload through the run's tracker and fire milestones.

        Shared by the dashboard (its own run) and the app tick (every other
        watched run) so notifications are screen-independent and each run is
        polled by exactly one of them. Finished runs are dropped so a re-poll
        cannot replay a duplicate terminal notification.
        """
        if not payload or generation_id in self._finished_generation_ids:
            return
        tracker = self._notification_trackers.setdefault(
            generation_id,
            MilestoneTracker(generation_id),
        )
        fire_milestones(tracker.process(payload))
        if (payload.get("status") or "").lower() in TERMINAL_STATUSES:
            self._finished_generation_ids.add(generation_id)
            self._watched_generation_ids.discard(generation_id)
            self._notification_trackers.pop(generation_id, None)

    def _displayed_generation_id(self) -> str | None:
        """The run currently shown on a screen that polls its own status."""
        try:
            screen = self.screen
        except Exception:
            return None
        return screen.generation_id if isinstance(screen, (DashboardScreen, WorkspaceMessagesScreen)) else None

    async def notify_active_sessions(self) -> None:
        # The visible dashboard/workspace screen polls its own run; exclude it
        # here so list-shaped and status-shaped payloads cannot interleave.
        displayed = self._displayed_generation_id()
        exclude = {displayed} if displayed else set()

        sessions_by_id: dict[str, dict[str, Any]] = {}
        if fetch_sessions is not None:
            try:
                sessions = await fetch_sessions(exclude=exclude)
            except Exception:
                sessions = []
            for session in sessions:
                generation_id = session.get("generation_id")
                if generation_id:
                    self.watch_generation_id(generation_id)
                    sessions_by_id[generation_id] = session

        for generation_id in self._watched_generation_ids - set(sessions_by_id) - exclude:
            payload = await poll_once(generation_id)
            if payload:
                sessions_by_id[generation_id] = payload

        for generation_id, payload in sessions_by_id.items():
            self.process_payload(generation_id, payload)


# ---------------------------------------------------------------------------
# Entry point + non-TTY fallback
# ---------------------------------------------------------------------------


def _plain_status(payload: dict[str, Any] | None, generation_id: str) -> str:
    """Plain-text status summary for non-interactive terminals."""
    if not payload:
        return f"{generation_id}: status unavailable (backend unreachable)."
    lines = [
        f"Generation {generation_id}",
        f"  Status:   {payload.get('status', 'unknown')}",
        f"  Phase:    {payload.get('current_phase', '—')}",
    ]
    tokens = render.tokens_summary(payload)
    if tokens:
        lines.append(f"  Usage:    {tokens}")
    for bar in render.workspace_bars(payload):
        lines.append(f"  {bar.workspace_id}  {bar.phase_label}  {bar.percent}%  {bar.phase_name}")
    return "\n".join(lines)


async def run_tui(args) -> int:
    """Resolve the session and launch the TUI (or print plain status if non-TTY)."""
    given = Path(getattr(args, "root_path", None) or Path.cwd()).resolve()
    # Locate the self-host checkout from cwd or, failing that, from this install's
    # own location — an editable `uv tool install` runs from the clone, so the
    # TUI works from any folder with no init step. `.env` / `.specflow-local` /
    # the init script all resolve against this root.
    root = local_env.resolve_repo_root(given)
    # Headless status doesn't need the checkout — fall back to the given dir so it
    # still works from any project folder that has config.
    effective_root = root or given
    set_project_root(effective_root)
    generation_id = resolve_generation_id(getattr(args, "generation_id", None), effective_root)
    poll_interval = getattr(args, "interval", None) or DEFAULT_POLL_INTERVAL

    # Non-interactive fallback: never construct the full-screen app.
    if not sys.stdout.isatty():
        if not local_env.is_setup_complete(effective_root):
            print("SpecFlow isn't set up yet. Run `specflow init` first.")
            return 1
        if not generation_id:
            print(
                "No active generation in this project. Run `specflow run-generation` to start one."
            )
            return 0
        payload = await poll_once(generation_id)
        print(_plain_status(payload, generation_id))
        return 0

    # Interactive launch requires the SpecFlow checkout: the TUI is the local
    # control surface for a self-hosted stack (onboarding runs the init script,
    # the gate starts the docker-compose stack). Outside a checkout the in-app
    # gate can only notify-and-exit, which tears the screen down before the
    # toast paints — so the user sees the app flash open and close with no
    # message. Refuse here with a visible, actionable line instead.
    if root is None:
        print(
            "`specflow tui` is the local control surface for a self-hosted "
            "SpecFlow stack, so it needs your SpecFlow checkout — none was found.\n"
            "  • Self-hosting? Run this from inside your clone, pass --root-path "
            "pointing at it, or install the CLI from the clone with "
            "`uv tool install --editable ./mcp_server`.\n"
            "  • Using a remote SpecFlow backend? The TUI doesn't apply — drive "
            "generations through the MCP tools / CLI instead.",
            file=sys.stderr,
        )
        return 1

    app = SpecFlowTUI(root=root, generation_id=generation_id, poll_interval=poll_interval)
    await app.run_async()
    return 0
