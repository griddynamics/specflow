# SpecFlow MCP User Guide

Use this after the local quickstart has created `.specflow-local/mcp-config.json`.
Open-source users run SpecFlow against their own local backend.

## Local Install
See [QUICKSTART](./QUICKSTART.md) for details.

`specflow-init.sh` created a JSON snippet with MCP config.

### Cursor
Use quick button:
[![Install MCP Server](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en-US/install-mcp?name=specflow&config=eyJlbnYiOnsiQkFDS0VORF9VUkwiOiJodHRwOi8vMTI3LjAuMC4xOjgwMDAiLCJVU0VSX0VNQUlMIjoieW91ci1lbWFpbC1oZXJlIn0sImNvbW1hbmQiOiJ1dnggLS1mcm9tIGdkLXNwZWNmbG93IHNwZWNmbG93LW1jcCAtLXJlZnJlc2ggLS1uby1jYWNoZSJ9)

Or

Open Settings -> MCP and paste contents `.specflow-local/mcp-config.json`.


### Claude Code

```bash
claude mcp add-json specflow "$(jq '.mcpServers.specflow' .specflow-local/mcp-config.json)"
```

### Claude Desktop

Open Settings -> Developer -> Edit Config and merge the `mcpServers` block from
`.specflow-local/mcp-config.json`.

### VS Code Copilot or Gemini CLI

Copy the `mcpServers` block from `.specflow-local/mcp-config.json` into your
client's MCP config file.

## MCP Settings

| Variable | Required | Default | Role |
|----------|----------|---------|------|
| `BACKEND_URL` | No | `http://127.0.0.1:8000` | Override only when connecting to a non-default backend |
| `USER_EMAIL` | No | `git config user.email` | Local identity and display email |
| `WORKSPACE_COUNT` | No | `3` | Variants per run: `1`, `2`, or `3` |
| `LLM_HIGH` | No | Opus 4.6 | High-complexity model (planning, knowledge base) |
| `LLM_MEDIUM` | No | Sonnet 4.6, GPT-5.3 Codex, Haiku 4.5" | Coding agents, comma-separated|
| `LLM_LOW` | No | Haiku 4.5 | Lightweight tasks model |
| `MCP_SERVERS_ENABLED` | No | `playwright` | Optional agent MCPs forwarded to the backend (`playwright`, `figma`) |
| `LOG_LEVEL` | No | `INFO` | Python logging level for the MCP process |

Local quickstart omits `BACKEND_URL`; the MCP server defaults to the local
backend at `http://127.0.0.1:8000`.

## Local CLI

`specflow-init.sh` installs the local CLI once. Use it without `uv run`:

```bash
specflow sessions
specflow check-status
specflow sessions --watch
```

### LLM choice

**We strongly recommend state-of-the-art models like Sonnet 4.6, GPT-5.5, Opus 4.8 for best results.**
Keep optional model and agent MCP overrides out of first setup. Add them only when you need to tune behavior.
Available models: [OpenRouter](https://openrouter.ai/models) — US only, ZDR data policy.

<details>
<summary>Optional LLM configuration</summary>

Add these to the existing `env` block in your MCP configuration:

```json
"WORKSPACE_COUNT": "3",
"LLM_HIGH": "anthropic/claude-opus-4.6",
"LLM_MEDIUM": "anthropic/claude-sonnet-4.6,openai/gpt-5.4,anthropic/claude-sonnet-4.6",
"LLM_LOW": "anthropic/claude-haiku-4.5"
```

- `LLM_HIGH` — used for planning.
- `LLM_MEDIUM` — comma-separated, one per workspace (1–3); used for coding and analysis.
- `LLM_LOW` — used for quick indexing and simple operations.
</details>


## First Prompt

```text
Use SpecFlow MCP to check specification completeness
in spec_dir "my_specs", and outputs dir in "results"
```

For an existing codebase:

```text
Use SpecFlow MCP to check specification completeness
in spec_dir "my_specs", outputs dir in "results",
and source dir in "src_dir"
```

## Workflow

1. `check_specification_completeness` runs locally and writes `{outputs_dir}/analysis/specification_completeness.md`.
2. `run_planning` runs locally and writes `{outputs_dir}/planning/IMPLEMENTATION_PLAN.md`.
3. `run_generation` sends specs and plans files to your local backend and starts code generation.
4. `check_status` checks progress when you want an update.
5. `download_outputs` downloads generated code and reports.
6. `retry_generation` resumes a failed run from its last checkpoint.

Steps 1 and 2 are local and safe to repeat. `run_generation` starts the long
backend run.

## Tools

| Tool | Purpose |
|------|---------|
| `check_specification_completeness` | Local spec analysis template; agent writes the analysis file. |
| `run_planning` | Local implementation plan template; agent writes planning files. |
| `read_document` | Local extraction for PDFs, DOCX, PPTX, XLSX, and CSV inputs. |
| `run_generation` | Backend upload, validation, and parallel code generation. |
| `check_status` | Progress check. |
| `retry_generation` | Retry a failed generation. |
| `download_outputs` | Download generated outputs. |

Full parameters and rejection codes: [docs/mcp/API_REFERENCE.md](docs/mcp/API_REFERENCE.md).

## Session File

The MCP stores the active run in `specflow_session.json` in your project root.
To start fresh, remove this file.

It is git-ignored and lets Cursor, Claude Code, or another MCP client recover
the same run after restart.

## GD Employees: Managed Service

Managed SpecFlow is internal to Grid Dynamics. If you are a GD employee with
access, configure your MCP client with the managed backend URL, your API key,
and your email:

```bash
claude mcp add specflow \
  -e BACKEND_URL="https://<url>" \
  -e SPECFLOW_API_KEY="specflow_xxxxx..." \
  -e USER_EMAIL="you@example.com" \
  -- uvx --from specflow specflow-mcp --refresh --no-cache
```

Open-source users cannot connect to this service. Use
[QUICKSTART.md](QUICKSTART.md) instead.
