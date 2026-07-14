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
  level (bubblewrap namespaces on Linux, Seatbelt on macOS);
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
