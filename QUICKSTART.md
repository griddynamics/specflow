# Quickstart: Run SpecFlow Locally

Local harness in a sandbox is the default mode. It hosts the backend with AI agents in Docker, then gives your IDE a keyless MCP config.

## 1. Setup

```bash
git clone https://github.com/griddynamics/specflow.git
cd specflow

cp .env.quickstart.example .env
# Fill the REQUIRED block at the top of .env.

# Install the specflow CLI (includes the `specflow tui` terminal UI).
uv tool install --editable ./mcp_server

# Bootstrap the local harness sandbox.
specflow init

# Now the Harness Sandbox is live — use the MCP server!
```

Prefer an interactive setup? Skip the `cp .env` / `specflow init` steps and run
`specflow tui` — on first launch it walks you through collecting the required values,
writes `.env`, and runs the bootstrap for you.

> The editable install runs from your clone, so `specflow` / `specflow tui` work from
> **any** directory afterwards — they locate this checkout automatically, no extra setup.

#### MCP Setup
When `specflow init` finishes, add the generated `.specflow-local/mcp-config.json` to your
IDE MCP settings.

MCP Setup Instructions: **[MCP_USER.md](MCP_USER.md)**

#### Start using SpecFlow
Open a project in your IDE, create a new folder for "specs" and copy there your PDFs, markdown specs, etc. 
Then ask your IDE agent:

```text
Use SpecFlow MCP to check specification completeness
in spec_dir "my_specs", and outputs dir in "results"
```

## 2. Prerequisites

- Docker + Docker Compose
- `uv` ([install](https://github.com/astral-sh/uv))
- `curl`
- Cursor, Claude Code, Claude Desktop, Copilot, or another MCP-capable IDE

## 3. Required `.env` Values

Keep first setup focused. Fill only these values before running `specflow init`:

| Variable | What to put there | Required? |
|----------|-------------------| --------- |
| `GITHUB_TOKEN` | GitHub PAT for disposable workspace repos. SpecFlow always needs `repo` + `read:user` (creates repos and resolves your GitHub login), plus `workflow` only for deploy/E2E runs. Create it with the broader Compass scopes (`repo,read:user,workflow,admin:repo_hook,user`) to reuse one token for both, or use a separate `repo,read:user` token if you prefer to keep keys separate. | Required
| `P10Y_API_KEY` | Compass/P10Y API token. See [Compass setup](docs/quickstart-compass.md). | Required
| `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` | One LLM provider key. OpenRouter by default | Required
| `GITHUB_ORG` | Name of GH org where repos will be created. If not provided, created as users repos. | Optional

#### Setup Compass P10Y
How to obtain P10Y_API_KEY:
[Compass P10Y setup](docs/quickstart-compass.md)

#### Prefer Anthropic?
Set `ANTHROPIC_API_KEY` instead of `OPENROUTER_API_KEY`.
The backend uses Anthropic automatically when it's the only provider key set
(if both are set, OpenRouter is used).

Everything else in `.env.quickstart.example` is already set for local mode or is
optional. 

## 4. What `specflow init` Does

- Verifies P10Y/Compass access.
- Starts the Docker-Compose backend stack
- Creates or reuses disposable GitHub workspace repos.
- Writes `.specflow-local/mcp-config.json` for your IDE.

Useful flags:

```bash
specflow init --reset-local-db
specflow init --provide-own-repos "repo1,repo2,repo3"
```

`specflow init` also accepts `--max-parallel-runs`, `--skip-build`, and `--dry-run`.

Each parallel run needs 1-3 workspace repos. Workspace repos are destructively reset
before generation. Use these repos only for SpecFlow.

## 5. Install the MCP

The MCP server runs inside your IDE, not in Docker.

Cursor: Settings -> MCP, paste `.specflow-local/mcp-config.json`.

Claude Code:

```bash
claude mcp add-json specflow "$(jq '.mcpServers.specflow' .specflow-local/mcp-config.json)"
```

Claude Desktop: Settings -> Developer -> Edit Config, then merge the
`mcpServers` block from `.specflow-local/mcp-config.json`.

## 6. Run a Generation

From your IDE chat:

1. `check_specification_completeness` writes `analysis/specification_completeness.md`.
2. `run_planning` writes `planning/IMPLEMENTATION_PLAN.md`.
3. `run_generation` uploads specs and plans to your local backend.
4. `check_status` shows progress.
5. `download_outputs` downloads generated code and reports.

Steps 1 and 2 run locally in the IDE and are safe to repeat. Generation uses your
local backend and workspace repos.

## Local CLI

After bootstrap, you can also drive runs from the local CLI:

```bash
specflow sessions
specflow check-status
specflow sessions --watch
```

For a live, glanceable view of a run — pipeline timeline, per-workspace progress bars,
token/cost, and in-app actions — launch the interactive terminal UI:

```bash
specflow tui
```

(The TUI is installed with the CLI in step 1. On first launch with no `.env` yet,
`specflow tui` walks you through collecting the required values, writes `.env`, and runs
the bootstrap for you; on later launches it offers to start the Docker stack if it isn't
already running.)

## Stop or Reset

Stop the local stack:

```bash
docker compose down --timeout 90
```

Reset local Firestore state and reseed:

```bash
specflow init --reset-local-db
```

## More Guides

- [Compass/P10Y setup](docs/quickstart-compass.md)
- [MCP usage](MCP_USER.md)
- [MCP API reference](docs/mcp/API_REFERENCE.md)
