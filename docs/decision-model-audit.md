---
type: Overview
title: "Decision model audit"
description: "Where a typed, probability-scored decision model could replace hand-written rules or general LLM calls in this repository, and where it must not"
status: draft
tags: [llm, graph]
---
# Decision model audit

Date of audit: 2026-11-12. Scope: the whole repository, read-only.

**Source of truth.** The task named `docs/decision-models-report.md`. That file does not exist in the repository (checked with `git ls-files` and a file-system search). The full report text pasted in the task was used instead. Every vendor, price, benchmark and rule below comes from that text and is cited as "report §N". Confidence labels are copied from the report: **[V]** vendor, **[I]** independent, **[A]** academic, **[S]** secondary. Repo figures cite `file:line`. Where a fact is unknown, it is written `UNKNOWN` and listed in section 8.

**Method note.** "Not covered by report" marks topics the report does not address. Confidence for those is low.

---

## 1. Summary

### Counts per recommendation type

15 decision points were examined.

| Recommendation | Count | IDs |
|---|---|---|
| DECISION_MODEL | 0 | none |
| HYBRID_CASCADE | 1 | DP-001 |
| FINE_TUNED_CLASSIFIER | 0 | none (no decision point has 1,000 trustworthy labels, rule R4a) |
| LLM_CALL (keep) | 9 | DP-002 to DP-010 |
| KEEP_RULES | 5 | DP-011 to DP-015 |
| CLASSICAL_ML_BANDIT | 0 | none (S11 patterns have no hit in `backend/src`, `frontend/`) |

The honest result: this repository is a poor match for decision models today. It makes hundreds to low thousands of judge calls per first build (measured), runs them as background tasks, and requires **verbatim quotes** in all 7 `judge_*.md` prompts (decision points DP-003 to DP-008). A decision model returns probabilities and no text (report §2, M1), so it cannot supply a quote. One decision point fits well (DP-001). The rest should stay as they are.

### Top 5 opportunities (ranked by estimated benefit, all small in dollars)

1. **DP-001 requirement dedup verdict** (`conform.py`). The only closed yes/no decision with no quote requirement and a verdict store that already exists. Cascade: decision model first, current small LLM for the low band, "no merge" as the floor. Cost saving is cents per build (section 4, DP-001). Real benefit: a measurable probability instead of a self-reported `high/medium/low`, and lower latency (report §7 range, not measured here).
2. **DP-003 attach-orphan judge pre-gate** (`infer.py`). Highest-volume judge: 1,345 calls measured in one first build (`docs/graph/build.md:995`). A decision model could skip the LLM call only for subjects that have no container. The share of such subjects is `UNKNOWN`, so this is an experiment, not a plan.
3. **DP-002 hub identity pair and twin questions** (`hubjudge.py`). Same question shape as DP-001 (same thing or not, plus a label choice). Volume is 2 to 3 twin calls and 28 to 43 pair batches per build (`docs/graph/build.md:989`). Adopt only after DP-001 proves out.
4. **DP-008 answered-elsewhere pre-screen** (`answered.py`). A per-fragment yes/no could screen the 4 candidates before the quote-giving LLM call. Capped at 60 calls per build (`spec-ontology.yaml:1108`), so benefit is minimal.
5. **DP-004 cross-document judge, decomposed.** One call holds up to 8 pairs plus owner decisions in about 17.2k input tokens measured (`docs/graph/build.md:996`). Splitting into narrow questions is possible, but quotes are still required, so this stays an LLM call.

Items 2 to 5 are conditional on measurements that do not exist yet.

### Top 3 risks

1. **Quote-evidence contract.** Verdicts are accepted only when each quote is a verbatim substring of its fragment (`graph/core/llm/judge.py:81-98`, used at `infer.py:1521`, `answered.py:440`). A decision model cannot meet this. Dropping the quote check to adopt one would remove the repo's main guard against invented links.
2. **No labels to calibrate on.** The repo has no human-labeled judge set of 1,000 or more. Stored verdicts are LLM outputs, not truth (`conform.py:969-1010`). The report says vendor calibration is unproven on your data and needed "a few thousand labels" for recalibration in one independent audit (report TL;DR, §3.1 [A]).
3. **Wrong merges and links persist.** A wrong dedup merge joins two different requirements and can hide a contradiction (`docs/graph/how-it-works.md`, "Dedup in detail"). Stored verdicts are keyed by `prompt|model` (`consolidate.py:217-230`, `conform.py:969-975`), so a silent vendor model update (report: Microsoft-Decision-1 "weights updated continually"; Perplexity aliases) would not change the key and would go unseen.

---

## 2. Repo context

### Stack (Phase 1)

| Item | Finding | Evidence |
|---|---|---|
| Size | 2,431 tracked files; 1,106 under `backend/`, 354 under `frontend/` | `git ls-files` |
| Backend | Python 3.12, uv project, FastAPI, Temporal workers, SQLAlchemy and asyncpg, graph stores Neo4j, ArcadeDB, PostgreSQL (TigerGraph optional) | `backend/pyproject.toml`, `Makefile:29-60` |
| Frontend | Next.js 16 | `README.md` badge |
| Entry points | `specflow = cli:main`, `backend/src/api/app.py`, `backend/src/temporal/worker.py`, MCP server `backend/src/api/mcp`, frontend app | `pyproject.toml`, tree |
| Infra | Docker Compose, Helm chart `charts/specflow`, GCS or S3 object storage, Aurora or Cloud SQL class Postgres, Keycloak auth, optional Langfuse tracing | `docs/configuration.md:99-150, 355-375, 424-433` |
| Cloud | Dual: `gcp` and `aws` profiles. Which one a given deployment uses is `UNKNOWN` | `docs/configuration.md:355` |
| Queues and batch | Temporal workflows for parse and agents. **Graph builds do not use Temporal: they run in-process in the API, one asyncio task per project, polled over 202 endpoints ("no Temporal yet")** | `backend/src/temporal/`, `api/graph_build/runner.py:1,131`, `api/routers/graph_build.py:4` |
| Rules engines | `plan-rules.yaml` (plan placement), ontology YAML (types, floors, caps). No DMN or Drools | `graph/plan/plan-rules.yaml`, `graph/core/ontology/spec-ontology.yaml` |
| Existing ML classifiers | None found. Retrieval uses embeddings plus full-text (`graph/core/reader.py:72-81`) | grep |
| Feature flags | No flag service found (grep for LaunchDarkly, Unleash, GrowthBook, feature flag: only validator fixtures). Config is env settings, ontology YAML and internal params | `docs/configuration.md`, `hubjudge.py:208-219` |
| Compliance hints | None for this product. PCI and "data residency" strings in the ontology are example text for specs the product reads, not constraints on it | `spec-ontology.yaml:574-606` |

### LLM usage

| Path | SDK and model | Where |
|---|---|---|
| Graph build and plan (all closed decisions below) | `openai.AsyncOpenAI` against an OpenAI-compatible gateway (`LLM_GATEWAY_URL`). Models set by env: `GRAPH_LLM_MODEL`, `GRAPH_SMALL_MODEL`, `GRAPH_EMBEDDING_MODEL`. Judge class defaults to the main model when no judge model is set. **Actual model names: `UNKNOWN`** | `graph/core/llm/gateway.py:406-415`, `graph/core/config.py:57-74`, `docs/configuration.md:32-34` |
| One choke point | Every model call goes through `Gateway` (requirement FR-LLM-01). Call classes: `extraction`, `judge`, `small`, `embedding`. Concurrency default 8. Timeouts and attempts: extraction 180 s / 4, small 60 s / 4, judge 120 s / 3 | `docs/graph/requirements.md:60`, `graph/core/config.py:40-48`, `docs/configuration.md:64` |
| Output handling | JSON-schema `response_format`, pydantic validation, one repair retry (smell S3) | `gateway.py:430-475` |
| PDF figure and math enrichers | `llm.client.Client`, default `claude-haiku-4-5`, vision | `file_parsers/pdf/enricher.py:30` |
| Agents | `claude-agent-sdk` (0.2.174), `openai-agents` with Temporal. Document summarizer is free text via a tool call | `agent_runtime/summarizer.py`, `pyproject.toml` |
| Tracing | Langfuse (optional). `UsageRecorder` counts calls, tokens, duration per call class and prompt | `gateway.py:415, 422-425` |

### Constraints found

- **Volume is low and measured per build, not per day.** A first build of a 157k-token corpus: 598 s, 288 calls, 2.79M input tokens, 552 requests, no retries or errors (`docs/graph/how-it-works.md:486-488`). Calls per month: `UNKNOWN`.
- **Bounded by design.** Judge calls are capped per build: conform owner-conflict 200, answered-elsewhere 60, inference 100 per rule (on first builds up to 2,000 per rule within 11,000,000 input tokens), cross-document 36 (`spec-ontology.yaml:1097-1176`).
- **Judge inputs are large.** Measured: about 17.2k input tokens per cross-document call (specflow corpus) and 8.7k (stitch corpus); the measured update build averaged 715k tokens over 149 inference calls, about 4.8k per call (`docs/graph/build.md:995-996`). These sizes exceed the 4K window of Strands Decider (report §3.8). The 17.2k size is over half of the 32K state limit of Jev and of the 32,768-token context of Microsoft-Decision-1 (report §3.1, §3.5).
- **Text is user-supplied.** Inputs are fragments of uploaded or Drive documents. They are treated as untrusted by the gateway (`gateway.py:86-127`) and by every judge prompt. Treat them as attacker-writable.
- **Latency is not interactive.** Builds are background tasks in the API process; the UI polls (`api/graph_build/runner.py:131`, `api/routers/graph_build.py:4`). No latency complaint is recorded in the repo.
- **Verdict memory exists.** Verdicts are stored per project with an evidence hash and a `prompt|model` fingerprint (`consolidate.py:217-330`, `conform.py:969-1010`). Changing prompt or model re-asks the pair.
- **A replay harness exists.** `backend/examples/graph_replay.py` replays one step of a stored build with other settings (A/B). `graph_eval.py` reports speed, calls and tokens per step. `docs/graph/analysis.md:164` says judged items can be labelled per rule and confidence band.
- **Existing measured quality.** Node precision 90%, edge precision 93% on a reviewed sample; dedup merge precision .93 (specflow corpus) and 1.00 (stitch corpus) at replay TEST.3 R0 (`docs/graph/how-it-works.md:484`, `docs/graph/build.md:588-589`). The size of those labeled samples is not stated.

### UNKNOWNs

Builds per day or month; real model names and prices behind the gateway; gateway vendor and region; data classification of customer documents; document languages; token size of a dedup call; share of attach-orphan judge calls that return null; count of human-labeled judge items; latency target; rate limits of the gateway. Full list in section 8.

---

## 3. Decision point inventory

Volume anchors are first builds of the repo's two test corpora ("specflow" 2,828 requirements, "stitch" 443), from `docs/graph/build.md`.

| ID | Location | Current implementation | Recommendation | Rule fired | Top candidate | Confidence |
|---|---|---|---|---|---|---|
| DP-001 | `graph/build/conform.py::_adjudicate_dedup` (`dedup_adjudicate.3`) | Small LLM yes/no with 3-level confidence, behind code guards and a direct-merge rule | HYBRID_CASCADE | R4f then R5 | pplx-decider v1.2 | low |
| DP-002 | `graph/build/hubjudge.py` pair pass (`consolidate_hubs.4` batches) and twin pass (`hub_twins.3`) | Judge-class LLM; groups of ids, or same + label per pair | LLM_CALL | R4f, then volume exclusion (§6.2) | none now; follow-on to DP-001 | low |
| DP-003 | `graph/build/infer.py` attach_orphan (`judge_attach_orphan`, rules INF-CONT-01, INF-CONT-02) | Judge-class LLM: one of 3 candidates or null, plus quotes | LLM_CALL | R3 | decision model as optional pre-gate only | low |
| DP-004 | `graph/build/infer_xdoc.py` (`judge_cross_document`, INF-XDOC-01) | Judge-class LLM: link yes/no, relation, direction, owner, plus quotes | LLM_CALL | R3 | none | low |
| DP-005 | `graph/build/infer.py` endpoints (`judge_endpoints`, INF-INT-01) | Judge-class LLM: link and direction per module pair, plus quotes | LLM_CALL | R3 | none | low |
| DP-006 | `graph/build/infer.py` realization (`judge_realization`, INF-CAP-01) | Judge-class LLM: multi-select modules, plus quotes | LLM_CALL | R3 | none | low |
| DP-007 | `graph/build/normalise.py` (`judge_owner_conflict`) | Judge-class LLM: relates or unrelated per lost node, plus quotes | LLM_CALL | R3 | none | low |
| DP-008 | `graph/build/answered.py` (`judge_answered_elsewhere`) | Small LLM: answered, partly or open, plus fragment id and quote | LLM_CALL | R3 | optional per-fragment pre-screen | low |
| DP-009 | `graph/build/prompts.py::attribute_system_message` (`attributes.7`) | Small LLM fills `in_scope`, `is_assumption`, `source_status` together with free text | LLM_CALL | R3 | none | low |
| DP-010 | `file_parsers/pdf/enricher.py::PdfFigureEnricher` (figure `kind`, `is_decorative`) | Vision LLM returns kind, decorative flag and description in one call | LLM_CALL | R3 | none | low |
| DP-011 | `graph/build/conform.py` dedup guards and direct-merge rule | Code: numbers, negation, modality, ids, same sentence, cosine and Jaccard | KEEP_RULES | R0 | none | high |
| DP-012 | `graph/core/reader.py` hybrid search fusion; embedding candidate floors | Code: weighted rank fusion, cosine floors | KEEP_RULES | R0 | none | medium |
| DP-013 | `backend/src/llm/permissions.py` shell-command allow and deny policy | Code: parser-based allow list | KEEP_RULES | R6 (veto) | none | high |
| DP-014 | `backend/src/drive/categories.py`, `file_parsers/formats.py` | Code: category and parser by MIME type | KEEP_RULES | R0 | none | high |
| DP-015 | `graph/plan/plan-rules.yaml` and plan placement | YAML rules over graph types and relations | KEEP_RULES | R0 | none | high |

---

## 4. Decision point details

Each entry has one sentence of rationale, then a JSON object in the report's Section 9 schema plus an `"evidence"` field. Values the repo does not give are `UNKNOWN`. Where the schema example uses a number and the real value is unknown or needs context, the value is a string.

### DP-001: requirement dedup verdict

Rationale: a closed yes/no on two short statements, with no quote requirement, a code guard layer that already holds the numeric, negation and modality logic, and an existing verdict store; this is the best fit in the repo.

```json
{
  "id": "DP-001",
  "location": "backend/src/graph/build/conform.py::_adjudicate_dedup",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (GRAPH_SMALL_MODEL, env)", "prompt_ref": "dedup_adjudicate.3 (inline in conform.py::dedup_messages)", "output_parser": "Gateway.structured: JSON schema + pydantic + one repair retry"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN (two names, two quotes of at most 300 chars each, one type description)", "languages": ["UNKNOWN (prompts are English)"], "untrusted_text": true},
  "output": {"type": "yes/no", "options": ["same=true", "same=false"], "plus": [{"type": "choice", "name": "confidence", "options": ["high", "medium", "low"]}]},
  "volume": {"per_day": "UNKNOWN (194 judged pairs in one first build of the 2,828-requirement corpus; 11 for the 443-requirement corpus: docs/graph/build.md:991)", "peak_qps": "UNKNOWN (gateway concurrency 8)"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN (call timeout 60000, 4 attempts: core/config.py:46)", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "high (a wrong merge joins two different requirements)", "fn_cost": "low to medium (a duplicate node stays)"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN (the same text already goes to LLM_GATEWAY_URL)"},
  "regulatory": "none found in repo; UNKNOWN for customer documents",
  "labels_available": "0 trustworthy human labels visible; stored verdicts are LLM outputs; offline labeled replay sets exist, size UNKNOWN",
  "hidden_computation": ["numbers of one kind, negation, must/shall vs should/may, explicit ids, same source sentence: already computed in code before the model is asked (docs/graph/build.md:590-602); keep there", "name cosine and token Jaccard: computed in code"],
  "recommendation": "HYBRID_CASCADE",
  "candidates": [
    {"name": "pplx-decider v1.2 (hosted $0.02/M, or self-host, Apache-2.0)", "why": "cheapest hosted text option; open weights keep a self-host path if data may not leave; report §3.7", "confidence": "low"},
    {"name": "Jev jev-1.13.0 ($0.042/M)", "why": "pinned version fits the prompt|model fingerprint; 594 of 600 answers identical two weeks apart [I]; English-first; report §3.1", "confidence": "low"},
    {"name": "No vendor: keep current small LLM + guards", "why": "volume is 194 calls per first build; fine-tuned classifier not eligible (no 1,000 labels, R4a)", "confidence": "medium"}
  ],
  "thresholds": {"auto_accept": "UNKNOWN: fit on held-out labels", "llm_fallback_below": "UNKNOWN: fit on held-out labels", "human_if": "never auto-merge in the low band; keep existing rule: low means no merge and nothing stored"},
  "expected_benefit": {"cost_reduction_pct": "UNKNOWN (baseline price UNKNOWN; see arithmetic below)", "p50_latency_ms_before_after": "UNKNOWN before; report §7 range: LLM about 700-4,000 ms vs decision model about 40-500 ms", "auto_resolved_share_est": "UNKNOWN (R5 needs at least 0.40 to justify a cascade)"},
  "risks": ["miscalibration on this distribution (report §11)", "pair order bias: first-option bias (Jev), alphabetical key sort (Gemini), key order moved probabilities up to 0.19 (Strands)", "no escape option forces a class (0 of 30 out-of-scope flagged, report §3.1)", "adversarial text in a statement (report §3.3 gotcha)", "alias hides served version (pplx)", "DedupMemory fingerprint cannot see a silent vendor update", "endpoint is not chat-completions: needs adapter inside Gateway"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 2000, "metrics": ["precision of merge", "macro_f1", "ECE", "coverage@precision0.95", "order-swap agreement", "latency p50/p95"], "shadow_days": "14 (report default); better: 2 first builds and 3 update builds"},
  "rollout": "replay on stored builds -> shadow (log DM verdict beside the LLM verdict, apply LLM) -> DUAL compare -> canary by project -> cascade with LLM fallback",
  "source_confidence": "low (report has no benchmark for requirement dedup or entity resolution: not covered by report; closed yes/no triage is covered, §5)",
  "evidence": [
    "conform.py:113-122 (DEDUP_PROMPT, cosine 0.85 / 0.95, rich floor 0.80)",
    "conform.py:128-130 (_STRONG_MODALS, _NEGATIONS)",
    "conform.py:890-897 (SameCommitment: same: bool, confidence)",
    "conform.py:1486 (gateway.structured(\"small\", ..., SameCommitment))",
    "conform.py:969-1010 (DedupMemory stores same + confidence + evidence hash)",
    "docs/graph/build.md:581-618 (candidates, guards, direct merge, low = no merge, memory)"
  ]
}
```

**Vendor ranking and cost.** Arithmetic uses report prices. Token size per call is the report's own reference state (1,500 tokens, report §4.1), not a repo measurement. Replace it after measuring.

- Calls: 194 per first build (specflow corpus, `docs/graph/build.md:991`). Tokens: 194 × 1,500 = 291,000.
- pplx-decider v1.2: 291,000 / 1,000,000 × $0.02 = **$0.0058**. Confidence: low. Fits: cheapest, open weights. Might not: aliases hide the served version (red flag, report §8); latency under 2 s for a few hundred tokens, confidence medium (report §3.7).
- Jev 1.13.0: (1,500 + 122 question tokens) × 194 = 314,668 tokens × $0.042 / 1,000,000 = **$0.0132**. Confidence: low. Fits: version pin. Might not: English-first, first-option bias, rate-limit conflict (1,200 req/min vs 80 req/s, report §3.1). At concurrency 8 and 0.20 s median [A], peak could reach 8 / 0.20 = 40 req/s = 2,400 req/min, above the 1,200 req/min figure; the gateway backs off on 429.
- OpenAI Decisions API (gpt-6-luna), only if the gateway vendor is OpenAI: 291,000 × $0.10 / 1,000,000 = **$0.0291**. Only the ZDR, HIPAA and EU residency case (R4d) justifies it.
- Self-hosted open model: Amazon's case used `ml.g5.xlarge` at $1.408 per hour [V-internal] (report §3.8). Always on for 30 days: 1.408 × 24 × 30 = **$1,013.76** per month. At this volume it only pays if data may not leave the network (R4b).
- Illustrative baseline only: the report lists Claude Haiku 5.5 at $0.10 input per 1M tokens (report §3.12). Input-only cost for the same tokens is $0.0291; output tokens are `UNKNOWN`. The repo's real baseline model is `UNKNOWN`.

Conclusion: savings are cents per build. Justify the work by calibration and latency, or do not do it.

### DP-002: hub identity pair pass and twin pass

Rationale: the pair pass and the twin pass ask "same part of the system?" and "which label?" for candidate pairs, which matches a decision-model question, but at 28 to 43 pair batches and 2 to 3 twin calls per build the volume sits below the report's exclusion line (§6.2).

```json
{
  "id": "DP-002",
  "location": "backend/src/graph/build/hubjudge.py::_pair_pass, ::_twin_pass",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (judge class: GRAPH_LLM_MODEL unless a judge model is set)", "prompt_ref": "consolidate_hubs.4 (pair batches of up to 16 hubs), hub_twins.3 (up to 8 pairs per call)", "output_parser": "Gateway.structured into HubGroups / TwinAnswers"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "yes/no", "options": ["same true with merged label", "same false"], "plus": [{"type": "choice", "name": "label", "options": ["merge-across hub labels of the ontology"]}, {"type": "choice", "name": "confidence", "options": ["high", "medium", "low"]}]},
  "volume": {"per_day": "UNKNOWN (specflow corpus: 28 pair batches and 2 twin calls; 43 and 3 in a later run: docs/graph/build.md:989)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN (judge timeout 120000, 3 attempts)", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "high (two different parts merged)", "fn_cost": "medium (one part split, its requirements split in half: docs/graph/how-it-works.md Dedup in detail)"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN; hub merge precision .93 / 1.00 measured offline, sample size not stated (docs/graph/build.md:588)",
  "hidden_computation": ["compound-name rule ('X and Y' names two things), modifier_of, cosine candidates: code already does the candidate and alias logic (hubjudge.py:664-675, 1007-1045)"],
  "recommendation": "LLM_CALL",
  "candidates": [
    {"name": "Keep current judge LLM", "why": "volume below 1,000 calls per month per build count unless builds exceed about 300 per month (UNKNOWN)", "confidence": "low"},
    {"name": "pplx-decider v1.2 (follow-on, same question design as DP-001)", "why": "twin question is a bool plus a label choice per pair; split into noul + choice", "confidence": "low"}
  ],
  "thresholds": {"auto_accept": "UNKNOWN", "llm_fallback_below": "UNKNOWN", "human_if": "low band changes nothing today (existing rule)"},
  "expected_benefit": {"cost_reduction_pct": "UNKNOWN", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "UNKNOWN"},
  "risks": ["batched groups of up to 16 hubs are a clustering answer, not a single closed decision: they would need decomposing into pair questions", "same-name-two-labels default 'same' is a prompt rule that a decision model cannot hold without criteria text", "compound-name and part-vs-grouping rules are long and lived in prompt text"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 500, "metrics": ["precision of merge", "macro_f1", "ECE"], "shadow_days": "UNKNOWN"},
  "rollout": "only after DP-001 pilot passes: replay pair and twin steps (graph_replay.py) -> shadow",
  "source_confidence": "low (not covered by report: entity merge adjudication)",
  "evidence": [
    "hubjudge.py:118-131 (TWINS_PROMPT, TWIN_BATCH_PAIRS 8, PAIR_K 5, PAIR_FLOOR 0.52, PAIR_CALL_CAP 60)",
    "hubjudge.py:585-618 (twin_system), :642-650 (TwinAnswer: same, label, confidence)",
    "hubjudge.py:574-579 (HubGroups), :985, :1398 (gateway.structured calls)",
    "docs/graph/build.md:989 (measured pair batches and twin calls)"
  ]
}
```

Cost note: not computed. Token size per call is `UNKNOWN`, and the volume is too small to matter.

### DP-003: attach-orphan judge (INF-CONT-01, INF-CONT-02)

Rationale: the answer is one of at most 3 candidates or null, which is closed, but code accepts a verdict only with a verbatim quote, so rule R3 fires; a decision model could at most skip the call when no container exists.

```json
{
  "id": "DP-003",
  "location": "backend/src/graph/build/infer.py (AttachOrphanVerdict; prompt judge_attach_orphan; batch prompt judge_attach_orphan_batch exists, batching off)",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (judge class)", "prompt_ref": "backend/src/graph/core/ontology/judge_prompts/judge_attach_orphan.md", "output_parser": "Gateway.structured + meets_floor + quotes_valid"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN per rule (mixed inference average about 4,800 in the measured update build)", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["C1", "C2", "C3", "null"], "plus": [{"type": "score", "name": "confidence"}]},
  "volume": {"per_day": "UNKNOWN (first build: 1,345 measured calls on the specflow corpus, 86 on stitch, plus 105 and 25 hub subjects: docs/graph/build.md:995)", "peak_qps": "UNKNOWN (concurrency 8)"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN (judge timeout 120000)", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "high (a wrong container is worse than none: prompt rule)", "fn_cost": "low (edge stays unwritten and the subject is queued)"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN; replay can label items per rule and confidence band (docs/graph/analysis.md:164)",
  "hidden_computation": ["candidate choice by cosine and heading names is already code (docs/graph/how-it-works.md:766)", "scope exclusion in_scope=no is already code (ontology exclude_where)"],
  "recommendation": "LLM_CALL",
  "candidates": [
    {"name": "Keep judge LLM (quotes required)", "why": "quotes_valid needs verbatim text; a decision model emits no text (report §2 M1)", "confidence": "medium"},
    {"name": "pplx-decider v1.2 as null pre-gate (experiment)", "why": "skips the LLM only for 'no container'; gain = null share x LLM cost, share UNKNOWN", "confidence": "low"}
  ],
  "thresholds": {"auto_accept": "none: the LLM still runs for every non-null subject", "llm_fallback_below": "UNKNOWN", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "UNKNOWN (needs null share)", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "UNKNOWN"},
  "risks": ["verbalized confidence floor 0.8 is replaced by an uncalibrated probability", "choice always spreads probability even when none fits (report §3.3): add an explicit 'none' option", "added call for every non-null subject raises cost"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 1000, "metrics": ["null recall at precision 0.95", "ECE", "coverage@precision0.95"], "shadow_days": "UNKNOWN"},
  "rollout": "measure null share from stored inference reports first; if above 40% run replay -> shadow",
  "source_confidence": "low",
  "evidence": [
    "infer.py:125-129 (AttachOrphanVerdict.target: str | None)",
    "infer.py:1521 (meets_floor and quotes_valid gate)",
    "spec-ontology.yaml:1110-1130 (floor 0.8, max_candidates 3, batch_prompt off)",
    "judge_prompts/judge_attach_orphan.md (answer null when unsure)",
    "graph/core/llm/judge.py:81-98 (quotes_valid)"
  ]
}
```

Cost arithmetic for the optional pre-gate, order of magnitude: 1,345 calls × 4,798 tokens (715,000 / 149 from the measured update build, a mixed-rule average) = 6,453,310 tokens. pplx-decider v1.2: × $0.02 / 1M = $0.129. Jev: × $0.042 / 1M = $0.271. OpenAI: × $0.10 / 1M = $0.645. The first-build inference input budget is 11,000,000 tokens (`spec-ontology.yaml:1112`).

### DP-004: cross-document judge (INF-XDOC-01)

Rationale: one call decides up to 8 member pairs (link, relation, direction) plus owner choices and needs quotes from both sides, so it can be split into narrow questions but still must produce text, which gives R3.

```json
{
  "id": "DP-004",
  "location": "backend/src/graph/build/infer_xdoc.py (CrossDocumentVerdict; prompt judge_cross_document)",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (judge class)", "prompt_ref": "judge_prompts/judge_cross_document.md (114 lines)", "output_parser": "Gateway.structured + floor + quotes from both members"},
  "inputs": {"modality": ["text"], "avg_tokens": 17200, "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["no link", "RELATES_TO", "DEPENDS_ON"], "plus": [{"type": "choice", "name": "owner per orphan member"}, {"type": "choice", "name": "direction"}]},
  "volume": {"per_day": "UNKNOWN (first build: 212 measured calls specflow, 14 stitch: docs/graph/build.md:996)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "high (false link or dependency)", "fn_cost": "low"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN; link precision .97 measured at pair_floor calibration (docs/graph/analysis.md:121)",
  "hidden_computation": ["pair choice by cosine and same-owner exclusion: code", "DEPENDS_ON needs dependency words: a literal cue, could be a code check"],
  "recommendation": "LLM_CALL",
  "candidates": [
    {"name": "Keep judge LLM", "why": "quotes from both members are mandatory; 17.2k-token inputs exceed the Strands 4K window and are over half of the 32K state limit of Jev and of the 32,768-token context of Microsoft-Decision-1", "confidence": "medium"}
  ],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "0"},
  "risks": ["decomposition would multiply fragment tokens per question", "quotes lost"],
  "migration_effort": "L",
  "eval_plan": {"labeled_set": 500, "metrics": ["link precision", "ECE"], "shadow_days": "UNKNOWN"},
  "rollout": "none recommended",
  "source_confidence": "low",
  "evidence": [
    "infer_xdoc.py:70 (CrossDocumentVerdict)",
    "infer_xdoc.py:477-486 (quotes then floor)",
    "spec-ontology.yaml:1160-1176 (pair_floor 0.55, floor 0.85, max_pairs 8, cap 36)",
    "judge_prompts/judge_cross_document.md (links, owners, evidence rules)"
  ]
}
```

Cost arithmetic if it were adopted (not recommended): 212 × 17,200 = 3,646,400 tokens. pplx-decider $0.0729, Jev $0.153, OpenAI $0.365 per first build (report prices).

### DP-005: endpoints judge (INF-INT-01)

Rationale: decides link and direction per module pair from one integration requirement, with quotes required; R3.

```json
{
  "id": "DP-005",
  "location": "backend/src/graph/build/infer.py (EndpointsVerdict; prompt judge_endpoints)",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (judge class)", "prompt_ref": "judge_prompts/judge_endpoints.md", "output_parser": "Gateway.structured + floor + quotes"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["no link", "A to B", "B to A"], "plus": []},
  "volume": {"per_day": "UNKNOWN (0.04 calls per expected node in a first build: docs/graph/build.md:995)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "medium (wrong INTEGRATES_WITH edge)", "fn_cost": "low"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN",
  "hidden_computation": ["pair eligibility (already linked either way): code"],
  "recommendation": "LLM_CALL",
  "candidates": [{"name": "Keep judge LLM", "why": "quotes required; volume far below 1,000 per month per rule unless builds are very frequent (UNKNOWN); direction judgment is sensitive to first-option bias (report §8)", "confidence": "medium"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "0"},
  "risks": ["both directions invalid for one pair (prompt rule) needs a joint decision"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 200, "metrics": ["precision", "direction accuracy"], "shadow_days": "UNKNOWN"},
  "rollout": "none recommended",
  "source_confidence": "low",
  "evidence": ["infer.py:113-116 (EndpointsVerdict.links)", "spec-ontology.yaml:1136-1147 (INF-INT-01)", "judge_prompts/judge_endpoints.md"]
}
```

### DP-006: realization judge (INF-CAP-01)

Rationale: multi-select of modules that "do the work", needs quotes, rare calls (0.05 per node); R3.

```json
{
  "id": "DP-006",
  "location": "backend/src/graph/build/infer.py (RealizationVerdict; prompt judge_realization)",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (judge class)", "prompt_ref": "judge_prompts/judge_realization.md", "output_parser": "Gateway.structured + floor + quotes"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["subset of up to 12 candidate modules, or empty"], "plus": []},
  "volume": {"per_day": "UNKNOWN (0.05 calls per expected node in a first build: docs/graph/build.md:995)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "medium", "fn_cost": "low"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN",
  "hidden_computation": ["candidate modules by graph paths: code"],
  "recommendation": "LLM_CALL",
  "candidates": [{"name": "Keep judge LLM", "why": "quotes required; 'does the work vs holds the data' is a subtle semantic split", "confidence": "medium"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "0"},
  "risks": ["one multi-select question per candidate would need one noul each"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 200, "metrics": ["precision", "recall"], "shadow_days": "UNKNOWN"},
  "rollout": "none recommended",
  "source_confidence": "low",
  "evidence": ["infer.py:119-122 (RealizationVerdict.targets)", "spec-ontology.yaml:1148-1158 (INF-CAP-01)", "judge_prompts/judge_realization.md"]
}
```

### DP-007: owner-conflict judge

Rationale: per lost node, `relates` or `unrelated`, but `relates` needs a quote naming the loser, so R3.

```json
{
  "id": "DP-007",
  "location": "backend/src/graph/build/normalise.py (OwnerVerdict; prompt judge_owner_conflict)",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (judge class)", "prompt_ref": "judge_prompts/judge_owner_conflict.md", "output_parser": "Gateway.structured + floor 0.7 + quotes"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["relates", "unrelated"], "plus": [{"type": "score", "name": "confidence"}]},
  "volume": {"per_day": "UNKNOWN (cap 200 calls per build: spec-ontology.yaml:1099)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "low (a weaker RELATES_TO link)", "fn_cost": "low"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN",
  "hidden_computation": ["owner selection and cycle breaking: code"],
  "recommendation": "LLM_CALL",
  "candidates": [{"name": "Keep judge LLM", "why": "quote per loser is required; below-floor outcome already falls back to RELATES_TO", "confidence": "medium"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "0"},
  "risks": ["none beyond DP-003"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 200, "metrics": ["precision"], "shadow_days": "UNKNOWN"},
  "rollout": "none recommended",
  "source_confidence": "low",
  "evidence": ["normalise.py:221 (OwnerVerdict)", "normalise.py:254-261 (below floor gives fallback relation)", "spec-ontology.yaml:1097-1101 (cap 200, floor 0.7)"]
}
```

### DP-008: answered-elsewhere judge

Rationale: three-way choice (answered, partly, open) per open question, but `answered` is accepted only with a verbatim quote from the cited fragment, so R3.

```json
{
  "id": "DP-008",
  "location": "backend/src/graph/build/answered.py::_ask_all (AnswerVerdict; prompt judge_answered_elsewhere)",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (small class)", "prompt_ref": "judge_prompts/judge_answered_elsewhere.md", "output_parser": "Gateway.structured + floor 0.8 + quotes_valid"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN (question quote up to 600 chars plus 4 candidate fragments)", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["answered", "partly", "open"], "plus": [{"type": "score", "name": "confidence"}]},
  "volume": {"per_day": "UNKNOWN (cap 60 per build; 43 of 48 stitch questions and 10 of 14 specflow questions were called in the calibration run: docs/graph/build.md:709)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN (small timeout 60000)", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "high (a real open question is dropped)", "fn_cost": "low (a duplicate question stays)"},
  "audit": {"version_pinning": true, "log_probs": true, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "10 labeled answered questions in the stitch calibration (docs/graph/build.md:709); too few for any fit",
  "hidden_computation": ["default-column detection by regex (answered.py:36), candidate choice by cosine 0.35: code"],
  "recommendation": "LLM_CALL",
  "candidates": [{"name": "Keep small LLM", "why": "quote required; default-vs-decision wording needs reading, not matching; volume capped at 60", "confidence": "medium"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "0"},
  "risks": ["partly versus answered is a graded call"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 100, "metrics": ["precision of answered"], "shadow_days": "UNKNOWN"},
  "rollout": "none recommended",
  "source_confidence": "low",
  "evidence": ["answered.py:45-53 (AnswerVerdict)", "answered.py:436-446 (answered needs floor, fragment, quote)", "answered.py:474 (gateway.structured)", "spec-ontology.yaml:1102-1108 (floor 0.8, cap 60)"]
}
```

### DP-009: attribute enums inside the attributes call

Rationale: three closed enums (`in_scope`, `is_assumption`, `source_status`) are filled in one call with free text (`stmt`, `acceptance_criteria`, `release`), so the call must run anyway and a decision model saves nothing.

```json
{
  "id": "DP-009",
  "location": "backend/src/graph/build/prompts.py::attribute_system_message (attributes.7)",
  "current_impl": {"type": "llm_call", "model": "UNKNOWN (small class)", "prompt_ref": "attributes.7", "output_parser": "Gateway.structured + ontology alias tables (explicit to stated, true to yes)"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN (batches of up to 20 nodes, about 12,000 tokens of fragment text)", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["yes", "no"], "plus": [{"type": "choice", "name": "source_status", "options": ["stated", "implied"]}, {"type": "choice", "name": "is_assumption", "options": ["yes", "no"]}]},
  "volume": {"per_day": "UNKNOWN (one call per batch of up to 20 new or changed nodes: docs/graph/how-it-works.md:327)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "high for in_scope=no (spine excludes the requirement from inference and plans)", "fn_cost": "medium"},
  "audit": {"version_pinning": true, "log_probs": false, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN (reviewed samples exist, size not stated)",
  "hidden_computation": ["heading-path lookup for scope words could be a code feature"],
  "recommendation": "LLM_CALL",
  "candidates": [{"name": "Keep small LLM", "why": "free-text fields force the call; splitting adds a second call per batch", "confidence": "medium"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0 or negative", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "0"},
  "risks": ["alias tables (S3) show enum drift today; a typed output would remove them, but not worth a second call"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 500, "metrics": ["macro_f1 per enum"], "shadow_days": "UNKNOWN"},
  "rollout": "none recommended",
  "source_confidence": "low",
  "evidence": ["spec-ontology.yaml:161-190 (source_status, is_assumption, in_scope with aliases)", "prompts.py:32, 953-1014 (ATTRIBUTES_PROMPT, enumerated attributes)", "gateway.py:446-473 (repair retry)"]
}
```

### DP-010: PDF figure kind and decorative flag

Rationale: `kind` (6 values) and `is_decorative` are closed and could gate later steps, but the same vision call also returns a summary and details as free text, so R3 fires and the call must run.

```json
{
  "id": "DP-010",
  "location": "backend/src/file_parsers/pdf/enricher.py::PdfFigureEnricher._describe",
  "current_impl": {"type": "llm_call", "model": "claude-haiku-4-5 (default)", "prompt_ref": "backend/src/file_parsers/pdf/prompts/figure_description.md", "output_parser": "request_json + pydantic FigureDescription, retries"},
  "inputs": {"modality": ["image", "text"], "avg_tokens": "UNKNOWN", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "choice", "options": ["chart", "diagram", "equation", "screenshot", "photo", "other"], "plus": [{"type": "noul", "name": "is_decorative"}]},
  "volume": {"per_day": "UNKNOWN (one call per extracted figure)", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "low", "fn_cost": "medium (a diagram never reaches the Mermaid step)"},
  "audit": {"version_pinning": true, "log_probs": false, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "UNKNOWN",
  "hidden_computation": [],
  "recommendation": "LLM_CALL",
  "candidates": [
    {"name": "Keep the vision LLM", "why": "description text is the product of this call", "confidence": "medium"},
    {"name": "Gemini Decision 1 or Clef as an is_decorative pre-gate (image input)", "why": "report §3.3, §3.6 list image input; Jev 1.13 has none; gain needs the share of decorative figures (UNKNOWN); Gemini is v1beta and AI Studio only", "confidence": "low"}
  ],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "UNKNOWN", "p50_latency_ms_before_after": "UNKNOWN", "auto_resolved_share_est": "UNKNOWN"},
  "risks": ["extra image call for every non-decorative figure", "d1-ultra re-bills images per question (report §3.9)"],
  "migration_effort": "M",
  "eval_plan": {"labeled_set": 500, "metrics": ["decorative recall at precision 0.95"], "shadow_days": "UNKNOWN"},
  "rollout": "none recommended until the decorative share is measured",
  "source_confidence": "low",
  "evidence": ["pdf/enricher.py:30 (DEFAULT_MODEL claude-haiku-4-5)", "pdf/enricher.py:48-59 (FigureDescription.kind, is_decorative, summary, details)", "pdf/mermaid_enricher.py:54-66 (only kind == diagram and not decorative continue)", "pdf/prompts/figure_description.md"]
}
```

### DP-011 to DP-015: keep as rules

These are detected hits (S6, S8, S12). Each is a deterministic or literal rule, so the detail is a compact JSON object plus the reason in section 5.

```json
{
  "id": "DP-011",
  "location": "backend/src/graph/build/conform.py (dedup guards, direct-merge rule)",
  "current_impl": {"type": "rules", "model": "none", "prompt_ref": "none", "output_parser": "none"},
  "inputs": {"modality": ["text", "structured"], "avg_tokens": "UNKNOWN", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "yes/no", "options": ["merge", "refuse"], "plus": []},
  "volume": {"per_day": "UNKNOWN", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "microseconds to milliseconds", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "high", "fn_cost": "low"},
  "audit": {"version_pinning": true, "log_probs": false, "rationale_required": true},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": false},
  "regulatory": "none found in repo",
  "labels_available": "not needed",
  "hidden_computation": ["numbers, negation, modality, ids: this is where the report wants them (code)"],
  "recommendation": "KEEP_RULES",
  "candidates": [{"name": "Keep rules (policy floor under DP-001)", "why": "report §6.2 and §11: literal signals and arithmetic stay in code; guards must stay even if DP-001 adopts a decision model", "confidence": "high"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "not applicable", "auto_resolved_share_est": "not applicable"},
  "risks": ["none"],
  "migration_effort": "S",
  "eval_plan": {"labeled_set": 0, "metrics": [], "shadow_days": 0},
  "rollout": "none",
  "source_confidence": "high",
  "evidence": ["conform.py:113-130 (thresholds, modals, negations)", "docs/graph/build.md:590-602 (guards: numbers, negation, modality, ids, same sentence)"]
}
```

```json
{
  "id": "DP-012",
  "location": "backend/src/graph/core/reader.py (rank fusion); candidate floors in conform.py, hubjudge.py, spec-ontology.yaml",
  "current_impl": {"type": "rules", "model": "embeddings + full text (no LLM)", "prompt_ref": "none", "output_parser": "none"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN", "languages": ["UNKNOWN"], "untrusted_text": true},
  "output": {"type": "ranking", "options": ["ranked hits"], "plus": []},
  "volume": {"per_day": "UNKNOWN", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": true},
  "accuracy_req": {"macro_f1": "UNKNOWN", "fp_cost": "low", "fn_cost": "medium"},
  "audit": {"version_pinning": false, "log_probs": false, "rationale_required": false},
  "data": {"pii": "UNKNOWN", "residency": "UNKNOWN", "zdr_required": "UNKNOWN", "may_leave_network": "UNKNOWN"},
  "regulatory": "none found in repo",
  "labels_available": "93 evaluation queries (top-1 79, top-5 90, MRR 0.904: docs/graph/how-it-works.md:485)",
  "hidden_computation": ["rank arithmetic: code"],
  "recommendation": "KEEP_RULES",
  "candidates": [{"name": "Keep (report §5 lists passage grading as a strong use, but no repo evidence shows a need, and a grader would add a call)", "why": "weights tuned and measured; candidate floors are recall filters upstream of the judges", "confidence": "medium"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "not applicable", "auto_resolved_share_est": "not applicable"},
  "risks": ["revisit only if search quality complaints appear"],
  "migration_effort": "S",
  "eval_plan": {"labeled_set": 93, "metrics": ["MRR", "top-5"], "shadow_days": 0},
  "rollout": "none",
  "source_confidence": "medium",
  "evidence": ["reader.py:72-75 (RRF_K 10, VECTOR_WEIGHT 2.0)", "reader.py:80-81 (vector floor 0.60)", "hubjudge.py:125-127 (PAIR_K, PAIR_FLOOR)", "spec-ontology.yaml:1104-1106, 1171 (min_cosine 0.35, pair_floor 0.55)"]
}
```

```json
{
  "id": "DP-013",
  "location": "backend/src/llm/permissions.py",
  "current_impl": {"type": "rules", "model": "none", "prompt_ref": "none", "output_parser": "none"},
  "inputs": {"modality": ["text"], "avg_tokens": "UNKNOWN", "languages": ["shell"], "untrusted_text": true},
  "output": {"type": "yes/no", "options": ["allow", "deny"], "plus": []},
  "volume": {"per_day": "UNKNOWN", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "UNKNOWN", "p95": "UNKNOWN", "sync": true},
  "accuracy_req": {"macro_f1": "not applicable", "fp_cost": "high (access granted)", "fn_cost": "low"},
  "audit": {"version_pinning": true, "log_probs": false, "rationale_required": true},
  "data": {"pii": false, "residency": "not applicable", "zdr_required": false, "may_leave_network": false},
  "regulatory": "none",
  "labels_available": "not needed",
  "hidden_computation": ["shell parsing and path checks: code"],
  "recommendation": "KEEP_RULES",
  "candidates": [{"name": "Keep rules", "why": "R6: access-granting action; report §3.14 and §11: never the only gate on attacker-writable text. Guard-model quality was not re-benchmarked in the report (not covered by report)", "confidence": "high"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "not applicable", "auto_resolved_share_est": "not applicable"},
  "risks": ["none"],
  "migration_effort": "S",
  "eval_plan": {"labeled_set": 0, "metrics": [], "shadow_days": 0},
  "rollout": "none",
  "source_confidence": "high",
  "evidence": ["permissions.py:35-60 (unsupported shell markers and executables)", "permissions.py:202 (marker check)"]
}
```

```json
{
  "id": "DP-014",
  "location": "backend/src/drive/categories.py; backend/src/file_parsers/formats.py",
  "current_impl": {"type": "rules", "model": "none", "prompt_ref": "none", "output_parser": "none"},
  "inputs": {"modality": ["structured"], "avg_tokens": 0, "languages": ["not applicable"], "untrusted_text": false},
  "output": {"type": "choice", "options": ["12 categories", "processed / stored_only / skipped"], "plus": []},
  "volume": {"per_day": "UNKNOWN", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "microseconds", "p95": "microseconds", "sync": true},
  "accuracy_req": {"macro_f1": "1.0 by definition", "fp_cost": "low", "fn_cost": "low"},
  "audit": {"version_pinning": true, "log_probs": false, "rationale_required": true},
  "data": {"pii": false, "residency": "not applicable", "zdr_required": false, "may_leave_network": false},
  "regulatory": "none",
  "labels_available": "not needed",
  "hidden_computation": [],
  "recommendation": "KEEP_RULES",
  "candidates": [{"name": "Keep rules", "why": "R0: chosen by the Drive MIME type only, never by file name (DATA-GDUI-0001.AC5); zero known misclassifications is the report's exclusion test (§6.2)", "confidence": "high"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "not applicable", "auto_resolved_share_est": "not applicable"},
  "risks": ["none"],
  "migration_effort": "S",
  "eval_plan": {"labeled_set": 0, "metrics": [], "shadow_days": 0},
  "rollout": "none",
  "source_confidence": "high",
  "evidence": ["drive/categories.py:1-12 (category by MIME only)", "drive/categories.py:66-79 (class by category)"]
}
```

```json
{
  "id": "DP-015",
  "location": "backend/src/graph/plan/plan-rules.yaml; graph/plan/rules.py",
  "current_impl": {"type": "rules", "model": "none", "prompt_ref": "none", "output_parser": "none"},
  "inputs": {"modality": ["structured"], "avg_tokens": 0, "languages": ["not applicable"], "untrusted_text": false},
  "output": {"type": "choice", "options": ["area_anchor", "shared", "member", "context", "phase values"], "plus": []},
  "volume": {"per_day": "UNKNOWN", "peak_qps": "UNKNOWN"},
  "latency_budget_ms": {"p50": "milliseconds", "p95": "UNKNOWN", "sync": false},
  "accuracy_req": {"macro_f1": "not applicable", "fp_cost": "medium", "fn_cost": "medium"},
  "audit": {"version_pinning": true, "log_probs": false, "rationale_required": true},
  "data": {"pii": "UNKNOWN", "residency": "not applicable", "zdr_required": false, "may_leave_network": false},
  "regulatory": "none",
  "labels_available": "not needed",
  "hidden_computation": [],
  "recommendation": "KEEP_RULES",
  "candidates": [{"name": "Keep rules", "why": "R0: placement follows graph types and relations, a fixed spec; free text parts (task titles, descriptions) are R3 LLM calls already and not closed decisions", "confidence": "high"}],
  "thresholds": {"auto_accept": "not applicable", "llm_fallback_below": "not applicable", "human_if": "not applicable"},
  "expected_benefit": {"cost_reduction_pct": "0", "p50_latency_ms_before_after": "not applicable", "auto_resolved_share_est": "not applicable"},
  "risks": ["none"],
  "migration_effort": "S",
  "eval_plan": {"labeled_set": 0, "metrics": [], "shadow_days": 0},
  "rollout": "none",
  "source_confidence": "high",
  "evidence": ["plan-rules.yaml:1-40 (types, roles, phases, exclude_all)"]
}
```

---

## 5. Excluded / keep-as-is

| Candidate | Location | Reason |
|---|---|---|
| Hub block consolidation | `hubjudge.py` / `consolidate.py` (`consolidate_hubs.4`, blocks of up to 120 hubs) | Output is a set of id groups over many items, not one closed answer. The underlying pair question is DP-002. Rule R3 style (structured but open-ended). Not covered by report. |
| Node and edge extraction | `graph/build/extract.py`, `calls.py` (`nodes.hubs.14`, `nodes.statements.15`, `edges.8`) | Extracts names, quotes and facts as text: R3, §6.2 "free text or extracted values". |
| Plan generation | `graph/plan/planners.py`, `prompts/*.md` | Free text plans and tasks: R3. |
| Plan review | `graph/plan/review.py` | Advisory free-text issues, changes nothing: R3 and §6.2 (human-read output). |
| Document summarizer and agents | `agent_runtime/summarizer.py`, `temporal/agents`, `llm/agent.py`, `llm/coding.py` | Tool-calling agents and free text: R3. |
| PDF math, Mermaid and transcript enrichers | `file_parsers/pdf/math_enricher.py`, `mermaid_enricher.py`, `transcript/enricher.py` | Generate LaTeX, Mermaid and cleaned text: R3. The `convertible` flag in Mermaid is bundled with generated code. |
| `run_structured_judge` | `backend/src/llm/judge.py` | Library API for acceptance judging; the only callers are `tests/test_judge.py` and `tests/test_package.py:40`. No production decision point. Output includes per-criterion evidence text: R3. |
| Embedding thresholds as candidate generators | `DEDUP_*`, `PAIR_FLOOR`, `min_cosine`, `pair_floor` | They decide which pairs a judge sees (recall). A decision model cannot find pairs it is never shown. Calibrated by replay (`docs/graph/analysis.md:121`, `build.md:589`). See DP-012. |
| Frontend regex and path matching | `frontend/lib/markdown/links.ts`, `components/shell/shell-logic.ts`, `lib/domain/logic/source-link.ts` | Literal URL, extension and route matching: §6.2 and anti-pattern "replacing literal keyword rules". |
| Auth, settings validation, Drive gateway choice | `backend/src/auth/`, `settings.py`, `drive/select.py` | Deterministic policy on structured fields: S12, R0. |
| Retrieval ranking by A/B or bandit | none | S11 patterns have no hits. No reward signal is logged. |
| Fine-tuned classifier for any DP | none | R4a needs 1,000 trustworthy labels and stable criteria. No decision point has them; stored verdicts are LLM outputs. Revisit if DP-001 labeling reaches 2,000 or more. |

Leave alone, in short: all extraction and generation, all quote-bound judges (DP-003 to DP-008), the dedup guards, the permission policy and every literal matcher.

---

## 6. Cross-cutting recommendations

Do these only if DP-001 goes ahead. They cost little and reduce lock-in.

1. **Adapter inside `Gateway`.** Requirement FR-LLM-01 says every model call goes through the one gateway and its limiter (`docs/graph/requirements.md:60`). Today `Gateway.structured` is chat-completions only (`gateway.py:439-455`) against an OpenAI-compatible URL. Add a `decide(...)` method beside it with a neutral internal schema (question key, type `noul`, `choice` or `score`, criteria, always an "other/none" option). Provider adapters map to the System One shape (Jev, Perplexity, Clef, Strands: `/v1/systemone`), OpenAI (`/v1/decisions`) and Gemini (report §11, "ignore wire-format lock-in"). This keeps the limiter, retries, `UsageRecorder` and Langfuse tracing.
2. **Questions as versioned files.** The repo already keeps judge prompts as files with a `name.version` convention (`judge_prompts/*.md`, `dedup_adjudicate.3`). Add `decision_questions/<name>.<version>.yaml` next to them: instructions, criteria text, option order (sorted keys, report §3.8), version. The `name.version` string stays part of the verdict fingerprint.
3. **One log record per decision.** Log input hash, output, per-option probabilities, requested model and **served** model id (report §10 step 8; aliases echo no served version, report §3.7). `DedupMemory.write` stores only `same`, `confidence` and the evidence hash (`conform.py:1000-1010`); extend the stored payload with `probability` and `model_served`. Use `log_event` from `logger.logger` (backend rule).
4. **Unify confidence.** The repo has two scales: `high/medium/low` (dedup, hubs) and a float 0 to 1 (judges, floors 0.7, 0.8, 0.85). A calibrated probability should replace both, with thresholds fitted per question on held-out labels (report §10 step 6).
5. **Shared calibration and eval harness.** Extend `examples/graph_replay.py` and `graph_eval.py` (they already replay a stored step with other settings and report per-prompt calls and tokens) to compute precision, macro-F1, ECE and coverage@precision. One harness for DP-001 and any later question.
6. **Keep the guards.** Guards in DP-011 stay as the deterministic floor under any decision model (report §7 R6, §11).
7. **Rollout switch.** No flag service exists. Add one env setting (documented in `docs/configuration.md`) with three modes like Amazon's case (report §3.8 [V-internal]): current LLM, DUAL (run both, apply the LLM, log disagreements), DECIDER. Amazon rolled back with "a 45-second config change" (report §3.8). Here builds run inside the API process, so a settings change needs an API restart; its cost is `UNKNOWN`.
8. **Fingerprint hygiene.** Store decision-model verdicts under their own fingerprint during shadow, so they never overwrite or mix with LLM verdicts. Changing `prompt|model` expires the stored verdict and re-asks the pair (`docs/graph/build.md:617-618`); plan for that one-time re-judge.

---

## 7. Suggested pilot

**Pilot: DP-001, requirement dedup verdict.**

Why this one: single yes/no output; no quote requirement; short inputs; guards in code already do the arithmetic; verdict storage exists; a replay harness exists; the cost of a wrong answer is bounded by "low means no merge".

Limits to state: it is not the highest-volume point (DP-003 is, but needs quotes). The repo has no human labels for it. The 2,000-record target needs a labeling effort.

### 2,000-record replay plan

**Build the set (step 1).**
- Collect candidate pairs that reach the small model: pairs the guards and the direct-merge rule leave to it (`docs/graph/build.md:610-612`). One first build of the specflow corpus gives 194 (`build.md:991`), so about 10 such builds, or builds of several customer projects, are needed for 2,000 pairs. Number of available projects: `UNKNOWN`.
- Add guard-refused pairs from `dedup_refused` on the conform artifact as hard negatives (numbers, negation, modality, ids).
- Add direct-merge pairs (cosine at least 0.95, Jaccard at least 0.7) as an easy-positive check.
- Stratify: about half the pairs the current LLM called `same`, half `different`; include the `low` band.
- Pull the current verdicts and confidence from the `decisions` store (kind `decisions`, key `<project>|dedup|<a>|<b>`).

**Label (step 2).** A spec owner labels each pair as same commitment or not. The stored verdicts are LLM outputs and are **not** ground truth. Split by report §10 step 2: 1,000 pairs to fit calibration, 1,000 to score.

**Arms (step 3).**
- A: current small LLM, rerun through the conform step of `examples/graph_replay.py` (`graph/build/run.py:1214` selects `DEDUP_PROMPT`).
- B1: pplx-decider v1.2 on the same pairs. B2: Jev `jev-1.13.0`. R4f says benchmark both. Add Microsoft-Decision-1 only if the gateway is Azure (R4e, `UNKNOWN`).
- C: fine-tuned small classifier (ModernBERT or LoRA). **Eligible only if labels reach at least 1,000 for training in addition to the 1,000 calibration and scoring pairs** (R4a, and stable criteria for one quarter, `UNKNOWN`). With 2,000 labels it is not eligible. Report this and skip arm C.

**Question design (step 4).** One question: "Do both statements state the same commitment, where one may be shorter or more detailed, and they do not conflict?" Criteria text: same / different / **other-unsure**. Ask each pair in both orders (a,b and b,a) and average, to test order bias (report §8: first-option bias, key sorting). Keep keys sorted.

**Metrics (step 5).** Precision of merge (primary: a wrong merge costs most), recall, macro-F1, ECE before and after Platt scaling fitted on the calibration half, coverage at precision 0.95, order-swap agreement, p50 and p95 latency, dollars per 1,000 pairs.

**Decision rule (step 6).**
- Fit a threshold on the calibration half. The cascade only pays if the high-confidence band covers at least 40% of pairs at the required precision (R5).
- Required precision: not worse than the measured merge precision of the current path, .93 on the specflow corpus and 1.00 on the stitch corpus (`docs/graph/build.md:588-589`; sample sizes `UNKNOWN`).
- Cost: arithmetic in section 4, DP-001. Expect cents per build, so decide on calibration and latency, not dollars.

**Rollout after a pass.** Shadow (apply LLM, log decision-model verdict, store under a separate fingerprint) for 14 days or 2 first builds plus 3 update builds, whichever is longer; then DUAL compare; then a canary project; then the cascade: decision model above the threshold, current small LLM below it, "no merge" if still `low`.

---

## 8. Questions for the owner

Each item changes at least one recommendation.

1. How many graph builds run per month, per project and in total (first, update, rebuild)? Below about 1,000 judge calls per month with no latency complaint, the report excludes all of this (§6.2).
2. Which models and prices stand behind `GRAPH_LLM_MODEL`, `GRAPH_SMALL_MODEL` and the optional judge model, and who is the gateway vendor and region? Needed for the baseline, for R4d (existing OpenAI contract) and R4e (Azure only).
3. May customer document text leave your network and the current gateway to a second vendor? Is there a ZDR, HIPAA, EU residency or SOC 2 requirement? Decides R4b (self-host) and removes Jev while its SOC 2 and ISO status is unlisted (report §3.1).
4. In what languages are customer specs? Jev is English-first (report §3.1).
5. How many human-labeled items exist for dedup, hub merges and the judges, and where are they? The docs cite precision figures (.93, 1.00, .97) but no sample sizes.
6. Is there an acceptable precision for a wrong merge, and who labels pairs?
7. What share of attach-orphan judge calls return null? Stored inference reports should show it. It decides whether the DP-003 pre-gate has any value.
8. What is the average input size of a dedup call? Not in the docs.
9. Is the 17.2k-token cross-document call size typical for customer projects, or only for the test corpora?
10. Is latency of builds a concern for any customer? Today the repo records no complaint.
11. Can an API restart serve as a rollout switch (builds run in the API process), or is a faster switch needed?
12. Is a vendor model with a silent update path (Microsoft-Decision-1, Perplexity aliases) acceptable given the `prompt|model` fingerprint?

---

## 9. Report caveats that affect this repo

- **Report file missing.** `docs/decision-models-report.md` is not in the repo. This audit used the text pasted in the task. If the file differs from that text, rerun the vendor sections.
- **Recency (report §12).** Every product is 0 to 8 weeks old. Gemini Decision 1 launched 2026-11-11. Prices moved within weeks (Perplexity from $0.04 to $0.02). Prices used here may be stale on the day you read this.
- **Not covered by report, low confidence.** (a) Entity-resolution or merge adjudication of requirement statements (DP-001, DP-002). (b) Tasks that need verbatim quote output (DP-003 to DP-008): the report defines decision models as returning no text (§2). (c) Judge calls with 4.8k to 17.2k input tokens: the report has no benchmark at that size; Strands Decider's window is 4K, and Jev's 32K for state is the stated limit (§3.1, §3.8). (d) Guard-style and permission classification (DP-013): the report says guard models and routers were not re-benchmarked in this cycle (§3.13, §12), so treat any use there as low confidence. (e) Classical decision intelligence and bandits (§3.14, §12): low confidence; no use found.
- **Conflicts to re-verify (report §12).**
  - Jev rate limits: 1,200 req/min [A] vs 80 req/s (beri.net). At concurrency 8 and 0.20 s median the peak could reach 2,400 req/min, above the first figure. Get a committed limit in writing.
  - Jev context: 32K, 64K or 262K by source and header. DP-004 sends about 17.2k tokens per call.
  - Microsoft-Decision-1: GA vs public preview; weights updated continually. Silent drift conflicts with the `prompt|model` fingerprint.
  - Clef hosted price: $0.240 / $0.090 per M vs $0.038. Do not budget Clef until checked.
- **Version visibility.** Perplexity echoes the alias, not the served version (report §3.7). Gemini Decision 1 is on `v1beta`, AI Studio only, no Vertex, no caching or batch (§3.3). A free-tier input may be used to improve Google products.
- **Vendor claims not reproduced (report §12).** Google's lowest calibration error, Liquid's 112-eval claim, Perplexity's 85.71% vs 84.51%, Cloudflare's latency table, Microsoft's speed claim, Amazon's "performed just as well" (no accuracy figure). The Amazon cost figures ($0.0222 vs $0.0005 per ticket, $1.408 per hour) are [V-internal] and were used here only for the self-host arithmetic.
- **Calibration (report §11, §3.1).** Artificial Analysis found accuracy below the stated probability at almost every level; Rafe and Das [A] found an upward shift in Jev probabilities and fixed it with Platt scaling. Treat all decision-model probabilities as uncalibrated until the DP-001 replay shows otherwise.
- **Academic sources are preprints** (arXiv 2609.24052, 2609.24574, 2609.28940, 2608.20371). The cascade claim "a quarter to half of the LLM's cost" (report §4.1 [A], social-science tasks) is not shown for this repo.
- **Leaderboards disagree** (report §11). Decider v1.2 scored 70.5 on Decision Index v2 [S]; GLiDE 72.8; Jev 1.14 67.0. Do not choose by rank.
- **Scale check.** At this repo's measured volumes the decision-model price advantage is worth cents per build (section 4). The report's own advice (§12) is to start with the highest-volume single-label call that has ground truth. Here that is DP-001 with labels still to be made.
