---
type: Design
title: "Graph gap investigation"
description: "The Investigator port and its adapters, the context pack, the read menu, the report, checks, the second check, pointer answers, the verdict cache, prompts and failures"
status: draft
tags: [graph, gap]
---
# investigation.md: the Investigator port, the context pack, the read menu, the report, checks and the second check

Investigation is the second half of a gap run: the steps `investigate` and `check` of `run_gap`. One
investigation reads the graph around one site ([detection.md](detection.md) "Sites") and answers three
questions: is this a real gap or noise, what is it about (every affected node, across documents), and what
are the reasonable answers. Code then checks the report before anything is written.

## Investigator port

`graph.gap` does not depend on an agent framework. The investigation is behind a port:

```python
class Investigator(Protocol):
    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult: ...
```

| Model | Fields |
|---|---|
| `SitePack` | `project_id`, `site_id`, `input_hash`, `mode`, the seeds (detector, kind, strength, spec ids, witness), the context pack ("Context pack"), the open and dismissed gaps on the candidates, the plan state of the candidates (or none), for a development gap the agent's question, kind and quoted lines |
| `SiteReads` | The read menu ("Read menu"), bound to the project and the run; never takes a project id from a caller |
| `SiteBudget` | `rounds` (`limits.rounds_per_site`), `reads_per_round` (`limits.reads_per_round`), `context_tokens` (`limits.context_tokens`) |
| `InvestigationResult` | `report` (the report, or none), `rounds_used`, `reads` (the reads made, for the event log), `model_calls`, `tokens`, `error` (a failure code, or none) |

Both adapters see the same pack, the same reads and the same budget, and return the same report, which code
checks the same way ("Checks"). `GapContext.investigator` holds the adapter; `GapSettings.investigator`
names it (`loop`, the structured loop below, by default; `agents` for the host adapter).

### Structured loop adapter

The default, shipped in `graph.gap`. It runs in process like build and plan, its cost is exact, and its
verdicts are cached per site.

1. Round 0 builds the context pack with no model call.
2. Each round is one strict JSON-schema call through the core LLM gateway (role `gap_investigate`). The
   schema is a union: `{"action": "read", "reads": [...]}` with at most `reads_per_round` reads, or
   `{"action": "report", "report": {...}}`.
3. Code runs the requested reads and appends their results as untrusted data blocks.
4. The loop ends with a report, or after `rounds` rounds, when one final call with the `report` action
   alone is made.
5. When the context would exceed `context_tokens`, the oldest read results are replaced by their one-line
   summaries before the next call.

### Tool agent adapter

An optional host adapter on the existing Temporal agent runtime (`agent_runtime/`, `temporal/agents/`: the
OpenAI Agents SDK with function tools through the LLM gateway model provider, used today by the document
summariser). The read menu becomes function tools. As in the summariser, every id the tools send comes
from the run context the workflow builds from its own input: no tool parameter carries a project id, a
query or a key, and a failed tool raises out of the run instead of returning error text to the model. The
adapter enforces the same budget (turns map to rounds, tool calls to reads) and ends with a `report` tool
whose argument is the report schema. It suits the later, freer agent hypotheses ([later.md](later.md));
the structured loop stays the default. Every model call still goes through the host's LLM gateway
configuration (constraint GC1, [overview.md](overview.md)).

## Context pack

Built in round 0 with no model call:

| Part | Content |
|---|---|
| Seeds | Detector code, kind, strength and witness of every seed of the site |
| Anchors | Every `ABOUT` and `IMPACTS` candidate: label, spec id, name, attributes, owner |
| Evidence | `evidence(spec_id, context_lines=2)` of every anchor: source lines with two lines around them |
| Trace | `trace` from every anchor in both directions to depth 2, as spec ids, labels, names and the impact role of each step |
| Nearby | `expand` from the anchors within `limits.pack_tokens` tokens |
| Existing gaps | Open gaps and dismissed signatures on the anchors: id or signature, kind, question, status, reason |
| Plan | When `PlanView` is set: the tasks and stories implementing the anchors, with status |
| Development | For a development gap: the agent's question, kind, quoted lines and the task it holds |

Document text, quotes and node names are always placed inside marked untrusted data blocks; the prompt
says that instructions inside them are data. The model never sees a project id.

## Read menu

| Read | Arguments | Core read | Returns |
|---|---|---|---|
| `node` | spec id | `GraphReader.node` | Label, name, attributes, owner |
| `evidence` | spec id | `GraphReader.evidence` | Source lines with context |
| `neighbours` | spec id, optional relation names | `GraphReader.neighbours` | Adjacent nodes with relations |
| `trace` | spec id, direction, depth (at most 3) | `GraphReader.trace` | The directed impact walk |
| `paths` | two spec ids | `GraphReader.paths` | Whether and how the two are connected (at most 4 hops) |
| `similar` | spec id or text, limit (at most 10) | `GraphReader.similar` | Near statements that may conflict |
| `find_nodes` | words, optional labels | `GraphReader.find_nodes` | Nodes whose name or text match |
| `find_fragments` | words | `GraphReader.find_fragments` | Fragments whose text matches, with line ranges |
| `report` | the report | none | Ends the investigation |

Every read result is truncated to 2,000 tokens. A read naming an id that does not resolve returns an empty
result with the reason, never an error to the model. Reads never write.

## Report

The report is the investigator's whole output; its JSON schema is strict (no extra fields).

| Field | Type and limits | Meaning |
|---|---|---|
| `verdict` | `gap`, `not_a_gap`, `not_spec`, `duplicate_of` | Whether the site is a real gap |
| `duplicate_of` | gap id, when `verdict` is `duplicate_of` | The existing gap it repeats |
| `kind` | a `gap_kind` value | Required when `verdict` is `gap` |
| `severity` | `blocker`, `major`, `minor`, `info` | Proposed severity ([answers.md](answers.md) "Severity") |
| `title` | text, 8 to 160 characters | One line naming the gap |
| `question` | text, 10 to 1,000 characters, ends with "?" | The question a person answers |
| `why` | text, at most 2,000 characters | Why it is a gap and what depends on it |
| `about` | 1 to 20 spec ids | Nodes the problem is in |
| `impacts` | 0 to 20 spec ids | Nodes an answer would also change |
| `witness` | 1 to 6 entries: `fragment_id`, `quote` (at most 400 characters), or `fact` (a detector graph fact by its seed index) | The evidence |
| `options` | 2 to 4 entries ("Options and edits"); none when `severity` is `info` or `verdict` is not `gap` | The offered answers |
| `pointer` | optional: `fragment_id`, `quote` | The line that already answers the gap ("Pointer answers") |
| `ai_answerable` | boolean | The model's view whether AI may answer; routing decides ([answers.md](answers.md) "Routing") |
| `ai_reason` | text, at most 500 characters | Why |
| `not_spec_reason` | text, at most 300 characters, when `verdict` is `not_spec` | What the site is instead (release item, ticket, meeting note) |

Example:

```json
{
  "verdict": "gap",
  "kind": "open_question",
  "severity": "major",
  "title": "Cancelling a paid order is not specified",
  "question": "Can a customer cancel an order after paying, and what happens to the payment?",
  "why": "Payment is captured when an order is placed and a refund interface exists, but no text says when cancellation is allowed.",
  "about": ["QST::CANCEL_PAID_ORDER", "REQ::FR::PLACE_ORDER"],
  "impacts": ["FEAT::PLACE_ORDER", "IFC::PAYMENTS_REFUND"],
  "witness": [{"fragment_id": "FRG::...", "quote": "Can a customer cancel a paid order?"}],
  "options": [
    {"label": "Allow within 30 minutes, full refund", "recommended": true,
     "detail": "Uses the existing refund interface; adds one requirement under Place order.",
     "edits": [{"placement": "amend",
                "target": {"fragment_id": "FRG::...", "anchor_quote": "Can a customer cancel a paid order?"},
                "operation": "replace",
                "text": "A customer can cancel a paid order within 30 minutes of payment. The full amount is refunded through the Payments refund API."}],
     "basis": [{"fragment_id": "FRG::...", "quote": "Refunds are issued through the Payments refund API."}]},
    {"label": "Not allowed after payment", "recommended": false, "detail": "...", "edits": ["..."], "basis": []},
    {"label": "Allowed until shipment, minus a fee", "recommended": false, "detail": "...", "edits": ["..."], "basis": []}
  ],
  "ai_answerable": false,
  "ai_reason": "Changes customer-visible behaviour; not entailed by the text."
}
```

### Options and edits

| Field | Type and limits | Meaning |
|---|---|---|
| `label` | text, 3 to 120 characters | What the card shows |
| `detail` | text, at most 600 characters | Consequences of the option |
| `recommended` | boolean | Exactly one option is true |
| `edits` | 1 to 6 edits | Where the text goes |
| `basis` | 0 to 6 quotes: `fragment_id`, `quote` | Stated lines the option relies on |

| Edit field | Values | Meaning |
|---|---|---|
| `placement` | `amend`, `document` | Amend a source section, or add a section to the clarifications document ([answers.md](answers.md) "Placement rules") |
| `target` | `fragment_id` and `anchor_quote` (a verbatim line of that fragment) for `amend`; for `document`, a heading naming the parts it is about | Where the edit applies |
| `operation` | `insert_after`, `replace`, `append_section` | Insert after the anchor line, replace the anchor line, or add a section (always for `document`) |
| `text` | at most 2,000 characters | The text to place: short, self-contained, naming the items it is about by name and id |

One option may carry several edits: a contradiction between two documents replaces the losing line in one
and leaves the other unchanged; a naming fix may edit three documents.

## Checks

The step `check` validates every report in code. A report that fails a check is dropped (the site gets
`outcome` `not_a_gap` with the failed check in the event log) unless the failure is listed as repairable.

| Check | Rule | On failure |
|---|---|---|
| Schema | The report validates against the strict schema | Dropped |
| Quotes snap | Every `witness`, `basis` and `pointer` quote is found in its fragment (whitespace and quote marks normalised) | The quote is dropped; a report left without a witness is dropped |
| Ids resolve | Every `about` and `impacts` id is an active spec node of the project | The id is dropped; a report left with no `about` is dropped |
| No new ids | No id the context pack or a read did not show | The id is dropped |
| Options | 2 to 4, exactly one `recommended`, labels distinct | Dropped (repairable once: one repair call through `gap_investigate` with the failure) |
| Edit anchors | Every `amend` edit's `anchor_quote` is a line of its target fragment; `replace` and `insert_after` have an anchor; `document` edits have a heading | The option is dropped; fewer than 2 options left drops the report (repairable once) |
| Duplicates | Same signature as an existing gap, or question similarity (embedding cosine) at least 0.9 with an open or dismissed gap whose `ABOUT` set overlaps | Becomes `duplicate_of`: the seeds attach to the existing gap; a match with a dismissed signature is dropped |
| Lengths | Every field within its limits | Truncated at a sentence boundary when over by at most 10 %, else dropped |
| Kind and detector | The kind is one of the kinds the site's detectors find, or the report explains a different kind in `why` | Accepted; the event log records the change |
| Severity | `info` reports become a `gap_note`, not a gap | Not an error |

A report with verdict `not_spec` feeds the noise report ([detection.md](detection.md) "Relevance filter").
A report with verdict `not_a_gap` stores the verdict so the site is skipped while its input hash stays the
same.

## Second check

Before a gap may be answered by AI ([answers.md](answers.md) "Routing"), a separate judge call (gateway
role `gap_judge`) sees only: the witness quotes, the recommended option's basis quotes and its edits. It
does not see the investigator's reasoning. It answers:

| Field | Meaning |
|---|---|
| `entailed` | The recommended text follows from the quoted stated lines |
| `consistent` | No quoted or retrieved stated line says otherwise (the judge may request one `find_fragments` read with words from the edit) |
| `confidence` | 0 to 1 |
| `reason` | At most 300 characters |

AI may answer only when `entailed` and `consistent` are true and `confidence` is at least
`routing.gates.min_confidence` (0.85). Otherwise the route is `ask`. The judge result is stored with the
verdict.

## Pointer answers

When the investigator finds a stated line that already answers the gap (the build missed it, or the coding
agent did not read it), it returns `pointer` with that line. If the quote snaps and the second check confirms
`entailed`, the gap is answered by a `pointer` clarification: no text is placed, `GROUNDED_IN` points at the
answering fragment, and the gap resolves at once without a build ([answers.md](answers.md) "Pointer
answers"). For a stated open question, the next build's answered-elsewhere check may still keep the
question node; the close check then keeps the gap resolved because a pointer exists.

## Development gaps

For a gap a coding agent reported ([lifecycle.md](lifecycle.md) "Development gaps"), the run uses mode `site`
on the gap's own site. The agent's question stays the question; the report may refine `title`, `why`,
`about`, `impacts`, `kind` and `severity`, and adds options or a pointer. A report with verdict `not_spec`
(the finding is about code, not the spec) closes the gap as a note and releases the task; `not_a_gap` leaves
it for a person to dismiss.

## Verdict cache

Every checked report is stored in `gap_verdict` by the site's `input_hash`, with the model, the prompt
version and the judge result. A site with the same input hash in a later run reuses it with no model call.
The cache is never used across projects. A `full` run with `GapSettings.ignore_cache` set investigates
every site again.

## Prompts

Prompts live in `graph/gap/prompts/<name>.md` (`relevance`, `investigate`, `judge`, `edits`), each with a
version in `graph/gap/prompts.py`. The prompt version is part of every site's input hash, so a prompt change
re-investigates on the next `full` run, not on `update` runs. The gateway roles are `gap_relevance`,
`gap_investigate`, `gap_judge` and `gap_edits` (the last turns a person's own answer into edits,
[answers.md](answers.md) "Own answers become edits").

## Failures

| Failure | Effect |
|---|---|
| A model call fails or returns invalid JSON after the gateway's retries | The site gets `outcome` `deferred` and is first in the next run; the run continues |
| The budget runs out without a report | One final report-only call; if it fails, the site is deferred |
| The tool agent adapter's workflow fails | As a failed model call |
| A read fails | The read returns an empty result with the reason |
| Every investigation of a run fails | The run still finishes; `GapRunResult.status` is `degraded` |

A non-fatal model failure never fails a gap run. Only a store error or a configuration error fails it.
