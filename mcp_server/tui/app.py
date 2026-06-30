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
import sys
from pathlib import Path
from typing import Any

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
from services import local_env
from services.session import resolve_generation_id, set_project_root
from tui import actions, activity, onboarding, render
from tui.config import (
    EDITABLE_KEYS,
    ENV_SECRET_KEYS,
    MASKED_KEYS,
    load_env,
    load_env_secrets,
    save_env,
    save_env_secrets,
)
from tui.constants import DEFAULT_POLL_INTERVAL, TERMINAL_STATUSES
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
        }.get(step.state.value, "")
        body.append(f"  {step.symbol} {step.label}\n", style=style)
    return Panel(body, title="Pipeline", border_style="blue")


def _workspaces_panel(payload: dict[str, Any], selected_ws_id: str | None = None) -> Panel:
    bars = render.workspace_bars(payload)
    if not bars:
        return Panel(
            Text("Workspace progress not reported yet.", style="dim"),
            title="Workspaces",
            border_style="blue",
        )
    table = Table.grid(padding=(0, 1))
    table.add_column()  # selection marker
    table.add_column()  # ws id
    table.add_column()  # phase n/n
    table.add_column()  # phase name
    table.add_column()  # bar
    table.add_column(justify="right")  # pct
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
        )
    count = payload.get("workspace_count")
    title = f"Workspaces · {count} variants" if count else "Workspaces"
    subtitle = "↑/↓ select · o or ↵ open live stream"
    return Panel(table, title=title, subtitle=subtitle, border_style="blue")


def _estimate_panel(payload: dict[str, Any]) -> Panel | None:
    panel = render.estimate_panel(payload.get("result"))
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
    return Panel(grid, title="P10Y estimate", border_style="green")


def _activity_panel(root: Path, payload: dict[str, Any]) -> Panel | None:
    bars = render.workspace_bars(payload)
    ws_id = bars[0].workspace_id if bars else None
    lines = activity.recent_activity(root, ws_id)
    if not lines:
        return None
    body = Text("\n".join(lines), style="dim")
    title = f"Recent activity · {ws_id}" if ws_id else "Recent activity"
    return Panel(body, title=title, border_style="grey50")


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
        Binding("escape", "dismiss", "close"),
        Binding("enter", "dismiss", "close", show=False),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-panel"):
            yield Static(self._title, classes="modal-title")
            yield Static(self._body, classes="modal-body")
            yield Static("[esc] or [enter] to close", classes="modal-hint")

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


class DashboardScreen(_SpecFlowScreen):
    """Live dashboard for a single generation."""

    BINDINGS = [
        Binding("r", "retry", "retry"),
        Binding("w", "clear", "clear ws"),
        Binding("s", "settings", "settings"),
        Binding("b", "sessions", "sessions"),
        Binding("o", "open_workspace", "open ws"),
        Binding("enter", "open_workspace", "open ws", show=False),
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
        # A finished run will not change again — stop polling it.
        if payload and (payload.get("status") or "").lower() in TERMINAL_STATUSES:
            if self._poll_timer is not None:
                self._poll_timer.stop()
                self._poll_timer = None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "retry":
            status = ((self._payload or {}).get("status") or "").lower()
            return True if status == "failed" else None
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
        if ok:
            await self._run_suspended(actions.do_retry(self.app.root))

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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="ws-stream", wrap=True, markup=False, highlight=False, max_lines=2000)
            yield Static(id="ws-stats")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#ws-stream", RichLog)
        log.border_title = f"Live messages · {self._workspace_id}"
        log.write(Text("Waiting for live messages…", style="dim"))
        self.call_later(self.refresh_stats)
        self.set_interval(self.app.poll_interval, self.refresh_stats)
        self.run_worker(self._stream_worker(), exclusive=True, group="ws-stream")

    async def refresh_stats(self) -> None:
        payload = await poll_once(self._generation_id)
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


class _SessionItem(ListItem):
    """A sessions-list row carrying its generation id."""

    def __init__(self, generation_id: str, label: str) -> None:
        super().__init__(Label(label))
        self.generation_id = generation_id


def _session_label(s: dict) -> str:
    """Format a session dict as a fixed-width sessions-list label.

    Columns: status-symbol  generation-id  date  checkpoint-name
    """
    from tui.constants import CHECKPOINT_STEPS, STATUS_PILLS

    gid = s.get("generation_id", "")
    status_key = (s.get("status") or "unknown").lower()
    pill_text, _ = STATUS_PILLS.get(status_key, STATUS_PILLS["unknown"])
    # pill_text is like "✓ COMPLETED" — take the leading symbol only
    symbol = pill_text.split()[0] if pill_text else "?"

    # Human-readable checkpoint label from the steps mirror
    checkpoint_key = s.get("checkpoint", "")
    checkpoint_label = next(
        (label for key, label in CHECKPOINT_STEPS if key == checkpoint_key),
        checkpoint_key,
    )

    # Date from ISO created_at ("2026-06-30T14:23:45+00:00" → "Jun 30 14:23")
    created_at_raw = s.get("created_at", "")
    date_str = ""
    if created_at_raw:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(created_at_raw).astimezone(timezone.utc)
            date_str = dt.strftime("%b %d %H:%M")
        except ValueError:
            date_str = created_at_raw[:16]

    status_col = f"{symbol} {status_key.upper():<12}"
    date_col = f"{date_str:<14}" if date_str else " " * 14
    return f"{status_col}  {gid[:22]:<24}  {date_col}  {checkpoint_label}"


class SessionsScreen(_SpecFlowScreen):
    """Picker across all recent generation sessions (active and completed)."""

    BINDINGS = [
        Binding("r", "reload", "reload"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("SpecFlow · sessions   (↑/↓ select · ↵ open · r reload)", id="sessions-title")
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

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, _SessionItem):
            self.app.push_screen(DashboardScreen(item.generation_id))


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
        log.display = True
        repo = local_env.repo_root(self.app.root) or self.app.root
        rc = await local_env.run_init(repo, local_env.InitFlags(), on_line=log.write)
        if rc == 0:
            self.dismiss(True)
        else:
            self.notify("Setup failed — review the log and try again.", severity="error")
            self.query_one("#onboard-go", Button).disabled = False


class StartContainersScreen(_SpecFlowScreen):
    """Docker gate: offer to start the SpecFlow stack, or quit.

    ``y`` runs ``docker compose up -d`` and waits for the backend to report
    ready (streamed into a log pane), then dismisses ``True``; ``n`` dismisses
    ``False`` so the gate exits the app.
    """

    BINDINGS = [
        Binding("y", "yes", "start"),
        Binding("n", "no", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "The SpecFlow containers aren't running.\n\n" "Start them now?   [y] start    [n] quit",
            id="docker-prompt",
        )
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
                "Backend didn't become healthy — check `docker compose logs`.",
                severity="error",
            )


class SettingsScreen(Screen):
    """Editor for runtime settings (mcp-config.json) and secrets (.env)."""

    BINDINGS = [
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "cancel", "cancel"),
    ]

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
            yield Static("Secrets (.env)", classes="settings-section")
            for key in ENV_SECRET_KEYS:
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
        yield Footer()

    def action_save(self) -> None:
        new_env = {
            key: self.query_one(f"#field-{key}", Input).value.strip() for key in EDITABLE_KEYS
        }
        path = save_env(self.app.root, new_env)

        # Blank masked field means "keep stored" so a secret is never wiped by
        # editing the other fields.
        secret_updates: dict[str, str] = {}
        for key in ENV_SECRET_KEYS:
            value = self.query_one(f"#secret-{key}", Input).value.strip()
            if key in MASKED_KEYS and value == "":
                continue
            secret_updates[key] = value
        if secret_updates:
            save_env_secrets(self.app.root, secret_updates)

        self.notify(f"Saved settings to {path}", severity="information")
        self.app.pop_screen()

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
    #docker-prompt { padding: 2 3; }
    .settings-section { padding: 1 2 0 2; text-style: bold; color: $accent; }
    .settings-row { height: 3; padding: 0 2; }
    .settings-label { width: 20; content-align: left middle; }
    #ws-stream { height: 2fr; border: round $primary; padding: 0 1; }
    #ws-stats { height: 1fr; padding: 0 1; }
    #onboard-body { height: 1fr; padding: 0 2; }
    #onboard-choice-detail { height: auto; padding: 1 0 0 0; }
    #onboard-nav { height: 3; padding: 0 2; align-horizontal: left; }
    #onboard-back, #onboard-next, #onboard-go { margin: 0 1 0 0; }
    .onboard-why { padding: 1 2; }
    .onboard-howto { padding: 0 2 0 4; color: $text-muted; }
    #onboard-log, #docker-log { height: 1fr; border: round $primary; margin: 1 2; }
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
            if not await self.push_screen_wait(OnboardingScreen()):
                self.exit()
                return

        # (b) Docker check — offer to start the stack, or quit.
        running = await asyncio.to_thread(local_env.containers_running, self.root)
        if not running:
            if not await self.push_screen_wait(StartContainersScreen()):
                self.exit()
                return
        elif not await local_env.backend_ready(self.backend_url):
            # Containers up but not ready yet — reuse the gate to wait it out.
            if not await self.push_screen_wait(StartContainersScreen()):
                self.exit()
                return

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
        """The run currently shown on the dashboard, which polls it itself."""
        try:
            screen = self.screen
        except Exception:
            return None
        return screen.generation_id if isinstance(screen, DashboardScreen) else None

    async def notify_active_sessions(self) -> None:
        # The visible dashboard polls its own run; exclude it here so it is not
        # polled twice per interval.
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
