---
type: Design
title: "Graph gap investigation"
description: "The Investigator port and its adapters, the context pack, the read menu, the report, checks, the second check, pointer answers, the verdict cache, prompts and failures"
status: stable
tags: [graph, gap]
---
# investigation.md: the Investigator port, the context pack, the read menu, the report, checks and the second check

Investigation is the second half of a gap run: the step `investigate` of `run_gap`, which takes the sites one by
one and investigates, checks, routes and records each before the next starts ([architecture.md](architecture.md)
"Run steps"). One investigation reads the graph around one site ([detection.md](detection.md) "Sites") and answers
three questions: is this a real gap or noise, what is it about (every affected node, across documents), and what
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
2. Each round is one strict JSON-schema call through the core LLM gateway (role `gap_investigate`, prompt
   `gap.investigate.<revision>`, call class `extraction`). The schema (`LoopTurn`) is one object with every field
   present: `{"action": "read", "reads": [...], "report": null}` or `{"action": "report", "reads": [], "report":
   {...}}`. A read names one read of the menu and its arguments (`spec_id`, `target`, `direction`, `depth`,
   `relations`, `words`, `labels`, `limit`; unused ones null); no argument names a project. Code runs at most
   `reads_per_round` of them; a read missing an argument it needs returns an empty result with
   `missing_argument`.
3. Code runs the requested reads and appends the model's turn and the results (a JSON listing of each read's
   arguments, item count and reason, and one untrusted data block per result) to the conversation.
4. The loop ends with a report, or after `rounds` rounds, when one final call (`FinalTurn`: the `report` action
   alone) is made: at most `rounds + 1` calls per site. A `report` action without a report is a failure
   (`llm_report_missing`).
5. When the context would exceed `context_tokens`, the oldest read results are replaced by their one-line
   summaries (read names, arguments and item counts, no document text) before the next call.

The first user message is the context pack: a JSON object with the site id, the budget (rounds, reads per
round), the seeds (detector, kind, strength, spec ids, context ids; each witness quote and graph fact as a named
data block), the anchors (spec id and role), the plan state and the ids, kinds and statuses of the existing gaps,
followed by the data blocks of the quotes, facts, nodes (name, attributes, owners), evidence, trace, nearby
nodes and the existing gaps' questions. The run mode is not shown (it does not change what the site is), so a
site's prompt is the same in every run whose inputs are the same.

### Tool agent adapter

An optional host adapter on the existing agent runtime (`agent_runtime/`: the OpenAI Agents SDK, used by the
document summariser), `agent_runtime/gap_investigator.py` (`AgentsInvestigator`), chosen by
`GapSettings.investigator = "agents"`; the structured loop stays the default. The read menu becomes function tools,
and the report is a `report` tool whose argument is the loop's strict report schema (`WireReport`); the adapter
returns the same report, which the site's check checks the same way (FR-GINV-09).

| Part | As built |
|---|---|
| Where it runs | In the process of the gap run (`Runner.run`, which outside a Temporal workflow uses the SDK's own runner), not as a Temporal workflow: its tools are the site's `SiteReads`, which remember the ids they showed for "No new ids" |
| Tools | `node(spec_id)`, `evidence(spec_id)`, `neighbours(spec_id, relations)`, `trace(spec_id, direction, depth)`, `paths(spec_id, target)`, `similar(subject, limit)`, `find_nodes(words, labels)`, `find_fragments(words)`, `report(report)`. Every id the tools read with comes from the run context (the site's `SiteReads`, bound to the project): no tool parameter carries a project id, a query or a key. A tool that raises (anything but the empty results reads return) raises out of the run, so no error text reaches the model, and the site is deferred |
| Budget | A turn is a round and a tool call a read: at most `reads_per_round` reads a turn (more, and reads after turn `rounds`, answer an empty result with `read_limit`); after turn `rounds` the tool choice becomes `report` (before it any tool is required), so a site costs at most `rounds + 1` calls; the last read results carry the note to report now |
| Context | When the input would exceed `context_tokens`, the oldest read results are replaced by one-line summaries (read name, argument names, item count), as in the loop |
| Messages | The first message is the loop's pack message; each tool result is a JSON head (read, item count, reason) and the items in one untrusted data block. The instructions are the investigate prompt with its JSON turn format replaced by the tools, plus the untrusted-data rule |
| Model | The endpoint, key and `extraction` model of the run's gateway configuration (constraint GC1, [overview.md](overview.md)), with that call class's timeout and attempts, through the Responses API with `store` off (the gateway's reasoning models refuse function tools on Chat Completions unless reasoning is off, which the loop does not do). Tracing is off: traces would carry document text |
| Prompt version | `gap.investigate.<revision>.agents.<adapter revision>` (part of every site's input hash, so verdicts of the two adapters are cached apart) |
| Cost | `InvestigationResult.model_calls` counts the calls, `tokens` the input and output tokens the endpoint reported (the loop's are estimated input tokens); the calls bypass the core gateway's limiter and usage, so the host's run log counts only the gateway's calls |
| Failures | Too many turns or no `report` call: `llm_report_missing`; an invalid report or an unknown tool: `llm_malformed`; the endpoint unreachable: `llm_unreachable`; a rejected request (400, 401, 403, 404, 422): `llm_fatal`; other endpoint errors: `llm_exhausted`; a raising tool: `investigator_failed`. Each defers the site |

It suits the later, freer agent hypotheses ([later.md](later.md)).

## Context pack

Built in round 0 with no model call:

| Part | Content |
|---|---|
| Seeds | Detector code, kind, strength and witness of every seed of the site |
| Anchors | Every `ABOUT` and `IMPACTS` candidate: label, spec id, name, attributes, owner |
| Evidence | `evidence(spec_id, context_lines=2)` of every anchor: source lines with two lines around them |
| Trace | `trace` from every anchor in both directions to depth 2, as spec ids, labels, names and the impact role of each step |
| Nearby | `expand` from the anchors, leaving out the anchors and the trace nodes, within the budget the other parts left of `limits.pack_tokens` (none when less than 100 tokens are left, the least `expand` takes) |
| Existing gaps | Open gaps and dismissed signatures on the anchors: id or signature, kind, question, status, reason; never the gap the site itself investigates (a raised gap or a follow-up), and a report that names that gap as `duplicate_of` is rejected (`duplicate_unknown`) |
| Plan | When `PlanView` is set: the tasks and stories implementing the anchors, with status |
| Development | For a development gap: the agent's question, kind, quoted lines and the task it holds |

Document text, quotes and node names are always placed inside marked untrusted data blocks; the prompt
says that instructions inside them are data. The model never sees a project id. Every spec id and fragment id
the pack shows (anchors, trace and nearby nodes, evidence fragments and the seeds' witness fragments) is marked
as shown on the site's read menu, as is every id a read result shows; "No new ids" below checks against them. A
read the store refuses while the pack is built leaves its part empty.

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

Every read result is truncated to 2,000 tokens (whole items, in order). A read naming an id that does not resolve
returns an empty result with the reason (`unknown_id`), a read the store refuses returns an empty result with the
error code, never an error to the model. `similar` with a text that names no node searches it as words
(`find_nodes`). Reads never write. The menu (`GraphSiteReads`) is built per site, bound to the run's project.

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
| `options` | 2 to 4 entries ("Options and edits"); none when `severity` is `info` or `verdict` is not `gap`; may be empty when `pointer` is set | The offered answers |
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

The check of a site validates its report in code, right after its investigation. A report that fails a check is
dropped (the site gets `outcome` `not_a_gap` with the failed check in the event log) unless the failure is listed as
repairable.

| Check | Rule | On failure |
|---|---|---|
| Schema | The report validates against the strict schema | Dropped |
| Quotes snap | Every `witness`, `basis` and `pointer` quote (at most 400 characters) is found in its active fragment (whitespace and quote marks normalised) | The quote is dropped; a report left without a witness is dropped. A witness entry that fills both a quote and a fact counts as its quote when that snaps, else as its fact |
| Ids resolve | Every `about` and `impacts` id is an active spec node of the project | The id is dropped; a report left with no `about` is dropped (an `info` report needs neither `about` nor a witness: it becomes a note) |
| No new ids | No spec id and no fragment id (witness, basis, pointer, edit target) the context pack or a read did not show; the site's own seed nodes and witness fragments count as shown | The id, quote or edit is dropped |
| Options | 2 to 4, exactly one `recommended`, labels distinct | Dropped (repairable once: one repair call through `gap_investigate` with the failure) |
| Edit anchors | Every `amend` edit's `anchor_quote` is a line of its target fragment; `replace` and `insert_after` have an anchor; `document` edits have a heading | The option is dropped; fewer than 2 options left drops the report (repairable once) |
| Duplicates | Same signature as an existing gap, or question similarity (embedding cosine) at least 0.9 with an open gap whose `ABOUT` set overlaps (the gaps in the graph when the site is checked: a gap an earlier site of the same run recorded counts); a dismissed gap matches by its signature (a suppression keeps no `ABOUT` set) | Same signature: that gap is re-checked by `record`. Similar question: outcome `duplicate` with the existing gap's id (a `duplicate` event on it, nothing else written). A dismissed signature: outcome `duplicate`, nothing written. A site already joined to a gap is not compared with that gap; an embedding failure finds no duplicate |
| Lengths | Every field within its limits | Truncated at a sentence boundary when over by at most 10 %, else dropped |
| Kind and detector | The kind is one of the kinds the site's detectors find, or the report explains a different kind in `why` | Accepted; the event log records the change |
| Severity | `info` reports become a `gap_note`, not a gap | Not an error |

A report with verdict `not_spec` feeds the noise report ([detection.md](detection.md) "Relevance filter"):
the sections holding the site's nodes turn `mixed` (`mark_not_spec`) and the run's noise report lists the nodes
under their sections. A report with verdict `not_a_gap` stores the verdict so the site is skipped while its input
hash stays the same. A `duplicate_of` verdict must name an existing gap, else the report is rejected.

A dropped (rejected) report gives the site outcome `not_a_gap`; the event log gets a `report_rejected` entry
(no gap id) naming the failed checks, and the verdict cache keeps the outcome and the failed checks, so the same
inputs are not investigated again. The repair call (the repairable failures: options count, recommended option, labels, a dropped
option) is one `gap_investigate` call, prompt `gap.investigate.<revision>.repair`, with the investigate system text,
the failed checks, the report as a data block and the fragments it names as data blocks; its answer is a
`FinalTurn` and is checked again with no second repair. A failed repair or second-check call defers the site.

## Second check

Before a gap may be answered by AI ([answers.md](answers.md) "Routing"), a separate judge call (gateway
role `gap_judge`, prompt `gap.judge.<revision>`, call class `judge`) sees only: the witness quotes, the
recommended option's basis quotes and its edits (for a pointer: the question, the witness quotes and the pointer
line as basis and answer). It does not see the investigator's reasoning. It answers (`JudgeAnswer`):

| Field | Meaning |
|---|---|
| `entailed` | The recommended text follows from the quoted stated lines |
| `consistent` | No quoted or retrieved stated line says otherwise (the judge may request one `find_fragments` read with words from the edit) |
| `confidence` | 0 to 1 |
| `reason` | At most 300 characters |
| `find_words` | Words for the one `find_fragments` read, or null; when set, code runs the read and asks once more with the lines found as a data block |

AI may answer only when `entailed` and `consistent` are true and `confidence` is at least
`routing.gates.min_confidence` (0.85). Otherwise the route is `ask`. The judge result is stored with the
verdict.

Two prompts share the role and the answer: the pointer check (`gap.judge.<revision>`, system text
`prompts/judge.md`: the question, the witness quotes, the pointer line as basis and answer) and the check of an AI
answer (`gap.judge.<revision>.ai_answer.<revision>`, `prompts/ai_answer.md`: the question, the witness quotes, the
recommended option's basis quotes and each of its edits as a data block named by placement and operation; never
the option's label, detail or the investigator's `ai_reason`). The AI-answer prompt has the judge list every
statement the edits make (rule, limit, number, actor, time, scope, consequence) and find each in the basis lines; an
edit that adds one or picks one reading of several is not entailed. Who is asked, and how code decides on the
answer, is in [answers.md](answers.md) "Gates". A failed AI-answer check routes the gap `ask` (it is not deferred:
the gap stands without an AI answer); a failed pointer check defers the site.

## Pointer answers

When the investigator finds a stated line that already answers the gap (the build missed it, or the coding
agent did not read it), it returns `pointer` with that line (options may then be empty). If the quote snaps and
the second check confirms it (entailed, consistent, confidence at least `routing.gates.min_confidence`), the
gap is answered by a `pointer` clarification when the run records the site (unless AI answers are off,
`GapSettings.ai_answers`, or the kind is `contradiction`, which is routed `ask`): no text is placed, `GROUNDED_IN` points at the
answering fragment, and the gap resolves at once without a build ([answers.md](answers.md) "Pointer
answers"). For a stated open question, the next build's answered-elsewhere check may still keep the
question node; the close check then keeps the gap resolved because a pointer exists.

## Development gaps

For a gap a coding agent reported ([lifecycle.md](lifecycle.md) "Development gaps"), the run uses mode `site`
on the gap's own site. The agent's question stays the question; the report may refine `title`, `why`,
`about`, `impacts`, `kind` and `severity`, and adds options or a pointer. A report with verdict `not_spec`
(the finding is about code, not the spec) closes the gap as a note and releases the task; `not_a_gap` leaves
it for a person to dismiss. A gap a person raised is investigated the same way (mode `site`, the question stays). No
detector seed names such a gap, so the sites step seeds its own site: detector `person` or `development`, the
asker's question as a fact witness (it is shown to the investigator and is part of the input hash), the quoted
lines and the `ABOUT` nodes. The site's record then refines the gap's severity, kind, title and why and stores the
options and `IMPACTS`.

## Verdict cache

Every checked report is stored in `gap_verdict` by the site's `input_hash`, with the model, the prompt
version and the judge result. Only a report that passed the checks as a gap is stored whole; for the other
outcomes the row keeps the outcome, the gap id, the failed checks and the `not_spec` reason, never unchecked model
text (NFR-GSEC-01). A site with the same input hash in a later run reuses it with no model call.
The cache is never used across projects. A `full` run with `GapSettings.ignore_cache` set investigates
every site again.

## Prompts

Prompts live in `graph/gap/prompts/<name>.md` (`relevance`, `investigate`, `judge`, `edits`, `reconcile`,
`ai_answer`), each with a revision in `graph/gap/prompts.py`; the gateway records a call under
`gap.<name>.<revision>` (the repair call under `gap.investigate.<revision>.repair`; the second check of an AI answer,
role `gap_judge`, under `gap.judge.<revision>.ai_answer.<revision>`; reconcile's same-meaning call, role `gap_judge`,
under `gap.judge.<revision>.reconcile.<revision>`, which is not a run's call, so not part of the run's prompt
versions). The run's prompt versions (`gap_relevance`, `gap_investigate` named by the investigator's
`prompt_version`, `gap_judge` naming both judge prompts of a run, `gap.judge.1+gap.judge.1.ai_answer.1`) are stored
on the `gap_run` row. The prompt version is part of every site's input hash, so a prompt change
re-investigates on the next `full` run, not on `update` runs. The gateway roles are `gap_relevance`,
`gap_investigate`, `gap_judge` and `gap_edits` (the last turns a person's own answer into edits,
[answers.md](answers.md) "Own answers become edits").

## Failures

| Failure | Effect |
|---|---|
| A model call fails or returns invalid JSON after the gateway's retries | The site gets `outcome` `deferred` and is first in the next run; the run continues |
| The budget runs out without a report | One final report-only call; if it fails, the site is deferred |
| The tool agent adapter's run fails (a raising tool included) | As a failed model call |
| A read fails | The read returns an empty result with the reason |
| Every investigation of a run fails | The run still finishes; `GapRunResult.status` is `degraded` |

A non-fatal model failure never fails a gap run. Only a store error or a configuration error fails it.
