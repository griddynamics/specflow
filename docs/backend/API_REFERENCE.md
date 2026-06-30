# Backend API Reference

REST API for the SpecFlow backend (`backend/app/api/v1/`). Routers are mounted under **`/api/v1`** (see `app.main`). Interactive docs: **`/docs`** (Swagger), **`/redoc`**, OpenAPI JSON at **`/api/v1/openapi.json`**.

**PR255:** Spec analysis and planning are **not** backend endpoints. The MCP flow is: local IDE artifacts → `POST /workspace/sync` → `POST /generation-sessions/run`.

## Table of Contents

1. [Base URL](#base-url)
2. [Authentication](#authentication)
3. [Endpoint index](#endpoint-index)
4. [Auth](#auth)
5. [Generation sessions](#generation-sessions)
6. [Workspace](#workspace)
7. [Non-versioned routes (health)](#non-versioned-routes-health)
8. [Errors](#errors)
9. [Examples](#examples)

---

## Base URL

| Environment | URL |
|-------------|-----|
| Local | `http://localhost:8000` |
| Self-hosted | Your deployment URL |

**API prefix:** `/api/v1` for all routers below unless noted.

---

## Authentication

Most routes require:

| Header | Required | Purpose |
|--------|----------|---------|
| `X-API-Key` | Yes (except public health/docs) | API key |
| `X-User-Email` | Yes (for authenticated routes) | Must match the key’s `user_id` (case-insensitive) |

Alternatively: `Authorization: Bearer <api_key>`.

**Admin-only** routes use `require_admin` (see each endpoint): typically the caller’s key must include admin permission.

Public (no API key): `/health`, `/health/live`, `/health/ready`, `/docs`, `/redoc`, `/openapi.json` (see `AuthMiddleware.PUBLIC_PATHS` in `backend/app/middleware/auth.py`). The app sets `openapi_url` to `/api/v1/openapi.json`; if that path is not in the public set, use an API key when fetching the schema programmatically.

---

## Endpoint index

| Method | Path | Summary |
|--------|------|---------|
| **Auth** | | |
| GET | `/api/v1/auth/me` | Session recovery: `current_process`, lock flags, `key_uid`, `workspace_pool` |
| PUT | `/api/v1/auth/github-token` | Store encrypted GitHub PAT for the caller’s key (admin optional `target_key_uid`) |
| POST | `/api/v1/auth/keys` | Create API key (**admin**) |
| GET | `/api/v1/auth/keys` | List API keys (**admin**) |
| DELETE | `/api/v1/auth/keys/{api_key_prefix}` | Revoke key by prefix (**admin**) |
| POST | `/api/v1/auth/keys/{api_key_prefix}/reactivate` | Reactivate revoked key (**admin**) |
| **Generation sessions** | | |
| GET | `/api/v1/generation-sessions/{generation_id}` | Full generation record |
| GET | `/api/v1/generation-sessions/{generation_id}/status` | Lightweight status + checkpoint + usage |
| POST | `/api/v1/generation-sessions/run` | Start full pipeline (async background workflow) |
| POST | `/api/v1/generation-sessions/{generation_id}/retry` | Retry **failed** generation |
| DELETE | `/api/v1/generation-sessions/{generation_id}` | Cancel (marks failed) |
| POST | `/api/v1/generation-sessions/{generation_id}/resend-email` | Resend completion email; optional P10Y recalc |
| GET | `/api/v1/generation-sessions/{generation_id}/outputs` | Download outputs tarball (`tar.gz`) |
| **Workspace** | | |
| GET | `/api/v1/workspace/pool/status` | Pool counts |
| POST | `/api/v1/workspace/sync` | Upload `tar.gz` + JSON `params`; allocate/reuse workspaces; create/reuse session |
| POST | `/api/v1/workspace/force-deallocate` | Batch force-deallocate (**admin**) |
| GET | `/api/v1/workspace/cleanup/check` | Inspect available workspaces for stale disk/git |
| POST | `/api/v1/workspace/cleanup/clean` | Clean one workspace (**admin**) |
| POST | `/api/v1/workspace/{workspace_id}/force-release` | Operator force-release (**admin**) |

**Removed in PR255:** `POST /api/v1/specification/analyze`, `POST /api/v1/generation/plan`, `POST /api/v1/generation/run`.

---

## Auth

### GET `/api/v1/auth/me`

Returns MCP session recovery fields for the authenticated key: `current_process` (generation id or `null`), `in_progress`, `user_id`, `key_uid`, `workspace_pool`. Clears stale `current_process` if generation is missing, completed, or owned by another key.

### PUT `/api/v1/auth/github-token`

JSON body: `token` (GitHub PAT), optional `git_user_name`, optional `target_key_uid` (admin only — set PAT for another key by uid).

### POST `/api/v1/auth/keys` (admin)

JSON body (`APIKeyCreate`): `user_id`, `user_name`, optional `workspace_pool`, optional `expires_days`, `permissions` (default user).

Response includes `api_key`, `key_uid`, `workspace_pool`, timestamps.

### GET `/api/v1/auth/keys` (admin)

Lists keys with masked prefix and metadata.

### DELETE `/api/v1/auth/keys/{api_key_prefix}` (admin)

Revoke by first characters of the key (prefix match).

### POST `/api/v1/auth/keys/{api_key_prefix}/reactivate` (admin)

Reactivate a revoked key.

---

## Generation sessions

Ownership: routes use generation-session owner checks — session must belong to the caller’s `key_uid` / `workspace_pool`.

### GET `/api/v1/generation-sessions/{generation_id}`

Full document: `status`, `workspace_ids`, `parameters`, `progress`, `result` (when present), `error`, `retry_count`, `max_retries`, timestamps.

### GET `/api/v1/generation-sessions/{generation_id}/status`

Polling endpoint. Includes `checkpoint`, `current_phase`, `progress`, `retry_count`, `error`, `workspace_count`, token usage fields. When `status` is `completed`, also returns `result`, `artifact_path`, `code_archived`.

**Checkpoints (post–PR255):** `files_uploaded` → `contract_validated` → `kb_init_done` → `generation_started` → `generation_done` → `deploy_and_e2e_done` → `outputs_archived` → `estimation_done`.

### POST `/api/v1/generation-sessions/run`

**Content-Type:** `multipart/form-data`

| Field | Notes |
|-------|--------|
| `spec_path` | Required — path inside primary workspace after sync |
| `outputs_dir` | Default `specflow` in API; MCP typically sends `docs` |
| `src_dir` | Default `src` |
| `notification_email` | Optional; else `X-User-Email` |
| `session_id` | Optional |
| `max_retries` | Default 3 |
| `generation_id` | **Required for MCP flow** — from prior `POST /workspace/sync` |
| `LLM_HIGH`, `LLM_MEDIUM`, `LLM_LOW` | Optional model overrides |
| `MCP_SERVERS_ENABLED` | Optional; stored/pruned values win at run (`prefer_stored=True`) |
| `workspace_count` | Optional 1–3 (frozen on session after first sync when set) |

**Preconditions:** Session exists; `workspace_ids` allocated and still valid; caller owns session. Work runs in a background task.

**Contract rejection:** Validator failure invokes `reject_contract()` → session returns to **`pending`** (not `failed`). User fixes local files and re-runs MCP `run_generation` (sync + run), not `retry`.

Returns **201** with `generation_id`, `status`, `message`.

### POST `/api/v1/generation-sessions/{generation_id}/retry`

Retries a **`failed`** generation (same workspace IDs when code not archived). Contract rejections are **not** retried via this endpoint — use a fresh `run_generation` after fixing files.

### DELETE `/api/v1/generation-sessions/{generation_id}`

Cancels if not already completed/failed; marks generation failed with cancellation message.

### POST `/api/v1/generation-sessions/{generation_id}/resend-email`

**Content-Type:** `multipart/form-data`

| Field | Notes |
|-------|--------|
| `recipient_email` | Optional |
| `recalculate_p10y` | Optional boolean (default false). Re-runs P10Y on **completed** sessions when workspaces still valid. |

### GET `/api/v1/generation-sessions/{generation_id}/outputs`

Returns **`application/gzip`** tarball. May include `X-SpecFlow-Partial-Output: true` for emergency archives. See route docstring for 400/404 cases.

---

## Workspace

### GET `/api/v1/workspace/pool/status`

Aggregate pool statistics. Response model: `WorkspacePoolStatusResponse`.

### POST `/api/v1/workspace/sync`

**Multipart:** `archive` (`.tar.gz` / `.tgz`, max 100MB), `params` (JSON — `generation_id`, paths, `workspace_count`, MCP/LLM fields), `sync_to_all` (`"true"` / `"false"`), optional `user_id`.

**New session:** Creates `pending` generation (if API key not at capacity), allocates workspace set, extracts archive (MCP uses `sync_to_all=false` → primary workspace only).

**Reuse `generation_id` in `params`:**
- Allowed only while generation **`status` is `pending`** (e.g. before first successful run, or after **contract reject**).
- Returns **409** if status is `running`, `failed`, `completed`, or `initializing`.
- `ensure_workspaces_for_sync`: reuses workspaces still `allocated` and `locked_by` this session; otherwise allocates a fresh set (no cross-session workspace theft).

### POST `/api/v1/workspace/force-deallocate` (admin)

JSON: `workspace_ids`, `reason`.

### GET `/api/v1/workspace/cleanup/check`

Returns which available workspaces need cleaning.

### POST `/api/v1/workspace/cleanup/clean` (admin)

JSON: `workspace_id`, `reason`.

### POST `/api/v1/workspace/{workspace_id}/force-release` (admin)

JSON: `reason`, `confirmed_by` — operator escape hatch for ALLOCATED workspaces.

---

## Non-versioned routes (health)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Healthy if startup validation complete |
| GET | `/status` | Yes | Startup validator checks |
| GET | `/health/live` | No | Liveness |
| GET | `/health/ready` | No | Readiness |
| GET | `/` | No | Welcome message |

---

## Errors

FastAPI typically returns:

```json
{ "detail": "Human-readable message" }
```

Common codes: **400** bad input, **401** missing/invalid key, **403** admin/ownership, **404** not found, **409** conflict (sync reuse, at-capacity), **413** payload too large, **500** server, **503** not ready.

---

## Examples

### cURL: sync → run (MCP flow)

```bash
export API_KEY="gain_..."
export EMAIL="user@company.com"

# 1) Upload project (specs + src + docs/analysis + docs/planning in tar)
curl -s -X POST "http://localhost:8000/api/v1/workspace/sync" \
  -H "X-API-Key: $API_KEY" \
  -H "X-User-Email: $EMAIL" \
  -F 'archive=@project.tar.gz' \
  -F 'params={"spec_path":"specs","outputs_dir":"docs","workspace_count":3}' \
  -F 'sync_to_all=false'

# 2) Start generation (use generation_id from sync response)
curl -s -X POST "http://localhost:8000/api/v1/generation-sessions/run" \
  -H "X-API-Key: $API_KEY" \
  -H "X-User-Email: $EMAIL" \
  -F "generation_id=est-..." \
  -F "spec_path=specs" \
  -F "outputs_dir=docs" \
  -F "src_dir=src"

# 3) Poll status
curl -s "http://localhost:8000/api/v1/generation-sessions/est-.../status" \
  -H "X-API-Key: $API_KEY" \
  -H "X-User-Email: $EMAIL"
```

### Results

Use **`GET /generation-sessions/{id}`** or **`GET .../status`** — when completed, `result` includes P10Y summary, workspace estimations, and comparative analysis.

---

## Next steps

- **OpenAPI:** `/docs` on the running server
- **MCP tools:** `docs/mcp/API_REFERENCE.md`
- **State diagrams:** `docs/state-management/state-transition-diagrams.md`
- **Checkpoints:** `docs/backend/checkpoint_system.md`
- **Rejection catalog:** `CLAUDE.md`
