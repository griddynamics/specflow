# SpecFlow MCP API Reference

Reference for tools and prompts exposed by the SpecFlow MCP server (`mcp_server/server.py`, FastMCP). After **PR255**, specification analysis and planning run **locally in the IDE**; the remote backend is used only from `run_generation` onward.

## Tools (8)

| Tool | Role |
|------|------|
| `check_specification_completeness` | **Local.** Returns the `specflow-analysis` instruction template. The IDE agent writes `{outputs_dir}/analysis/specification_completeness.md`. No backend sync or session. |
| `run_planning` | **Local.** Returns the `specflow-planning` instruction template. The agent writes `{outputs_dir}/planning/IMPLEMENTATION_PLAN.md` (and `e2e-test-plan.md` when Part F is `INTEGRATION_TESTS_READY`). |
| `read_document` | **Local.** Extract PDF/DOCX/PPTX/XLSX/CSV to markdown (and embedded images) for analysis/planning. |
| `run_generation` | **Backend.** MCP precheck → upload specs/`src`/`outputs_dir` → contract validation → 2–8 h codegen (+ optional deploy/E2E, P10Y). |
| `check_status` | Poll `GET /api/v1/generation-sessions/{id}/status` (read-only; user-driven only). |
| `download_outputs` | Download archived artifact tarball from a generation session into a local folder. |
| `retry_generation` | `POST .../retry` for a failed run — user-driven only. |

## MCP prompts (2)

| Prompt | Role |
|--------|------|
| `specflow-diagnose` | Diagnose errors from a SpecFlow-deployed app. |
| `specflow-compare-variants` | Compare 1–3 workspace repos and produce an assembly plan. |

Bundled skill sources live under `mcp_server/services/skills/`. Analysis and planning are invoked via the **tools** above (same names as pre-PR255); prompts are separate utilities.

## Configuration (MCP `env`)

Set in the MCP client config (e.g. `mcp.json`):

| Variable | Notes |
|----------|--------|
| `BACKEND_URL` | API base (default: `http://127.0.0.1:8000`). |
| `SPECFLOW_API_KEY` | Sent as `X-API-Key`. |
| `USER_EMAIL` | Sent as `X-User-Email`; completion email for `run_generation`. |
| `WORKSPACE_COUNT` | Parallel workspaces `1`–`3` for new `run_generation` sessions (backend default: `3`). Not a tool parameter. |
| `LLM_HIGH`, `LLM_MEDIUM`, `LLM_LOW` | Model ids for backend agents (OpenRouter-style). |
| `MCP_SERVERS_ENABLED` | Comma-separated optional **agent** MCPs (`playwright`, `figma`); keyword-pruned after upload from spec index. |
| `LOG_LEVEL` | MCP process logging. |

Figma tokens are configured on the **backend**, not in the SpecFlow MCP `env` block. The Rosetta knowledge base ships as a plugin baked into the backend image — it needs no config or credentials. See repository `README.md` and `mcp_server/services/server_instructions.py` for policy text.

**Local self-host (keyless).** When the backend runs in `AUTH_MODE=local`, omit
`SPECFLOW_API_KEY` entirely — the backend authorises requests with a fixed internal
identity. The local MCP server defaults `BACKEND_URL` to `http://127.0.0.1:8000`, so the
generated quickstart config only needs user-specific values such as `USER_EMAIL`. The MCP
client sends no `X-API-Key` header when the key is unset. See the [Local Self-Host
Quickstart](../../QUICKSTART.md).

## Session and paths

- **`specflow_session.json`** lives in the **project root** (parent of `spec_dir`), not the MCP process CWD. It stores `generation_id` and `project_root` after the first successful `run_generation`.
- **Relative paths** on tools resolve against the MCP workspace root (client `list_roots`) once known.
- **Local tools** do not create or require a `generation_id`. Only `run_generation` creates a backend session.
- **New session:** delete or clear `specflow_session.json` only after status is `completed` or `failed` (see `session_note` from `check_status`).

## Required artifacts before `run_generation`

Paths are under `<project_root>/<outputs_dir>/` (default `outputs_dir`: `docs`). The same `spec_dir`, `outputs_dir`, and `src_dir` must be passed to local tools and `run_generation`.

| File | Producer |
|------|----------|
| `analysis/specification_completeness.md` | `check_specification_completeness` (IDE agent follows template) |
| `planning/IMPLEMENTATION_PLAN.md` | `run_planning` |
| `planning/e2e-test-plan.md` | `run_planning` — **only** if analysis Part F = `INTEGRATION_TESTS_READY` |

Optional (recommended for large spec trees): `analysis/specification_index.md`, `analysis/repo_summary.md` (brownfield).

Canonical names and fuzzy matching are enforced by the backend **contract validator** after upload. See `CLAUDE.md` (File/Directory Contract) for rejection codes and messages.

## Typical workflow

1. **`check_specification_completeness`** — IDE agent follows returned `template`; writes `specification_completeness.md`. Repeat anytime specs change. **No** `check_status` polling.
2. **`run_planning`** — Agent follows template; writes plan markdown locally. Repeat to refine. **No** backend call.
3. **`run_generation`** — Call **once** when the user asks to start codegen. Fails fast locally if required files are missing; otherwise uploads and runs on the backend for hours.
4. **`check_status`** — Only when the user asks for progress (never on a timer).
5. **`download_outputs`** — When the user wants backend artifacts (post–KB-init checkpoints and later).
6. On failure, user may request **`retry_generation`**.

Do **not** call `run_generation` while a generation is already **`running`** / **`initializing`**.

---

## `check_specification_completeness`

**Behavior (PR255):** Fully local. Returns JSON with `mode: "local"`, `skill: "specflow-analysis"`, `template` (full SKILL.md text), `writes_to`, and echoed path arguments. **Does not** call the backend.

**Parameters**

| Name | Default | Description |
|------|---------|-------------|
| `spec_dir` | `"specs"` | Spec directory relative to project root. |
| `outputs_dir` | `"docs"` | Root for analysis/planning artifacts; must match `run_generation`. |
| `src_dir` | `"src"` | Existing source tree for brownfield context (optional if missing). |

**Agent obligation:** Write the report to `{outputs_dir}/analysis/specification_completeness.md` with a **Dimension Status — full inventory** listing every Part A–F dimension (not only summary counts). See bundled `specflow-analysis` SKILL.md.

---

## `run_planning`

**Behavior (PR255):** Fully local. Returns JSON with `mode: "local"`, `skill: "specflow-planning"`, `template`, `writes_to`, and path arguments.

**Parameters**

| Name | Default | Description |
|------|---------|-------------|
| `spec_dir` | `"specs"` | Spec directory (same as analysis). |
| `outputs_dir` | `"docs"` | Must match analysis and `run_generation`. |
| `src_dir` | `"src"` | Brownfield source root. |

**Requires** `{outputs_dir}/analysis/specification_completeness.md` (agent reads it while planning).

**Writes:** `{outputs_dir}/planning/IMPLEMENTATION_PLAN.md`; optionally `{outputs_dir}/planning/e2e-test-plan.md`.

---

## `read_document`

**Parameters**

| Name | Description |
|------|-------------|
| `file_path` | Absolute or project-relative path to `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xls`, `.csv`. |

**Returns:** JSON with `markdown`, optional embedded `images` (base64), and `warnings`. For standalone images, use the IDE file reader instead.

Used by local analysis/planning agents for non-text spec formats.

---

## `run_generation`

**Parameters**

| Name | Default | Description |
|------|---------|-------------|
| `spec_dir` | `"specs"` | Spec directory (must match local tools). |
| `outputs_dir` | `"docs"` | Directory containing `analysis/` and `planning/` artifacts. |
| `src_dir` | `"src"` | Existing source tree for brownfield context. |
| `generation_id` | session | Optional override; otherwise from `specflow_session.json`. Reuse continues an already-allocated session (e.g. after contract rejection). |

Parallel workspace count is set only via `WORKSPACE_COUNT` in MCP `env`, not as a tool argument.

**Gate:** MCP **precheck** (missing files, Part F / E2E rules) runs before upload. Backend **contract validator** runs after upload (normalization, markdown→JSON, MCP keyword prune). Either layer returns the same shape:

```json
{
  "error": "<human message>",
  "code": "<REJECTION_CODE>",
  "missing_files": [],
  "ambiguous": []
}
```

**Rules:** Only when the user explicitly asks to start generation. **Do not** call if status is already **`running`**. **Do not** auto-retry on timeout — the job may have started.

**Returns:** JSON with `generation_id`, `status`, `message`. Work continues on the backend for hours; email sent to `USER_EMAIL` when done.

---

## `check_status`

**Parameters**

| Name | Default | Description |
|------|---------|-------------|
| `generation_id` | session | Which generation to poll. |
| `spec_dir` | `None` | Optional absolute spec directory to restore `project_root` after MCP restart. |

**Returns:** JSON from `/api/v1/generation-sessions/{id}/status`, plus:

- **`phase`** — short label from `status` and `checkpoint`.
- **`can_run_generation`** — `true` when `status` is `pending` (session exists, no active run).
- **`session_file`**, **`session_note`** (terminal states) when project root is known.

**Checkpoints** (backend, post–PR255): `files_uploaded` → `contract_validated` → `kb_init_done` → `generation_started` → `generation_done` → `deploy_and_e2e_done` → `outputs_archived` → `estimation_done`.

There is no `can_run_planning` / `can_run_analysis` — planning and analysis are local-only.

---

## `download_outputs`

**Parameters**

| Name | Default | Description |
|------|---------|-------------|
| `generation_id` | (required) | Generation id. |
| `outputs_dir` | `"docs"` | Local directory for extracted files. |

**Returns:** Tarball extract summary, or backend JSON when nothing is archivable yet (requires checkpoint ≥ `kb_init_done` for artifacts).

---

## `retry_generation`

**Parameters**

| Name | Default | Description |
|------|---------|-------------|
| `generation_id` | session | Generation to retry. |
| `spec_dir` | `None` | Optional absolute spec directory to restore session after restart. |

**Rules:** User-initiated only; not while **`running`**.

**Returns:** JSON from the backend retry endpoint.

---

## Errors

- **Precheck / contract rejection:** Structured JSON with `error`, `code`, and optional `missing_files` / `ambiguous` — no stack traces, no Firestore jargon.
- **Paths:** Missing spec directory → precheck or validator message with canonical path and which tool to re-run.
- **Auth:** Backend `401` / `403` if API key or email headers are wrong.
- **Concurrent runs:** Second `run_generation` while one is active returns `GENERATION_ALREADY_RUNNING`.

For REST field-level detail, see **`docs/backend/API_REFERENCE.md`**. For the full rejection catalog, see **`CLAUDE.md`**. For operator setup, see **`README.md`**.
