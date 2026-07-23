# Backend runtime & agent isolation (`BACKEND_RUNTIME`)

`BACKEND_RUNTIME` selects **where the backend service runs** and, as a direct
consequence, **what isolates the code-generation agents from the host machine**.
It is decoupled from everything else — it changes only the launch path and the
agent protection layer, not any workflow, state-machine, or estimation logic.

| Value | Backend runs as | Agent isolation boundary |
|-------|-----------------|--------------------------|
| `docker` (default) | A container (`docker compose up`) | The **container** — unchanged behaviour |
| `process` | A bare-metal `uvicorn` process on the host | The **OS-level Bash sandbox** (bubblewrap on Linux, Seatbelt on macOS) engaged per agent query |

Supported host OSes for `process` mode: **macOS and Linux**. On Windows, use
`docker`.

## Selecting the runtime

Which runtime to launch is a **local-launcher** decision, not an MCP-server
setting (the MCP server only calls `backend_url` and is indifferent to how the
backend is launched). It is resolved with this precedence:

1. explicit CLI flag,
2. `BACKEND_RUNTIME` environment variable (must be exported — a value sitting only
   in `.env` is **not** read by the gate),
3. the launcher's saved choice at `.specflow-local/backend-runtime`,
4. default `docker`.

The TUI drives (3): on first launch, when the runtime isn't pinned by (1)/(2)
**and** nothing is already running (no containers, no bare process), it shows a
one-time chooser and writes the pick to `.specflow-local/backend-runtime`. If
something is already up, it infers the runtime from that instead of asking. This
file lives beside the process pidfile/log — deliberately **not** in
`mcp-config.json`.

## Why the agent sandbox matters in process mode

In `docker` mode the container is the only OS-level boundary around the agents.
Every other control is in-process and, by design, bypassable:

- the Bash allowlist (`backend/app/core/tool_usage.py`),
- workspace path scoping via `cwd` + `Read/Write/Edit({workspace}/**)`,
- the PreToolUse regex guard (`backend/app/services/agent_hooks.py`),
- credential-name redaction (`backend/app/agents_sandboxing/claude_env_vars.py`).

`agent_hooks.py` says so explicitly: "`python script.py` is not caught … the real
boundary is the sandbox." Removing Docker removes that boundary, so `process`
mode substitutes an OS-enforced one.

## How the substitute boundary works

Claude Code's built-in Bash sandbox is enabled per agent query via
`ClaudeAgentOptions.sandbox` (see `backend/app/agents_sandboxing/os_sandbox.py`,
`get_agent_sandbox_settings`). It:

- confines each agent's **Bash subprocesses and their children** to the query
  working directory (already the workspace) + the session temp dir, at the OS
  level (bubblewrap namespaces on Linux, Seatbelt on macOS). SpecFlow redirects
  tool caches (npm/pip/go/…) to `{WORKSPACE_BASE_PATH}/caches/…`, which is outside
  `cwd`, so `get_agent_sandbox_write_allowlist` grants that subtree write access
  via `Edit`/`Write` rules (the SDK routes sandbox filesystem writes through
  permission rules — `SandboxSettings` has no `filesystem` field) — otherwise
  `npm install` / `pip install` would be denied;
- restricts outbound network to an **allow-only** domain list
  (`DEFAULT_AGENT_SANDBOX_ALLOWED_DOMAINS`: package registries + the git host;
  override with `AGENT_SANDBOX_ALLOWED_DOMAINS`, comma-separated);
- runs **fail closed** — `allowUnsandboxedCommands=False`, so a command that
  cannot be sandboxed fails rather than silently running on the bare host.

This is an **added** OS-enforced layer on top of the existing in-process controls
(defense in depth), engaged only when `BACKEND_RUNTIME=process`. In `docker` mode
`get_agent_sandbox_settings()` returns `None` and nothing changes.

> The LLM API is intentionally **not** in the network allowlist: the Claude CLI
> process itself runs outside the Bash sandbox, so model connectivity is
> unaffected. The list only governs what agent shell commands (npm/pip/go/git…)
> may reach.

## Fail-closed gates

Because a 2–8h run cannot prompt the user mid-flight, the sandbox is preflighted
at two points (mirroring the `MODEL_UNAVAILABLE` two-gate pattern):

1. **TUI** (`StartBackendProcessScreen`) — refuses before even starting the
   backend, with an actionable install message.
2. **Backend `run_generation` entrance** — the authoritative gate
   (`check_agent_sandbox_available`). On failure it returns a short rejection
   (`code: SANDBOX_UNAVAILABLE`) and starts nothing. Like other entrance
   rejections this is **not** a state-machine `fail()`: no `failed_at`, workspaces
   stay allocated — install the dependency and call `run_generation` again.

## Stopping the backend (process mode)

Unlike docker mode (where the container stack is the lifecycle boundary), a
process-mode backend is a **detached host process that outlives the TUI** — it
keeps running after you quit the TUI, along with any in-flight generation. To
stop it:

- **From the TUI** — press `k` (*stop backend*) on the dashboard or sessions
  screen. The binding is shown **only in process mode**. It confirms first,
  naming how many generations are in flight (a stop interrupts them). Per the
  STEEL COMMANDMENTS a stop never releases workspaces: the generated code is
  preserved and `retry_generation` resumes from the last checkpoint. On success
  the TUI exits; relaunching re-runs the startup gate, which detects the backend
  is down and offers to start it again.
- **From the shell** — `make stop-process` (SIGTERMs the process group and
  clears the pidfile). Equivalent to what the TUI's `k` does.

## Switching runtime from the TUI

Press `R` (*switch runtime*) on the **sessions** screen to move docker↔process
without leaving the app. (It lives on the sessions overview, not the
single-generation dashboard, because it cancels *all* in-flight runs and restarts
the backend.) The switch:

1. **Preflights the target first** (before touching the running backend): a
   fail-closed sandbox check for →process, a `docker` CLI check for →docker.
   If the target can't run, it refuses and leaves the current backend untouched.
2. **Confirms**, naming how many in-flight generations will be cancelled.
3. **Cancels those generations on the current backend** (they are marked
   `CANCELLED`; workspaces and generated code are preserved per the STEEL
   COMMANDMENTS) — this must happen before teardown, while the API is still up.
4. **Tears down** the current backend (`stop_backend_process` / `docker compose
   down`), **persists** the new choice to `.specflow-local/backend-runtime`, and
   **starts** the target backend, waiting for it to become healthy.

Because both runtimes bind the same host:port and (in the default `sqlite` setup)
share `~/.specflow/db/specflow.db`, the cancelled runs remain visible after the
switch and the TUI keeps polling the new backend automatically.

## Dependencies

- **macOS**: nothing to install — `sandbox-exec` (Seatbelt) ships with the OS.
- **Linux**: `bubblewrap` and `socat`:
  - Debian/Ubuntu: `sudo apt-get install bubblewrap socat`
  - Fedora: `sudo dnf install bubblewrap socat`
  - Ubuntu 24.04+: the default AppArmor policy blocks unprivileged user
    namespaces `bwrap` needs. If
    `sysctl kernel.apparmor_restrict_unprivileged_userns` returns `1`, add an
    AppArmor profile for `/usr/bin/bwrap` (see the Claude Code sandboxing docs)
    and reload AppArmor.

**Licenses** (all separate binaries invoked out-of-process, not linked into
SpecFlow): bubblewrap — LGPL-2.1; socat — GPL-2.0; the Claude Agent SDK that
drives them — Apache-2.0; Seatbelt is a macOS system facility.

## Residual risks (inherent to the mechanism)

- The Bash sandbox confines **Bash subprocesses only**. `Read/Edit/Write` still
  rely on the in-process path allowlist.
- Network filtering is **hostname-based, no TLS inspection**, so a broad
  `allowedDomains` entry can be a data-exfiltration path (domain fronting). Keep
  the allowlist tight.
- Do **not** enable `enableWeakerNestedSandbox` on Linux except inside an outer
  container that already isolates you — it materially weakens the sandbox.

## Alternatives considered (and why not)

- **`@anthropic-ai/sandbox-runtime` (`srt`) wrapping the whole backend**
  (Apache-2.0): too broad — the backend legitimately needs wide filesystem and
  network access (workspaces, SQLite/Firestore, LLM APIs). Documented here as an
  optional extra hardening layer for operators who want to also confine the
  backend process itself; not built in.
- **firejail** (GPL-2.0, Linux-only, setuid-root) / **raw seccomp / Landlock**
  (Linux-only, low-level): rejected — not integrated with the SDK we already use,
  and the bubblewrap path already layers seccomp (Unix-socket blocking) for us.
