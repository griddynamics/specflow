# LLM model validation (`MODEL_UNAVAILABLE`)

Validates that the user-configured LLM models (`LLM_HIGH` / `LLM_MEDIUM` / `LLM_LOW`)
are actually available on the active provider **before** a 2–8 hour generation run —
so a typo or unavailable model is rejected up front instead of failing an agent mid-run.

## How it works

The check always runs on the **backend** — it is the only place that knows
`DEFAULT_PROVIDER` and holds the provider API keys. The MCP server only forwards the
configured `LLM_*` values and surfaces the result.

1. **Catalog fetch** — `backend/app/services/model_catalog.py`
   - One `ProviderCatalogFetcher` per provider, each with a 1-hour cache.
   - OpenRouter delegates to `openrouter_models.fetch_available_models` (no duplicate HTTP).
   - Anthropic fetches `GET {base}/v1/models` with `x-api-key` + `anthropic-version`
     headers, following pagination.
   - **Permissive on failure:** any error or a missing key yields an empty set
     (treated as `UNVERIFIED`, never blocks).

2. **Validation** — `backend/app/services/model_validation.py` (SSOT)
   - Each configured model is compared **after** `provider.transform_model_name(...)` —
     that transformed string is what the Claude Agent SDK actually receives, so it is
     what must exist in the catalog. OpenRouter ids are `provider/model`; Anthropic ids
     are bare; the transform reconciles both.
   - Per-model status: `VALID` / `INVALID` (with a fuzzy "did you mean" suggestion) /
     `UNVERIFIED`.

3. **Policy — block-on-any-invalid:** a tier blocks the run if *any* of its models is
   confidently `INVALID`. A catalog that can't be fetched (no key / transient outage)
   yields `UNVERIFIED` and **never** blocks.

## Where it runs (two gates, same as the file-rejection contract)

- **MCP `run_generation` pre-flight** — before any upload; primary path, instant IDE
  feedback. Rejects with `MODEL_UNAVAILABLE`.
- **Backend `/run` handler** — authoritative (it holds the keys and provider); protects
  direct API callers. Returns `400` with `{"detail": {"code": "MODEL_UNAVAILABLE", ...}}`,
  which the MCP surfaces via the structured-rejection path.

A connect-time best-effort warning (`validate_models_on_connect` in
`mcp_server/services/validate_models.py`) also fires on MCP initialize; it is fully
swallowed on any failure and never blocks.

## Workspace handling

Unlike file rejections, `MODEL_UNAVAILABLE` does **not** release workspaces — it is a
config error and the synced files are fine. The gate raises before the API-key session
slot is acquired and before the background task spawns, so no workspace state changes.
The user fixes the model env and calls `run_generation` again to reuse the same
workspaces.

## Key paths

- Catalog SSOT: `backend/app/services/model_catalog.py`
- Validation SSOT: `backend/app/services/model_validation.py`
- Endpoint: `POST /api/v1/models/validate` (`backend/app/api/v1/model_validation.py`)
- MCP forwarding + gate helpers: `mcp_server/services/validate_models.py`
