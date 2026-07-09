# Token Economy & Model Choice Guidance

Use this guide to forecast SpecFlow spend before starting generation.

## Budget baseline

For an active user running 2–3 spec-driven generations per week, budget:

> **Approximately $800–$1,600 per month**

This assumes a mix of small, single-variant runs and larger multi-variant runs. Occasional users cost proportionally less. Replace this baseline with your own observed cost after the first 3–5 representative runs.

## Cost formula

```text
Monthly cost ≈ runs per month × cost per run
Cost per run ≈ fixed run overhead + (P × W × C)
```

| Input | Meaning | Typical planning value |
|---|---|---:|
| Runs per month | Generation cadence | 8–12 for an active user |
| `P` | Generation and deploy/E2E phases | 10–25 |
| `W` | Parallel variants (`WORKSPACE_COUNT`) | 1–3; default 3 |
| `C` | Average cost per phase-session | Start with $3–$8, then calibrate |

The fixed overhead covers work such as knowledge-base initialization. For medium and large runs, `P × W × C` is usually the dominant cost.

`P` and `W` are known before generation starts. `C` depends on model prices, token volume, cache use, retries, and the work inside each phase. SpecFlow records per-model input, output, cache-read, and cache-write tokens; use that data instead of relying indefinitely on the starting range.

## Main cost drivers

1. **Phase count (`P`)**

   `run_planning` gives the phase count before generation. More phases produce an approximately linear increase in generation cost. A plan with an unexpectedly high phase count should be reviewed before starting the run.

2. **Parallel variants (`W`)**

   Each workspace builds the application independently. Moving from one to three workspaces makes the dominant generation cost approximately 3× higher.

   - Use **1 variant** for lower-cost builds where cross-model comparison is not needed.
   - Use **3 variants** when model agreement is important for completeness or estimation confidence.

3. **Model choice**

   Model pricing affects `C` directly. The MEDIUM tier runs generation across every phase and workspace, so it normally dominates spend. HIGH is used for planning and knowledge-base initialization; LOW handles mechanical tasks.

4. **Spec and plan quality**

   Vague requirements create oversized phases, retries, divergent implementations, and reruns. Review specification completeness and phase scope before generation. Removing one unnecessary phase avoids `W` phase-sessions.

5. **Deploy and E2E scope**

   When a project is `INTEGRATION_TESTS_READY`, deployment and E2E work add paid sessions. Include those phases in `P`.

6. **Caching and retries**

   Prompt caching lowers repeated-input cost, while retries increase token use. Both are reflected in the observed `C`; no separate budgeting formula is needed once team data is available.

## Worked example

A 22-phase build using three variants produces:

```text
Phase-sessions = 22 × 3 = 66
Estimated generation cost = 66 × C
```

Using a provisional `C` of $6:

```text
Estimated generation cost = 66 × $6 = $396
```

This is a medium-to-large three-variant run. Halving the phase count roughly halves its generation cost. Running one variant instead of three reduces the dominant cost to roughly one third, but removes the cross-model agreement signal.

## Model selection

| Tier | Environment variable | Use | Selection rule |
|---|---|---|---|
| HIGH | `LLM_HIGH` | Planning and KB initialization | Use the strongest reasoning model; planning quality controls downstream scope |
| MEDIUM | `LLM_MEDIUM` | Code generation and estimation | Use 1–3 comparable current-generation coding models |
| LOW | `LLM_LOW` | Indexing and structured conversions | Use the cheapest fast model that reliably follows instructions |

Current defaults:

- HIGH: `anthropic/claude-opus-4.8`
- MEDIUM: `anthropic/claude-sonnet-4.6`, `openai/gpt-5.5`, `z-ai/glm-5.2`
- LOW: `anthropic/claude-haiku-4.5`

For a multi-model MEDIUM fleet:

- Keep models at similar coding capability and within roughly a 2× price band.
- Do not mix old and current product generations.
- Replace the fleet together when changing model generations.
- Keep HIGH at least as capable as the MEDIUM models it plans for.

Model values use OpenRouter's `provider/model` format. With only `ANTHROPIC_API_KEY` configured, SpecFlow routes Anthropic models directly.

## Team calibration

After 3–5 representative runs:

1. Record each run's total cost, `P`, and `W`.
2. Estimate the fixed overhead from the reported workflow/model usage.
3. Calculate:

   ```text
   C = (run cost - fixed overhead) ÷ (P × W)
   ```

4. Use the median `C` for normal forecasts and a higher observed value for a conservative budget.
5. Recalculate after model, pricing, fleet, or major workflow changes.

For a practical monthly forecast, group expected runs by size rather than using one average:

```text
Monthly forecast =
  small runs × small-run cost
  + medium runs × medium-run cost
  + large runs × large-run cost
```

## Budget controls

Before each run:

- Confirm the phase count is proportionate to the requested scope.
- Choose `WORKSPACE_COUNT` based on whether cross-model agreement is required.
- Confirm MEDIUM models are comparable in capability and price.
- Include deploy/E2E phases when applicable.
- Use recent team telemetry for `C`.
- Add contingency for retries or uncertain specifications.

Provider prices and model availability change. Review calibration quarterly and whenever the configured models change.

