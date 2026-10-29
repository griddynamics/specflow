---
type: Roadmap
title: "Deferred graph features"
description: "Features deferred beyond the first release"
status: stable
tags: [graph]
---
# later.md: features deferred beyond the first release

| Feature | What it adds |
|---|---|
| `graph.gap` | Gaps, contradictions, ambiguities and findings; declares its own types with origins `analysis` and `human` |
| `graph.plan` | Implementation plans; declares its own types with origin `planner` |
| Clarifications | Answers to findings and pinned information that take precedence over older document statements |
| Derivation | Judged one-level consequences of an applied answer |
| Removal confirmation | Content dropped by a revision waits for a keep or remove decision instead of being retired at once |
| Caches and determinism | Response, embedding and verdict caches; the guarantee that a re-run with the same inputs gives the same graph and makes no model calls |
| Faithfulness check | Verification samples stated edges and judges them against their text |
| Recall re-extraction | One more extraction for an active fragment without nodes |
| Cross-document links | Citation edges between fragments and relatedness edges between documents |
| Retype | Correcting a wrongly typed node while keeping its identity |
| Merge reporting | Report merges of requirements separately from rekeys |
| Build speed | Tuning options measured in the large-corpus scale test, below |
| Durable build runs | The API's `/graph_build` runs the build as an in-process task (one API process, file stores); a Temporal workflow and worker take it over with the same HTTP contract, plus Postgres-backed stores and multi-instance coordination |

## Build speed

Options from the 2026-10-23 scale test on input-specflow (568 KB; its `first` build took 598 s at concurrency 8,
model-bound), with the measured or expected effect:

- `LLMConfig.concurrency` 16 (a host setting): about 600 s → about 400 s expected; tokens unchanged.
- Fewer node windows (`max_node_windows` 8 → 4, or narrower windows): about −29% input tokens; needs a quality check.
- Windowed edge extraction (or smaller edge units): about 3× edge calls at about the same input, for edge recall (42% of nodes had no domain edge).
- Larger attribute batches: doubling the batch size halves the attribute calls and their prompt overhead.
- A lower exact-vector threshold in `find_nodes` (2,000 → about 500): index path 11 ms vs exact 131 ms at 1,271 vectors.
