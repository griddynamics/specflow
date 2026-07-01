"""MCP client registry — the single source of truth for *how* to register the
SpecFlow MCP server with each AI client (Claude Code, Gemini CLI, Cursor, …).

Mirrors the ``tui/onboarding.py`` pattern: every structure and function here is
pure and unit-testable without a terminal or any client CLI installed. The
Textual ``ClientSetupScreen`` and the ``cli.cmd_init`` registration hint are both
thin renderers over this module — so the per-client commands/paths/encodings
live in exactly one place and cannot drift.

The canonical input is always the *live* server block read from
``.specflow-local/mcp-config.json`` (``load_server_block``) so a later edit to
``USER_EMAIL`` / ``WORKSPACE_COUNT`` in Settings propagates to every client.
"""

from __future__ import annotations

import base64
import copy
import json
import shutil
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from services import local_env

# Canonical MCP server name — the key under ``mcpServers``/``servers``.
SERVER_NAME = "specflow"

# Cursor silently truncates deeplinks past this length; fall back to file/manual.
MAX_DEEPLINK_URL_LENGTH = 8000

# Global SpecFlow config — the SSOT for cross-project TUI/MCP settings. Lives in
# the user's HOME (not the project) because connecting a client is a machine-wide
# action (claude/gemini `-s user`, Cursor `~/.cursor/mcp.json`) and the TUI must
# reach it from any project. Future global settings get their own top-level key.
CONFIG_DIRNAME = ".specflow"
CONFIG_FILENAME = "config.json"


# ---------------------------------------------------------------------------
# Enums describing the axes along which clients differ
# ---------------------------------------------------------------------------


class AddStrategy(Enum):
    """How a client is registered. The screen dispatches on this; nothing else."""

    CLI_JSON = "cli_json"  # `claude mcp add-json <name> '<json>'`
    CLI_FLAGS = "cli_flags"  # `gemini mcp add <name> <cmd> <args> -e K=V`
    DEEPLINK = "deeplink"  # open cursor://… (file-merge fallback)
    MANUAL_COPY = "manual_copy"  # universal: show JSON + path + clipboard


class Collision(Enum):
    """What a client does when the server name already exists."""

    ERROR_THEN_REMOVE = "error_then_remove"  # Claude: errors → remove first
    SILENT_OVERWRITE = "silent_overwrite"  # Gemini: re-add overwrites
    NONE = "none"


class ConfigShape(Enum):
    """Top-level key a client nests servers under (VS Code is the odd one out)."""

    MCP_SERVERS = "mcpServers"
    SERVERS = "servers"


class JsonForm(Enum):
    """Which JSON shape a consumer needs the server block rendered into."""

    INNER = "inner"  # {command, args, env} — deeplink / nested under a name
    WITH_TYPE_STDIO = "with_type_stdio"  # {type:stdio, command, args, env} — claude add-json
    FLAT_WITH_NAME = "flat_with_name"  # {name, command, args, env} — vscode --add-mcp


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerBlock:
    """The launch spec for the SpecFlow MCP server, read from mcp-config.json."""

    command: str
    args: tuple[str, ...]
    env: dict[str, str]


@dataclass(frozen=True)
class FileTarget:
    """A JSON config file to merge the server block into (deeplink fallback)."""

    path_template: str  # ``~``-relative; expanded at use
    key: ConfigShape
    platforms: tuple[str, ...] = ("darwin", "linux", "win32")

    def resolved_path(self, home: Path | None = None) -> Path:
        return Path(self.path_template.replace("~", str(home or Path.home()), 1))


@dataclass(frozen=True)
class McpClient:
    """One registry entry. Declarative: builders/detection read these fields."""

    client_id: str
    name: str
    icon: str
    strategy: AddStrategy
    detect: tuple[str, ...] = ()  # binaries probed via ``shutil.which``
    detect_paths: tuple[str, ...] = ()  # ``~``-relative dirs that also prove install
    collision: Collision = Collision.NONE
    config_shape: ConfigShape = ConfigShape.MCP_SERVERS
    file_target: FileTarget | None = None
    deeplink_template: str = ""  # contains ``{name}`` and ``{config}``
    verify_argv_template: tuple[str, ...] = ()  # ``{name}`` substituted; () = no verify
    caveat: str = ""  # standing gotcha shown after add (e.g. Gemini trust)
    restart_hint: str = ""  # what the user must do for the client to load it
    description: str = ""  # one-line, plain-language "what connect will do"

    @property
    def can_verify(self) -> bool:
        return bool(self.verify_argv_template)


# ---------------------------------------------------------------------------
# The registry (v1: Claude Code, Gemini CLI, Cursor, + universal copy)
# ---------------------------------------------------------------------------

CLAUDE_CODE = McpClient(
    client_id="claude_code",
    name="Claude Code",
    icon="◆",
    strategy=AddStrategy.CLI_JSON,
    detect=("claude",),
    collision=Collision.ERROR_THEN_REMOVE,
    verify_argv_template=("claude", "mcp", "get", "{name}"),
    restart_hint="Reload your Claude Code session (or the /mcp panel) to pick it up.",
    description="Register SpecFlow with Claude Code and verify it connected.",
)

GEMINI_CLI = McpClient(
    client_id="gemini",
    name="Gemini CLI",
    icon="✦",
    strategy=AddStrategy.CLI_FLAGS,
    detect=("gemini",),
    collision=Collision.SILENT_OVERWRITE,
    caveat="Gemini ignores MCP servers in untrusted folders — if it shows as "
    "disabled, trust this folder in Gemini and re-check.",
    restart_hint="Restart your Gemini session to load the new server.",
    description="Register SpecFlow with the Gemini CLI (trust this folder if it shows disabled).",
)

CURSOR = McpClient(
    client_id="cursor",
    name="Cursor",
    icon="▣",
    strategy=AddStrategy.DEEPLINK,
    detect=("cursor",),
    detect_paths=("~/.cursor",),
    config_shape=ConfigShape.MCP_SERVERS,
    file_target=FileTarget("~/.cursor/mcp.json", ConfigShape.MCP_SERVERS),
    deeplink_template="cursor://anysphere.cursor-deeplink/mcp/install?name={name}&config={config}",
    restart_hint="Approve it in Cursor, then check Settings → MCP shows specflow.",
    description="Add SpecFlow to Cursor's config and open its quick-install. "
    "Cursor has no read-back, so you'll confirm it in Cursor.",
)

MANUAL = McpClient(
    client_id="manual",
    name="Other / copy config",
    icon="⧉",
    strategy=AddStrategy.MANUAL_COPY,
    config_shape=ConfigShape.MCP_SERVERS,
    description="Show the config to copy-paste into any MCP client.",
)

REGISTRY: tuple[McpClient, ...] = (CLAUDE_CODE, GEMINI_CLI, CURSOR, MANUAL)


# ---------------------------------------------------------------------------
# Load-time consistency guard — a malformed entry fails at import, not runtime.
# ---------------------------------------------------------------------------


def _check_registry(registry: tuple[McpClient, ...]) -> None:
    ids = [c.client_id for c in registry]
    assert len(ids) == len(set(ids)), f"duplicate client_id in REGISTRY: {ids}"
    for c in registry:
        if c.strategy in (AddStrategy.CLI_JSON, AddStrategy.CLI_FLAGS):
            assert c.detect, f"{c.client_id}: CLI strategy needs a detect binary"
        if c.strategy is AddStrategy.DEEPLINK:
            assert "{config}" in c.deeplink_template, f"{c.client_id}: deeplink needs {{config}}"
            assert c.file_target is not None, f"{c.client_id}: deeplink needs a file fallback"
        if c.collision is Collision.ERROR_THEN_REMOVE:
            assert c.strategy is AddStrategy.CLI_JSON, (
                f"{c.client_id}: remove-then-add only applies to a CLI add"
            )


_check_registry(REGISTRY)


# ---------------------------------------------------------------------------
# Reading the live server block
# ---------------------------------------------------------------------------


def server_block(mcp_config: dict, name: str = SERVER_NAME) -> ServerBlock:
    """Extract the ``ServerBlock`` from a parsed ``mcp-config.json`` dict.

    Raises ``KeyError`` with an actionable message if the block is absent — the
    screen guards on setup-complete before ever calling this.
    """
    try:
        entry = mcp_config["mcpServers"][name]
        return ServerBlock(
            command=entry["command"],
            args=tuple(entry.get("args", [])),
            env=dict(entry.get("env", {})),
        )
    except (KeyError, TypeError) as exc:
        raise KeyError(
            f"No '{name}' server under mcpServers in mcp-config.json — run setup first."
        ) from exc


def load_server_block(root: Path, name: str = SERVER_NAME) -> ServerBlock:
    """Read and parse the live ``mcp-config.json`` from ``root`` into a block."""
    data = json.loads(local_env.mcp_config_path(root).read_text())
    return server_block(data, name)


# ---------------------------------------------------------------------------
# Pure rendering of the block into the various JSON shapes
# ---------------------------------------------------------------------------


def _inner_dict(block: ServerBlock, *, with_type: bool = False) -> dict:
    inner: dict = {}
    if with_type:
        inner["type"] = "stdio"
    inner["command"] = block.command
    inner["args"] = list(block.args)
    if block.env:
        inner["env"] = dict(block.env)
    return inner


def render_json(block: ServerBlock, form: JsonForm, name: str = SERVER_NAME) -> str:
    """Render the block into a client-specific JSON string (compact)."""
    if form is JsonForm.INNER:
        obj: dict = _inner_dict(block)
    elif form is JsonForm.WITH_TYPE_STDIO:
        obj = _inner_dict(block, with_type=True)
    elif form is JsonForm.FLAT_WITH_NAME:
        obj = {"name": name, **_inner_dict(block)}
    else:  # pragma: no cover - exhaustive
        raise ValueError(form)
    return json.dumps(obj, separators=(",", ":"))


def render_config_file(
    block: ServerBlock, shape: ConfigShape, name: str = SERVER_NAME
) -> str:
    """Pretty full-file JSON (``{shape: {name: inner}}``) for manual copy/paste."""
    with_type = shape is ConfigShape.SERVERS  # VS Code's `servers` wants type:stdio
    return json.dumps(
        {shape.value: {name: _inner_dict(block, with_type=with_type)}}, indent=2
    )


def build_deeplink(block: ServerBlock, client: McpClient, name: str = SERVER_NAME) -> str:
    """Build a Cursor-style install deeplink.

    ``config`` is standard base64 of the *inner* block (no name) then
    ``encodeURIComponent``-equivalent quoting — a raw ``+`` from base64 would
    otherwise decode to a space and break Cursor's JSON parse.
    """
    raw = json.dumps(_inner_dict(block), separators=(",", ":"))
    b64 = base64.b64encode(raw.encode()).decode()
    return client.deeplink_template.format(
        name=urllib.parse.quote(name, safe=""),
        config=urllib.parse.quote(b64, safe=""),
    )


def deeplink_too_long(url: str) -> bool:
    """True if the deeplink exceeds Cursor's cap and must fall back to file/manual."""
    return len(url) >= MAX_DEEPLINK_URL_LENGTH


# ---------------------------------------------------------------------------
# Building add / remove / verify argv (dispatch on strategy — pure)
# ---------------------------------------------------------------------------


def build_add_argv(client: McpClient, block: ServerBlock, name: str = SERVER_NAME) -> list[str]:
    """The exact non-interactive command to register the server with ``client``."""
    if client.strategy is AddStrategy.CLI_JSON:
        return ["claude", "mcp", "add-json", name,
                render_json(block, JsonForm.WITH_TYPE_STDIO, name), "-s", "user"]
    if client.strategy is AddStrategy.CLI_FLAGS:
        argv = ["gemini", "mcp", "add", name, block.command, *block.args]
        for key, value in block.env.items():
            argv += ["-e", f"{key}={value}"]
        return argv + ["-s", "user"]
    raise ValueError(f"{client.client_id}: strategy {client.strategy} has no CLI add command")


def build_remove_argv(client: McpClient, name: str = SERVER_NAME) -> list[str] | None:
    """Pre-add removal for clients that error on collision (Claude); else None."""
    if client.collision is Collision.ERROR_THEN_REMOVE:
        return ["claude", "mcp", "remove", name, "-s", "user"]
    return None


def build_verify_argv(client: McpClient, name: str = SERVER_NAME) -> list[str] | None:
    """Read-back command to confirm the add, or None when unverifiable (Cursor)."""
    if not client.verify_argv_template:
        return None
    return [part.replace("{name}", name) for part in client.verify_argv_template]


def verify_passed(output: str, name: str = SERVER_NAME) -> bool:
    """A verify command succeeded iff its output names our server."""
    return name in output


# ---------------------------------------------------------------------------
# Safe config-file merge (Cursor fallback) — never clobber sibling keys
# ---------------------------------------------------------------------------


def merge_block(
    existing: dict, block: ServerBlock, shape: ConfigShape, name: str = SERVER_NAME
) -> dict:
    """Return ``existing`` with only ``<shape>.<name>`` set to our block.

    Read-modify-write that preserves every other key and every other server.
    Callers must refuse on a malformed existing file rather than pass ``{}``.
    """
    merged = copy.deepcopy(existing)
    with_type = shape is ConfigShape.SERVERS
    servers = merged.setdefault(shape.value, {})
    if not isinstance(servers, dict):
        raise ValueError(f"existing '{shape.value}' is not an object")
    servers[name] = _inner_dict(block, with_type=with_type)
    return merged


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_installed(
    client: McpClient,
    *,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
) -> bool:
    """True if ``client`` looks installed (binary on PATH or a known config dir).

    ``which``/``home`` are injectable so detection is unit-testable without the
    real environment. The MANUAL entry is always available.
    """
    if client.strategy is AddStrategy.MANUAL_COPY:
        return True
    if any(which(binary) for binary in client.detect):
        return True
    base = home or Path.home()
    return any((base / p.replace("~/", "", 1)).exists() for p in client.detect_paths)


# ---------------------------------------------------------------------------
# Screen view-model + status (pure — the Textual screen renders these)
# ---------------------------------------------------------------------------


class ClientStatus(Enum):
    """A client's state on the setup screen — drives the row label, plain text."""

    NOT_INSTALLED = "not_installed"
    NOT_CONFIGURED = "not_configured"  # installed, not connected yet
    CONNECTED = "connected"  # connected in a previous session (from the marker)
    CONNECTING = "connecting"
    VERIFIED = "verified"  # added and a read-back confirmed it
    ADDED_UNVERIFIED = "added_unverified"  # added but we can't confirm (Cursor/Gemini)
    FAILED = "failed"


_STATUS_LABELS: dict[ClientStatus, str] = {
    ClientStatus.NOT_INSTALLED: "not installed",
    ClientStatus.NOT_CONFIGURED: "press ↵ to connect",
    ClientStatus.CONNECTED: "✓ connected",
    ClientStatus.CONNECTING: "connecting…",
    ClientStatus.VERIFIED: "✓ connected & verified",
    ClientStatus.ADDED_UNVERIFIED: "added — confirm in your client",
    ClientStatus.FAILED: "failed — press ↵ to retry",
}


def status_label(status: ClientStatus) -> str:
    """Plain-language label for a status (never a bare glyph the user must decode)."""
    return _STATUS_LABELS[status]


# Green is reserved for genuinely connected states (CONNECTED / VERIFIED). Every
# other state is neutral/amber/red so a glance never mistakes "installed" or
# "press to connect" for "done".
_STATUS_STYLES: dict[ClientStatus, str] = {
    ClientStatus.NOT_INSTALLED: "dim",
    ClientStatus.NOT_CONFIGURED: "default",
    ClientStatus.CONNECTED: "green",
    ClientStatus.CONNECTING: "yellow",
    ClientStatus.VERIFIED: "bold green",
    ClientStatus.ADDED_UNVERIFIED: "yellow",
    ClientStatus.FAILED: "bold red",
}


def status_style(status: ClientStatus) -> str:
    """Rich style for a status — drives the row colour on the setup screen."""
    return _STATUS_STYLES[status]


def initial_status(
    client: McpClient, *, installed: bool, saved: ClientStatus | None = None
) -> ClientStatus:
    """Status to show before any action this session.

    A previously *saved* outcome wins (so an unverified add is shown as exactly
    that next time — never silently upgraded to "connected"); detection only
    fills in when nothing was saved.
    """
    if client.strategy is not AddStrategy.MANUAL_COPY and not installed:
        return ClientStatus.NOT_INSTALLED
    if saved is not None:
        return saved
    return ClientStatus.NOT_CONFIGURED


def status_after_add(
    client: McpClient, *, add_ok: bool, verify_output: str | None = None
) -> ClientStatus:
    """Map an add (and optional verify) outcome to an honest terminal status.

    Verifiable clients (Claude) reach VERIFIED only when the read-back names the
    server; unverifiable ones (Cursor, Gemini in v1) cap at ADDED_UNVERIFIED and
    can never show a false ✓-verified.
    """
    if not add_ok:
        return ClientStatus.FAILED
    if client.can_verify:
        if verify_output is not None and verify_passed(verify_output):
            return ClientStatus.VERIFIED
        return ClientStatus.FAILED
    return ClientStatus.ADDED_UNVERIFIED


# Verbatim next-step prompts shown after a successful connect (README parity).
USAGE_PROMPTS: tuple[str, ...] = (
    '"Use SpecFlow MCP to check specification completeness"',
    '"Create an implementation plan using SpecFlow MCP"',
    '"Run generation with SpecFlow MCP"',
)


def success_body(client: McpClient, status: ClientStatus) -> str:
    """Plain-English 'what now' text shown once a client is connected."""
    lead = (
        f"SpecFlow is registered with {client.name} and verified."
        if status is ClientStatus.VERIFIED
        else f"SpecFlow was added to {client.name}."
    )
    lines = [lead]
    if client.restart_hint:
        lines.append(f"  → {client.restart_hint}")
    if status is ClientStatus.ADDED_UNVERIFIED:
        lines.append(
            "  → We can't confirm this automatically. Once you've checked your "
            "client, reopen this screen and press ↵ to confirm it works (or report it)."
        )
    lines.append("")
    lines.append("How to use it — put your specs in  specs/  then tell your AI agent, in order:")
    lines += [f"  • {p}" for p in USAGE_PROMPTS]
    return "\n".join(lines)


@dataclass(frozen=True)
class ClientRow:
    """A row in the setup screen: a client, detection, and its saved status."""

    client: McpClient
    installed: bool
    saved: ClientStatus | None  # last persisted outcome, or None if never acted on


def client_rows(
    *,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
) -> list[ClientRow]:
    """The full list of clients with detection + saved status (pure view model)."""
    saved = saved_statuses(home=home)
    return [
        ClientRow(c, is_installed(c, which=which, home=home), saved.get(c.client_id))
        for c in REGISTRY
    ]


# ---------------------------------------------------------------------------
# Global SpecFlow config (~/.specflow/config.json)
#
# Stores the *actual* per-client status — the same statuses shown in the TUI —
# under a "clients" section, so an unverified add is remembered as unverified,
# never assumed connected. This file is the SSOT for any FUTURE global SpecFlow
# settings: add new sections as sibling top-level keys; the read/write helpers
# here preserve every key they don't own, so sections never clobber each other.
# ---------------------------------------------------------------------------

# Outcomes worth persisting (transient/detection states are recomputed live).
PERSISTABLE_STATUSES: frozenset[ClientStatus] = frozenset(
    {
        ClientStatus.VERIFIED,
        ClientStatus.CONNECTED,
        ClientStatus.ADDED_UNVERIFIED,
        ClientStatus.FAILED,
    }
)

# Statuses that mean "the user already acted on this client" — used to decide
# whether the first-run setup screen still needs to nag.
_ACTED_STATUSES: frozenset[ClientStatus] = frozenset(
    {ClientStatus.VERIFIED, ClientStatus.CONNECTED, ClientStatus.ADDED_UNVERIFIED}
)


def config_path(home: Path | None = None) -> Path:
    """Path to the global SpecFlow config (``home`` injectable for tests)."""
    return (home or Path.home()) / CONFIG_DIRNAME / CONFIG_FILENAME


def _read_config(home: Path | None = None) -> dict:
    try:
        return json.loads(config_path(home).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_config(data: dict, home: Path | None = None) -> None:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def saved_statuses(*, home: Path | None = None) -> dict[str, ClientStatus]:
    """Per-client saved statuses from the global config (unknown ids/values skipped)."""
    out: dict[str, ClientStatus] = {}
    for client_id, value in (_read_config(home).get("clients") or {}).items():
        try:
            out[client_id] = ClientStatus(value)
        except ValueError:
            continue  # drop unknown/legacy status values rather than crash
    return out


def save_status(client_id: str, status: ClientStatus, *, home: Path | None = None) -> None:
    """Persist ``status`` for ``client_id`` into the global config's ``clients`` section.

    Read-modify-write that preserves every other top-level config section, so
    future settings living in the same file are never lost.
    """
    if status not in PERSISTABLE_STATUSES:
        return
    data = _read_config(home)
    clients = data.get("clients")
    if not isinstance(clients, dict):
        clients = {}
    clients[client_id] = status.value
    data["clients"] = {cid: clients[cid] for cid in sorted(clients)}
    _write_config(data, home)


def forget_status(client_id: str, *, home: Path | None = None) -> None:
    """Drop ``client_id`` from the saved statuses (e.g. it's no longer registered)."""
    data = _read_config(home)
    clients = data.get("clients")
    if isinstance(clients, dict) and client_id in clients:
        del clients[client_id]
        data["clients"] = {cid: clients[cid] for cid in sorted(clients)}
        _write_config(data, home)


def is_any_client_connected(*, home: Path | None = None) -> bool:
    """Gate for the startup screen: skip it once the user has connected/added a client.

    A bare FAILED does not count (they still need to succeed), so they are not
    dropped on the empty Sessions list with nothing wired up.
    """
    return any(s in _ACTED_STATUSES for s in saved_statuses(home=home).values())


# ---------------------------------------------------------------------------
# CLI registration hint — derived from the registry (replaces the hardcoded
# ``cli._IDE_REGISTRATION_HINT`` so client instructions live in one place).
# ---------------------------------------------------------------------------


def _cli_hint_line(client: McpClient, config: str) -> str | None:
    label = f"  {client.name + ':':<16}"
    if client.strategy is AddStrategy.CLI_JSON:
        return f"{label}claude mcp add-json {SERVER_NAME} \"$(cat {config} | jq '.mcpServers.{SERVER_NAME}')\" -s user"
    if client.strategy is AddStrategy.CLI_FLAGS:
        return f"{label}gemini mcp add {SERVER_NAME} <command> <args> -e KEY=VALUE -s user  (or: specflow tui → press c)"
    if client.strategy is AddStrategy.DEEPLINK:
        target = client.file_target.path_template if client.file_target else "the client config"
        return f"{label}specflow tui → press c, or paste the mcpServers.{SERVER_NAME} block into {target}"
    return None  # MANUAL covered by the generic footer line


def render_cli_hint(config_path: Path | str) -> str:
    """The post-``init`` 'register the MCP server' hint, built from the registry."""
    config = str(config_path)
    lines = [f"\nRegister the MCP server in your AI client (config written to {config}):"]
    lines += [line for c in REGISTRY if (line := _cli_hint_line(c, config))]
    lines.append(f"  {'Other clients:':<16}paste the mcpServers block from {config}")
    lines.append("Tip: run `specflow tui` and press `c` for guided one-key setup.\n")
    return "\n".join(lines)
