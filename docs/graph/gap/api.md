---
type: API
title: "Graph gap API"
description: "Public API of graph.gap: setup, gap runs, the gap service, reconcile, models, ports, the host runner, web routes, MCP tools and the user interface"
status: draft
tags: [graph, gap]
---
# api.md: public API of graph.gap: setup, gap runs, service calls, reconcile, models, ports, host runner, web routes, MCP tools and user interface

Every name below is exported from the `graph.gap` package root. All calls are async unless marked sync,
take the project id first, and filter every query on it.

## Setup

| Call | Returns | Notes |
|---|---|---|
| `load_gap_rules(path: Path \| None = None, *, ontology: Ontology) -> GapRules` (sync) | The validated rules | Shipped file when `path` is `None`; every problem in one `ConfigurationError` ([ontology.md](ontology.md) "Rules file") |
| `ensure_gap_schema(source, ontology, llm_config, database=None, *, records=None, project_ids=()) -> tuple[str, ...]` | The names of objects it created | The store's `ensure_schema` with the gap DDL (`ddl/gap_schema.cypher`, found by the store), like `ensure_plan_schema`: creates the `graph_gap_*` constraints and indexes of [data-model.md](data-model.md); idempotent (a second call returns `()`); then runs the migration of first-slice findings (`migrate_first_slice`) for each of `project_ids` that still has `Finding` nodes (no gap query reads across projects, so the host passes its project ids; without `records` the migration is skipped and logged) ([lifecycle.md](lifecycle.md) "Migration of first-slice findings"). The host runner calls it once per process with every project of the application database |

`load_ontology()` merges the gap ontology file when `graph.gap` is installed; nothing else is needed.

## Gap runs

```python
async def run_gap(
    ctx: GapContext,
    *,
    mode: Literal["update", "full", "site"] = "update",
    site_ids: Sequence[str] = (),       # mode "site": gap ids or seed keys to investigate now
) -> GapRunResult

async def estimate_gap(ctx: GapContext, *, mode: Literal["update", "full"] = "update") -> GapEstimate
```

`run_gap` runs the steps of [architecture.md](architecture.md) "Run steps". Mode `update` scopes
investigation to sites touched by `ctx.changes`; `full` investigates every site up to the full-run limit;
`site` skips scope and the close check and runs the other steps only for the named sites (used by
`raise_gap` and development gaps). A run never raises for a model failure: a site whose investigation
failed is recorded as `deferred`. It raises `ConfigurationError` for invalid rules or settings and lets
store errors through.

`estimate_gap` runs scope, relevance (rules only) and detection, and returns the number of sites that
would be investigated, the cached ones, and the expected model calls and tokens per gateway role. It makes
no model call and writes nothing.

`RUN_STEPS` names the steps in order. The run artifact store is `GapContext.store`, a `GapRunStore`:

```python
class GapRunStore(Protocol):
    async def create_run(self, record: GapRunRecord) -> None: ...          # run_exists when the id is taken
    async def get_run(self, project_id: str, run_id: str) -> GapRunRecord | None: ...
    async def latest_run(self, project_id: str, *, finished_only: bool = False) -> GapRunRecord | None: ...
    async def put_progress(self, project_id: str, run_id: str, step: str, state: StepState) -> None: ...
    async def put_artifact(self, project_id: str, run_id: str, step: str, payload: Mapping[str, Any]) -> None: ...
    async def get_artifact(self, project_id: str, run_id: str, step: str) -> Mapping[str, Any] | None: ...
    async def finish_run(self, project_id: str, result: GapRunResult) -> None: ...  # sets finished_at and status
    async def get_result(self, project_id: str, run_id: str) -> GapRunResult | None: ...
```

`open_gap_run_store(root)` returns the file store (`<root>/<project_id>/gap-runs/<run_id>/run.json.gz`, one
`<step>.json.gz` per finished step, `result.json.gz`; gzip JSON written atomically); `MemoryGapRunStore` has the
same semantics in memory. A `GapRunRecord` holds `project_id`, `run_id`, `mode` (the effective mode: `full` for an
`update` without a finished earlier run), `build_id` (`ctx.build.build_id`, else the last change report's),
`started_at`, `finished_at`, `status`, `progress` (step to `pending`, `running`, `done` or `failed`) and `site_ids`
(of a `site` run, for its resume); `finished` is true once `finished_at` is set.

How a run uses it:
- A run without `ctx.run_id` starts a new run (id `gaprun-<UTC instant>-<6 hex>`) and refuses with
  `ConfigurationError("unfinished_gap_run", run_id=...)` while the project has an unfinished run: the host resumes
  that run by passing its id. A `ctx.run_id` the store does not know starts a new run with that id; a finished one
  returns its stored result.
- Each step reads the artifacts of the steps before it and writes its own before it is marked `done`, so a resumed
  run continues at its first step not `done` (the graph snapshot is read again; seeds, sites, reports and routes
  are not computed again).
- A `ConfigurationError` or `ConformanceError` raised by a step (an invalid rule, a missing artifact, a port's
  refusal) finishes the run `failed` (result, store and `gap_run` row) and is raised again, so it never blocks the
  project's later runs; any other error (a store that is down) leaves the run resumable.
- `start_run` writes the `gap_run` row at the start; `finish_run` its counts, cost and status at the end.
- The sites the last finished run deferred come first (`deferred_first`); limits are
  `settings.effective_limits(rules.limits)`; `settings.ignore_cache` investigates sites with a cached verdict too.
- Cost per gateway role comes from the steps: the relevance step's calls and tokens, the investigations' (the
  `InvestigationResult` counts), and the check step's repair calls (`gap_investigate`) and second-check calls
  (`gap_judge`); `gap_edits` is a service call, never a run's. Embedding calls of the duplicate check are not a
  gateway role and are not in the cost.

Investigation ([investigation.md](investigation.md)): the step `investigate` builds each site's context pack
(`steps/pack.py`) and its read menu `GraphSiteReads` (`steps/reads.py`, bound to the project and the run) and calls
`ctx.investigator`; the host passes `LoopInvestigator(gateway)` (exported), the structured loop over the run's
gateway. The step's artifact keeps, per site, the `InvestigationResult` and the spec ids and fragment ids the pack and
the reads showed (the check step's "No new ids"). `check` validates every report, repairs it once when only its
options failed, finds duplicates and runs the second check of a pointer and of an AI-answer candidate; `route`
applies [answers.md](answers.md) "Severity", "Routing", "Gates" and "The valve" and holds back the new `ask` gaps
beyond `limits.new_questions_per_run`; `record` writes the gaps, their option rows, pointer clarifications and AI
answers (a `pending` clarification of mode `ai` and its `pending` amendment rows, through `ctx.amendments`). The step `close` is the close check ([lifecycle.md](lifecycle.md) "Close check"; `steps/close.py`):
it settles `applying` gaps, takes out obsolete open gaps and deletes stale `CITES`, and the follow-up gaps it opens
get a site in the same run (`SiteRun.follow_ups`). Its counts are the run metric `close` (`resolved`, `follow_ups`,
`waiting`, `obsolete`, `cites_removed`). An `info` report is a `gap_note` (site
outcome `not_a_gap`). Every checked site's report is cached in `gap_verdict` under its input hash by the `record`
step (a rejected report as its outcome and failed checks), so the next run with the same inputs skips it.

The step `record` writes each gap with `ABOUT`, `IMPACTS` and `CITES` (`quote`, `line_start`, `line_end`,
`quote_hash`) in one owned-writer transaction; relations carry `added_by_run`. Keys: the gap node's `uuid` is uuid5
of the project and gap id; edge keys are `<gap_id>|ABOUT|<spec_id>`, `<gap_id>|IMPACTS|<spec_id>` and
`<gap_id>|CITES|<fragment_id>|<16 hex of the quote hash>`. The gap's signature is that of the report: the primary
seed's detector, the report's `ABOUT` ids and its witness (a fact named by seed index counts every fact of that
seed); for a one-seed site it equals the seed's signature. A report whose signature is a gap's in the graph, or a
site joined to an existing gap, re-checks that gap: an open gap gets the relations it lacks (and a new
`updated_at` when it got any), and a `rechecked` event is logged; nothing is duplicated.

## Models

| Model | Fields |
|---|---|
| `GapContext` | `holds: StoryHolds \| None` (the plan's hold calls: the run holds the tasks of open blocker gaps it records and releases the holds of the gaps it ends; none without a plan), `project_id`, `executor` (core store executor), `reader` (`GraphReader`), `gateway` (core `Gateway`), `ontology`, `rules` (`GapRules`), `records` (`GapRecords`), `amendments` (`Amendments`), `investigator` (`Investigator`), `changes: tuple[GapChangeReport, ...]`, `build: BuildFacts` (the dedup refusals and cycle cuts of the build the run follows), `plan: PlanView \| None`, `settings: GapSettings`, `store` (run artifact store, for resume), `run_id: str \| None`, `on_progress` (optional callback of step name, done, total) |
| `GapSettings` | `enabled` (default `True`), `rules_path` (`None`: shipped rules), `investigator` (`"loop"` or `"agents"`, default `"loop"`), `auto_run_after_build` (default `True`), `ai_answers` (default `True`), `limits` (optional overrides of the rules' `limits`, by name), `apply_quiet_seconds` (overrides the rules' value), `ignore_cache` (default `False`: a `full` run reuses cached verdicts), `reconcile` (`ReconcileSettings`: `anchor_similarity` 0.7, `section_similarity` 0.6, `judge_min_confidence` 0.7, [reconcile.md](reconcile.md)). `gap_settings_from_env` maps the `GAP_*` variables of [architecture.md](architecture.md) "Settings"; `effective_limits` applies the overrides to the rules' limits |
| `GapChangeReport` | The gap-side copy of one build change report: `build_id`, `added`, `removed`, `merged` (old id to new id), `rekeyed` (old id to new id), `changed_meaning`, `changed_links`, `changed_evidence` (spec id tuples), `fragments_changed` (fragment ids by lineage), `documents` (lineage to revision id). The host converts the build's report; `graph.gap` never imports `graph.build` |
| `BuildFacts` | `build_id`, `refused_pairs` (`RefusedPair`: `first_spec_id`, `second_spec_id`, `label`, `guard`: the build result's dedup refusals) and `cut_edges` (`CutEdge`: `relation`, `source_spec_id`, `target_spec_id`, `fragment_ids`: the build result's quarantined `cycle_broken` edges). The host converts them from the build result; CT-VALUE and IN-CYCLE read them ([detection.md](detection.md) "Detector catalogue") |
| `GapRunResult` | `run_id`, `mode`, `build_id`, `status` (`completed`, `degraded` when every investigation failed, `failed`), `counts` (the run metrics of [detection.md](detection.md) "Run metrics": seeds by detector, sites investigated, cached, deferred, gaps new, follow-ups, AI answered, resolved, obsolete, notes), `cost` (calls and tokens per gateway role), `noise` (`NoiseReport`), `deferred_sites`, `held_tasks`, `warnings` |
| `NoiseReport` | Per section: lineage, heading path, relevance, signals, how it was decided (`rules`, `model`, `person`), and the node ids it removed from structural seeds |
| `GapEstimate` | `sites`, `cached`, `calls` and `tokens` per role (expected investigation calls are the loop's bound: `rounds_per_site` plus the forced report call per site, each of at most `context_tokens`) |
| `GapRunRecord` | The run store's record of one run ("Gap runs" above) |
| `GapFinding`, `GapFindingPage` | What `GapService.findings` returns: an open gap as the plan's `FindingLike` (`finding_id`, `kind`, `text`, `blocking`, `about_spec_ids`, `story_id`, `created_by`, `created_at`, `orphaned`) and the page of them |
| `GapView` | What reads return: the gap's properties (`held_back` true while the gap is a new question its run held back from the inbox's default view; `story_id` of a development gap; `aliases`, the other ids it answers to), `about` and `impacts` (id, type, name, `aliases`: the node's earlier spec ids, so a plan task naming one still meets the gap), `witness` (`WitnessView`: fragment id, quote, lines, `document_id` (the corpus lineage of the cited fragment); from `get_gap` also `document_title`, `document_path` and the section's `heading_path`), `options` (from `gap_option`), `clarification` (current one), `history` (events, follow-up chain), `reach` (stories and tasks found by trace and the plan, computed at read time) |
| `AnswerInput` | `gap_id`, `option_rank` or `own_text` (exactly one), `placement` (`amend`, `document`; default from the option's edits), `edits` (optional: edits confirmed from `preview_edits`), `answered_by`, `updated_at` (the gap's as the caller read it; None skips the stale check) |
| `AnswerResult`, `ApplyResult`, `EditPreview` (with `SectionPreview`), `RaisedGap` | What `answer_gap`, `apply_now`, `preview_edits` and `raise_gap` return ("Gap service" below) |
| `AiAnswerList`, `AiAnswer` | The review list: per AI or pointer clarification, `gap_id`, `title`, `question`, `gap_status`, `clarification` (`ClarificationView`) and `follow_up` (the gap a revert opened) |
| `DocumentAnswers` | What `document_answers` returns: `lineages` (`LineageAnswers`: `lineage`, `answers`), `clarifications_lineage` and `clarifications` (the sections of the generated clarifications document); each answer a `DocumentAnswer` (`clr_id`, `gap_id`, `title` of the gap, `status`: the rows' `active` or `undecided`, for the clarifications document the clarification's `applied` or `pending`; `amendments`, its rows in that lineage) |
| `RevertResult` | What `revert_clarification` returns: `clr_id`, `gap_id` (the gap a person answers next: the follow-up, or the gap itself back `open`), `follow_up`, `amendments` (reverted), `build` (the build request's answer, None when no text left the corpus) |
| `NewGap` | `kind`, `question`, `about` (spec ids), `quotes` (fragment id and quote, optional), `severity` (optional), `origin` (`person` or `development`), `blocking`, `story_id` and `claim_token` (development), `created_by`, `refs` (`SourceRef`: `type` `pr`, `commit`, `file` or `url` and `value`; at most 10, kept in the `created` event) |
| `ReconcileResult` | `amendments`: per amendment an `AmendmentResult` (`amendment_id`, `clr_id`, outcome `applied`, `reanchored`, `absorbed`, `conflict`, `orphaned` or `undecided`, the new anchor when re-anchored or suspended on the owner's line, the deciding line, similarity, judge verdict and confidence); `text` (the effective text the build must read); `changed` (rows to store); `applied` (rows applied to `text`); `follow_ups` (clarifications whose amendments became `suspended` or `orphaned`) |
| Rows of the ports | `OptionRow`, `AmendmentRow`, `RunRow`, `SeedRow`, `SiteRow`, `VerdictRow`, `SectionKey`, `RelevanceRow`, `NoiseRuleRow`, `SuppressionRow`, `NoteRow`, `EventRow`: one model per table of [data-model.md](data-model.md) "Relational store" (without `project_id`, which every port call takes); `TaskRef` (`task_id`, `story_id`, `status`) and `HoldResult` (`held`, `in_progress`) of the plan ports |
| Investigation | `SitePack`, `SiteBudget`, `InvestigationResult`, `GapReport` (the report, with `ReportOption`, `Edit`, `EditTarget`, `WitnessRef`, `Quote`), `ReadResult` of the read menu `SiteReads` ([investigation.md](investigation.md)) |

## Gap service

`GapService(executor, reader, ontology, rules, records, amendments, *, gateway=None, investigator=None, holds: StoryHolds | None = None, builds: BuildRequests | None = None, settings: GapSettings | None = None, plan: PlanView | None = None, clock=None)`
serves the inbox, the routes and the MCP tools. `plan` gives the story filter and the stories and tasks of reach;
`clock` gives the instants of the write calls.

| Call | Returns | Errors |
|---|---|---|
| `list_gaps(project_id, *, status=None, kind=None, severity=None, origin=None, route=None, about=None, story_id=None, document=None, held_back=None, cursor=None, limit=50)` | A page of `GapView` (no options, history or reach) and the next cursor, by severity (blocker, major, minor), then reach (the number of plan tasks implementing the gap's `ABOUT` and `IMPACTS` nodes, one `PlanView.tasks_for` read per list; 0 without a plan), newest first, then gap id; `status` (default `open`, `answered`, `applying`), `kind`, `severity`, `origin` and `route` take one value or several; `about` filters by spec ids (aliases resolved), `story_id` by the nodes the story's tasks implement (an empty page without a plan), `document` (a document id) by the gaps citing a fragment of it or `ABOUT` a node with evidence in it; `held_back=False` is the inbox's default view (the new questions a run held back are left out until the next run, FR-GRTE-06), `True` lists only those, `None` both | `AnswerInvalidError` (`field` `limit` outside 1 to 200, `cursor`, or a filter value outside its vocabulary) |
| `get_gap(project_id, gap_id)` | `gap_id` may be an alias the gap keeps (a migrated finding's `FND::` id). `GapView` with options, history (events of `GapRecords.events`, follow-up chain), reach (trace from the `ABOUT` and `IMPACTS` nodes along `affects`, depth 2; the plan's tasks of all of them and their stories; empty without a plan) and each witness line's document title, path and heading path (the reader's documents and the fragments of each cited document) | `GapNotFoundError` |
| `document_answers(project_id)` | `DocumentAnswers`: per lineage with `active` or `undecided` amendments, one entry per clarification (its gap and title, the number of its rows there); and the sections of the clarifications document as the next corpus read generates them ([reconcile.md](reconcile.md) "Download with answers") | — |
| `preview_edits(project_id, gap_id, *, option_rank=None, own_text=None, placement=None)` | An `EditPreview`: the checked edits an answer would make and each target section before and after (`SectionPreview`); for own text, one `gap_edits` call drafts the edits (`drafted`), and when it fails or its edits do not check the text goes to the clarifications document as written (`fallback` names why); nothing is stored | `GapNotFoundError`, `GapStateError`, `AnswerInvalidError` (as for `answer_gap`) |
| `answer_gap(project_id, answer: AnswerInput)` | An `AnswerResult`: the clarification id, the pending one it replaced, placement, amendment ids, the apply time (now plus the quiet period) and `immediate` (a blocking development gap, applied at once) | `GapNotFoundError`, `GapStateError` (gap not `open` or `answered`, or `resolved` without an applied clarification), `AnswerInvalidError` ([answers.md](answers.md) "Answering"), `StaleGapError` (its `updated_at` differs from `AnswerInput.updated_at`: the person must reload) |
| `dismiss_gap(project_id, gap_id, *, reason, by)` | The task ids its holds released (`StoryHolds.release_gap` for the gap id and its aliases) | `GapNotFoundError`, `GapStateError` (not `open`) |
| `ai_answers(project_id, *, limit=50)` | The review list "Answered by AI" (`AiAnswerList`): every clarification of mode `ai` or `pointer`, every status, newest first | `AnswerInvalidError` (`limit` outside 1 to 200) |
| `revert_clarification(project_id, clr_id, *, by, reason)` | A `RevertResult` ([answers.md](answers.md) "Revert") | `GapNotFoundError` (`clr_id`), `GapStateError` (not `applied` or `absorbed` on a resolved gap, nor `pending` on an `answered` or `applying` one), `AnswerInvalidError` (`reason` outside 1 to 500 characters), `ConfigurationError` (`gap_builds_missing`) when a build is needed and there is no `builds` |
| `raise_gap(project_id, new: NewGap)` | A `RaisedGap` (gap id, `created` false when the same question about the same ids is open, `held`: the tasks it holds); the host then queues a `site` run on it ([answers.md](answers.md) "Raising a gap by hand"); a blocking development gap passes `StoryHolds.check_block` first and blocks the claimed story after it is written (deleted again when the block fails), a raised `blocker` holds the tasks of its nodes ([lifecycle.md](lifecycle.md) "Development gaps", "Plan link") | `AnswerInvalidError` (`question`, `about` unknown ids or count, `quotes` not found, `limit` for a 51st open development gap of the agent on the story), `ConfigurationError` (`gap_holds_missing`) for a blocking gap without `holds`, plan errors of the hold (`TaskStateError`) pass through |
| `hold_blockers(project_id, spec_ids=None)` | A `HoldResult`: the tasks every open `blocker` gap (about one of `spec_ids`, aliases resolved, when given) holds now through `StoryHolds.hold_tasks`; idempotent; the agent claim route calls it with the story's nodes before a claim ([lifecycle.md](lifecycle.md) "Plan link") | — (a refused hold is logged) |
| `add_note(project_id, *, spec_ids, text, created_by, story_id=None)` | The note id (`NOTE::` and 12 hex): a coding agent's `note` finding as a `gap_note` row (detector `development`, no run) | `AnswerInvalidError` (`text` outside 1 to 2000 characters, `about` count or unknown ids) |
| `apply_now(project_id, *, by, immediate=False)` | An `ApplyResult`: the gaps of the pending clarifications, the clarifications and amendments applied, and the build request's answer ([answers.md](answers.md) "Applying answers") | `GapStateError` (`status` `nothing_pending`) when nothing is pending; `ConfigurationError` (`gap_builds_missing`) without `builds` |
| `open_counts(project_id, spec_ids)` | Open gaps per spec id | Satisfies the plan's `FindingsReads` |
| `findings(project_id, *, spec_ids=None, limit=50)` | A `GapFindingPage` whose items satisfy the plan's `FindingLike` ([lifecycle.md](lifecycle.md) "Plan link"), blocking first, then newest first | Satisfies the plan's `FindingsReads`; `AnswerInvalidError` for `limit` outside 1 to 200 |
| `noise_rules(project_id)`, `add_noise_rule(project_id, scope, value, reason, by)`, `remove_noise_rule(project_id, rule_id, by)` | The rules; the rule id (`NR::` and 12 hex of scope and value: the same rule added twice is one); whether a rule was removed | `AnswerInvalidError` (`scope`, `value` 1 to 500 characters or a pattern that does not compile, `reason` 1 to 500) |

Answers and dismissals write the gap event log. Every write is one transaction in the graph store and one
in the relational store; the relational row is written first and removed again when the graph write fails. The
relational rows are port calls, each its own transaction: the rows the graph write depends on (amendments, a
suppression, amendment statuses) go first and are removed or set back when the graph write fails; the event rows
follow the graph write (the log is append-only), and a dismissal deletes the options after it. Answers, applies and dismissals of one
project run one at a time in the process (a per-project lock in `GapService`; the API runs one process), so two
answers on one gap never both commit. An apply marks each clarification it applies (`apply_requested_at`), so a
re-answer of a resolved gap, which stays `pending` until the close check, is not applied again. A blocking development
answer whose immediate apply fails is kept and waits for the quiet period (`AnswerResult.immediate` false). A failed
dismissal keeps a suppression of the signature that existed before it.

## Reconcile

```python
# graph.gap.reconcile
async def reconcile_document(
    project_id: str,
    revision: LineageText,                 # lineage, source revision id, text
    amendments: Sequence[AmendmentRow],
    *,
    previous_text: str | None = None,      # the text the last build read (active fragments joined)
    judge: Judge | None = None,            # None: the same-meaning question is skipped (undecided)
    settings: ReconcileSettings | None = None,
    now: datetime | None = None,
) -> ReconcileResult

async def reconcile_corpus(
    project_id: str, revisions: Sequence[LineageText], inputs: ReconcileInputs, *, dry_run: bool = False
) -> CorpusReconcile                       # rows to apply by lineage, every outcome

def gateway_judge(gateway, records: GapRecords | None, project_id: str, now) -> Judge
```

Called through the host's corpus reader when a new revision of a document with active amendments arrives,
before the build reads it ([reconcile.md](reconcile.md)). `reconcile_document` decides every amendment's outcome
and returns the effective text and the rows to store; `reconcile_corpus` does that for every lineage with `active`
or `undecided` amendments (`ReconcileInputs`: `amendments`, `records`, `judge`, `previous_text(lineage)`,
`gap_ids()` (clarification id to gap id, for the events), `settings`, `now`), stores the changed rows through
`Amendments.put` and the `reconciled` events through `GapRecords.log` (not in a dry run), and orphans the rows of a
lineage that left the snapshot. The next gap run's close step reads the statuses, moves the clarifications on and
opens the follow-ups. Its only model call is the same-meaning judge (`gateway_judge`: role `gap_judge`, prompt
`RECONCILE_PROMPT`, `SameMeaningAnswer` of `verdict` `same`/`different`/`unrelated`, `confidence`, `reason`), cached
in `gap_verdict` by input hash.

## Errors

| Error | Raised when |
|---|---|
| `ConfigurationError` (core) | Rules or settings are invalid; a port is missing for a call that needs it |
| `GapNotFoundError` | The gap or clarification id is unknown in the project |
| `GapStateError` | The call does not apply to the current status; `status` and `allowed` name why |
| `AnswerInvalidError` | An answer, a raised gap or a noise rule fails validation; `field` and `reason` name it |
| `StaleGapError` | The gap changed after the caller read it (its `updated_at` differs) |

All are `GraphError` subclasses and carry a stable `code`.

## Ports

```python
class GapRecords(Protocol):
    """The relational rows of graph.gap (host: PostgreSQL)."""
    async def options(self, project_id: str, gap_id: str) -> Sequence[OptionRow]: ...
    async def put_options(self, project_id: str, gap_id: str, rows: Sequence[OptionRow]) -> None: ...
    async def start_run(self, project_id: str, run: RunRow) -> None: ...
    async def finish_run(self, project_id: str, run_id: str, counts: Mapping[str, Any], cost: Mapping[str, Any], *, status: FinishedRunStatus = "completed") -> None: ...
    async def put_seeds(self, project_id: str, run_id: str, seeds: Sequence[SeedRow]) -> None: ...
    async def put_sites(self, project_id: str, run_id: str, sites: Sequence[SiteRow]) -> None: ...
    async def verdict(self, project_id: str, input_hash: str) -> VerdictRow | None: ...
    async def put_verdict(self, project_id: str, row: VerdictRow) -> None: ...
    async def relevance(self, project_id: str, keys: Sequence[SectionKey]) -> Mapping[SectionKey, RelevanceRow]: ...
    async def put_relevance(self, project_id: str, rows: Sequence[RelevanceRow]) -> None: ...
    async def noise_rules(self, project_id: str) -> Sequence[NoiseRuleRow]: ...
    async def put_noise_rule(self, project_id: str, row: NoiseRuleRow) -> None: ...
    async def delete_noise_rule(self, project_id: str, rule_id: str) -> bool: ...
    async def suppressed(self, project_id: str, signatures: Sequence[str]) -> frozenset[str]: ...
    async def suppress(self, project_id: str, row: SuppressionRow) -> None: ...
    async def unsuppress(self, project_id: str, signature: str) -> None: ...
    async def put_notes(self, project_id: str, run_id: str, notes: Sequence[NoteRow]) -> None: ...
    async def log(self, project_id: str, events: Sequence[EventRow]) -> None: ...
    async def events(self, project_id: str, gap_id: str) -> Sequence[EventRow]: ...

class Amendments(Protocol):
    """Stored edits applied by the corpus reader (host: PostgreSQL, beside the documents)."""
    async def for_lineage(self, project_id: str, lineage: str, *, statuses: Sequence[str] = ("active",)) -> Sequence[AmendmentRow]: ...
    async def for_clarification(self, project_id: str, clr_id: str) -> Sequence[AmendmentRow]: ...
    async def put(self, project_id: str, rows: Sequence[AmendmentRow]) -> None: ...
    async def set_status(self, project_id: str, amendment_ids: Sequence[str], status: str, *, revision_id: str | None = None) -> None: ...
    async def delete(self, project_id: str, amendment_ids: Sequence[str]) -> None: ...
    async def pending_lineages(self, project_id: str) -> Sequence[str]: ...  # lineages with active or undecided rows

class Investigator(Protocol):
    """Reads around one site and returns a report (investigation.md)."""
    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult: ...

class StoryHolds(Protocol):
    """The plan's hold and release calls (host: PlanService)."""
    async def hold_tasks(self, project_id: str, gap_id: str, spec_ids: Sequence[str], *, reason: str = "spec_ambiguous") -> HoldResult: ...
    async def release_gap(self, project_id: str, gap_id: str) -> Sequence[str]: ...
    async def check_block(self, project_id: str, story_id: str, *, claimed_by: str, claim_token: str, spec_ids: Sequence[str]) -> None: ...
    async def block_story(self, project_id: str, story_id: str, *, claim_token: str, reason: str, finding_id: str) -> Any: ...

class BuildRequests(Protocol):
    """Asks the host for an update build; builds stay one at a time per project."""
    async def request_update_build(self, project_id: str, reason: str, *, immediate: bool) -> str: ...

class PlanView(Protocol):
    """What the gap run reads of the plan: tasks per spec node and their status (host: plan reader)."""
    async def tasks_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[TaskRef]]: ...
    async def stories_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[str]]: ...
```

`GapRecords` and `Amendments` behave the same on every implementation (host: `api.gap.records` on PostgreSQL;
tests: the in-memory fakes; one contract suite runs on both). `put_options` replaces the gap's options and
`options` returns them by rank; a set with a repeated rank, more than one recommended row or a row of another gap
raises `ConformanceError` (`gap_options_invalid`) before anything is written (the 2-to-4 count and the one
recommended row of an `ask` gap are the `check` step's rule, GI5). `start_run` stores the run (a second call replaces it); `finish_run` sets
`status` (`completed`, `degraded` or `failed`; another value raises `ConformanceError` `gap_run_status_invalid`),
`finished_at`, counts and cost, and raises `ConformanceError` (`gap_run_unknown`) for a run the project does not
have. `events` returns one gap's events of the project by `at`, then id. `put_seeds` and `put_sites` raise `ConformanceError` (`gap_rows_of_another_run`) for a row
of another run; a site put again for its run takes the new values. `put_notes` files every note under the given
run. Verdicts, relevance rows, suppressions and amendments are upserts on their keys ([data-model.md](data-model.md)
"Relational store"); so is `put_noise_rule`. `delete_noise_rule` (true when the rule existed), `unsuppress` and
`Amendments.delete` remove only the project's rows (a failed graph write takes back the rows written before it, and a
replaced pending answer's amendments go). `set_status` changes only the project's amendments among the ids, ignores the others and
moves `updated_at`, and raises `ConformanceError` (`gap_amendment_status_unknown`) for a status outside the
vocabulary; `for_lineage` and `for_clarification` return rows by `created_at`, then id (code-point order), and a
put of an existing amendment replaces it, `created_at` included. A batch that names one key twice keeps its last
row. On PostgreSQL the project id is the project's uuid (`gap_project_id_invalid` otherwise).

`check_block`, `block_story` and `release_gap` map onto the plan service's existing guard, block and unblock
calls; `hold_tasks` is the blocker hold of decision GD2, the plan's `hold_tasks` call (`reason` the block reason of the
gap's kind). The host's adapter is `api.gap.holds.PlanHolds`. A host without a plan passes `holds=None` and
`plan=None`: nothing is held, and reach shows only spec nodes.

The gateway roles of `graph.gap` are `gap_relevance` (section classifier), `gap_investigate` (investigation
rounds), `gap_judge` (second check, contradiction re-check, same-meaning judge) and `gap_edits` (drafting
edits for own text). Each has its own model and limits in the gateway settings.

## Host runner

`api/gap/runner.py` runs gap runs in process, like the build and plan runners: one background task per
project, file stores under `GRAPH_STATE_DIR` for run artifacts (the site list, the step reached, reports
waiting to be recorded), so an interrupted run resumes at its step on the next start. A per-project queue
keeps one gap run at a time; a site-mode run for a development gap jumps the queue but never runs alongside
another run of the same project.

| Trigger | Run |
|---|---|
| A build returns (and `auto_run_after_build`) | `update`, with the change reports of the builds since the last gap run; then the plan run |
| `POST .../gap/runs` | `full` |
| `raise_gap`, `add_finding` | `site` for that gap, at the head of the queue, on the build of the latest finished gap run (read when the run starts) |
| Answers pending past the quiet period, or `apply_now` | No run: amendments are activated and an update build is requested (`GraphBuildRunner.request_update_build`: `started`, or `queued` behind the running build, which starts one more build when it ends); the run after that build closes the gaps |

Status is polled like the plan's: step, progress, counts, the last result.

How the host does it (`api/gap/runner.py`, `GapRunner` on `Runtime.gaps`, built with the graph build runner when
`GAP_ENABLED` is on; the `GAP_*` variables reach it as `RoleSettings.gap` through `gap_settings_from_env`):

- **Queue.** Each project has a queue drained by one task, so its runs never overlap; runs of different projects
  share the process's run slot. A waiting `update` run is replaced by a newer one (both read the change reports from
  the same gap run on, so the newer build covers the older). A `full` request joins a full run already queued or
  running. A site-mode run will be put at the head of the queue, never beside the running run.
- **After a build.** The build runner calls `after_build(project_id, build_id)` after each returned build, before
  its task ends, so the run is queued before any plan run can start. It queues nothing when the settings turn
  automatic runs off, when the build failed, or when the build's settlement lists `documents_incomplete` (logged
  `api.gap.skipped` with the reason). It never raises into the build.
- **Before a plan run.** The plan runner first awaits `wait_idle(project_id)`, which returns once the project has
  no gap run queued or running: the order after a build is build, gap run, plan run. A gap run failure, or a failure
  of the wait, is logged and the plan run goes on.
- **Context.** `changes`: the change reports of the builds after the build of the project's latest finished gap run
  up to the run's build (the plan's chain, `api/graph_build/reports.py`), converted by `api/gap/changes.py`; the
  first gap run of a project has none. A chain the build store cannot complete, or a latest gap run that failed,
  gives none and logs `api.gap.change_reports_missing` with the reason. `build`: the dedup refusals (both ends
  persisted and distinct) and `cycle_broken` cuts of the build's stored result. `plan`: the persisted plan through
  `PlanReader` (tasks per spec node by `IMPLEMENTS`, aliases resolved, closed tasks included). `fragments_changed`
  and `documents` stay empty: the build's change report names neither.
  `gateway`: one `Gateway` per run on the host's graph-build model settings; `investigator`:
  `GapRunnerDependencies.investigator(gateway)`, by default `LoopInvestigator` over that gateway.
- **Resume.** A project's unfinished run (interrupted by a shutdown, or failed) is resumed by the project's next
  run, with its own mode, build and sites, before the requested run (which is skipped when the resumed run covers
  it: an update of the same build, or a full run).
- **Status.** `GET .../gap/runs/latest` answers `idle`, `queued`, `running` (with the step, steps done, the items of
  the running step, the runs waiting) or the latest run's outcome with its counts and stored `GapRunResult`; an
  interrupted run shows `failed` with `interrupted`.
- **Reads and writes.** The routes use one `GapService` built from the runner's ontology, rules, store, relational
  ports, settings, plan view, the build runner as `BuildRequests` (`use_builds`) and one `Gateway` for `gap_edits`
  (closed with the runner).
- **Answers.** `schedule_apply(project_id, at)` keeps the project's apply time (a later answer moves it); when it
  passes, `apply_now(by="gap-apply")` runs (nothing pending is logged as skipped). The time lives in the process. A
  gap run whose result counts `ai_answered` schedules the apply at the end of the quiet period too (AI answers are
  never applied at once).

The host also:
- adds the relational tables of [data-model.md](data-model.md) "Relational store" in one migration;
- changes the corpus reader (`PostgresCorpusSource(session_factory, answers=...)`, `api/graph_build/corpus.py`) to
  apply answers when the gap module is on. `answers` is the gap runner (`CorpusAnswers.corpus_answers(project_id)` ->
  `CorpusAnswersRead`: the amendments to apply by lineage through `Amendments`, and the clarifications of the
  clarifications document read from the graph by `graph.gap.corpus.document_clarifications`); the build runner wires
  it when its gap hooks implement `CorpusAnswers`. The snapshot passes its revisions
  (`corpus_answers(project_id, revisions, dry_run=...)`): the gap runner reconciles them first
  (`graph.gap.reconcile.reconcile_corpus`, with a `gap_judge` gateway opened only when a question needs it, none in a
  dry run, and the graph's active fragments as the previous text); without revisions (the export) the stored
  `active` and `undecided` rows are applied as they are. `with_answers` applies each lineage's active amendments
  (`graph.gap.corpus.apply_amendments`), sets the effective revision id (`effective_revision_id`) and adds the
  generated clarifications document ([answers.md](answers.md) "Clarifications document"); an amendment whose anchor
  line is missing is left out and logged as `api.graph_build.amendment_unanchored` (ids only);
  `effective_document(rows, lineage, answers, project_id)` is the export of one lineage;
- stores each build's change report with the build (the plan prerequisite, [lifecycle.md](lifecycle.md)).

## Web routes

Under `/projects/{pid}`, in the style of the existing project routers. Project membership is the access rule.

| Method and path | Call |
|---|---|
| `GET /projects/{pid}/gaps` | `list_gaps` (query: status, kind, severity, origin, route, about, story_id, document, held_back, cursor, limit; a list filter repeats its parameter; `document` takes the graph's document id, a lineage, or a document's id as the documents routes name it, which the route turns into its corpus lineage: `drive:<connection>:<file>` for a Drive document, else `doc:<id>`) |
| `GET /projects/{pid}/gaps/{gap_id}` | `get_gap` |
| `POST /projects/{pid}/gaps` | `raise_gap` (origin `person`) |
| `POST /projects/{pid}/gaps/{gap_id}/edits_preview` | `preview_edits` |
| `POST /projects/{pid}/gaps/{gap_id}/answer` | `answer_gap` |
| `POST /projects/{pid}/gaps/{gap_id}/dismiss` | `dismiss_gap` |
| `GET /projects/{pid}/gaps/documents` | The document view: `{documents: [{document_id, lineage, path, title, generated, answers: [DocumentAnswer]}]}`, every corpus document (newest of its lineage, path order) with `document_answers` of its lineage, then the generated clarifications document (`generated` true, its `document_id` the UUID of `doc:<uuid5>`) while it has a section; registered before `GET .../gaps/{gap_id}` |
| `GET /projects/{pid}/gaps/ai_answers` | `ai_answers` (query `limit`, 1 to 200); registered before `GET .../gaps/{gap_id}` |
| `POST /projects/{pid}/gaps/clarifications/{clr_id}/revert` | `revert_clarification` (body `reason`), the member as `by`; `RevertResult` |
| `POST /projects/{pid}/gaps/apply` | `apply_now` |
| `POST /projects/{pid}/gap/runs` | Start a full run (202, run status) |
| `GET /projects/{pid}/gap/runs/latest` | Run status and the last `GapRunResult` |
| `GET /projects/{pid}/gap/runs/{run_id}/noise` | The noise report of a finished run (`404 gap_run_not_found` otherwise) |
| `GET /projects/{pid}/gaps/noise_rules` | `noise_rules` |
| `POST /projects/{pid}/gaps/noise_rules` | `add_noise_rule` |
| `DELETE /projects/{pid}/gaps/noise_rules/{rule_id}` | `remove_noise_rule` |
| `GET /projects/{pid}/documents/{document_id}/export?answers=1` | The newest text of the document's corpus lineage, with `answers=1` its applied amendments (marker lines included), as the last build read it: `text/markdown` attachment named after the document; `404 document_not_found` outside the corpus; without the gap module the source text ([reconcile.md](reconcile.md) "Download with answers"). The clarifications document's id downloads the generated text (`Clarifications.md`; `404 document_not_found` without the gap module or a section). Served by `api.routers.gaps` |

A write answers `400 answer_invalid` (`details`: `field`, `reason`), `409 gap_wrong_state` (`details`: `status`,
`allowed`), `409 gap_stale` (the answer's `updated_at` is not the gap's: reload), `404 gap_not_found` or
`404 noise_rule_not_found`; `POST .../gaps` answers `201` with the `RaisedGap`, `POST .../gaps/noise_rules` `201`
with the rule id, dismiss and rule removal `204`. `GET .../gaps/noise_rules` is registered before
`GET .../gaps/{gap_id}`. A non-member gets `404 project_not_found`, as on every project route. `get_gap` answers `404 gap_not_found` for an
unknown gap; an invalid cursor `400 invalid_cursor`; a full run of a project without a finished build
`409 no_graph_build`. Every gap route answers `503 gap_unavailable` while the gap runner is not configured (no graph
build settings, or `GAP_ENABLED` off).

The existing `/projects/{pid}/findings` routes stay as aliases of `GET .../gaps?origin=development` and of
dismiss for one release, then go. As built (`api/routers/findings.py`): the list keeps its shape (`FindingOut`: the
gap id, the finding kind of the gap's kind, the question as text, `open` for open, answered and applying gaps and
`resolved` for resolved ones, `refers_to` empty) and its `status` (`open`, `resolved`, `all`; `dismissed` lists none:
a dismissed gap is gone), `node` (the `about` filter) and `after` (the cursor); `resolve` and `dismiss` both dismiss
the open gap with the note as reason (`Resolved` or `Dismissed` without one) and answer the closed finding and the
number of tasks released; `404 finding_not_found`, `409 finding_closed`, `409 plan_applying` (the plan refused to
release the gap's tasks during a persist: nothing changed, retry), `503 gap_unavailable`. `POST .../gaps/{gap_id}/dismiss`
answers `409 plan_applying` the same way: `dismiss_gap` releases the holds before it deletes the gap.

## MCP tools

| Tool | Change |
|---|---|
| `add_finding` | Kept with its arguments; mapped to `raise_gap` with origin `development` (a `note` to `add_note`) and a `site` run at once ([lifecycle.md](lifecycle.md) "Development gaps", as built); the answer keeps its shape, `finding_id` is the gap id and the author of a repeat is the gap's first reporter; the API answers `503 gap_unavailable` while gap runs are not configured (the tool reports `internal_error`) ([../../mcp-server/tools.md](../../mcp-server/tools.md) "add_finding") |
| `list_gaps` (new, read-only) | Open gaps about a story's nodes (`story_id`, through `PlanService.story_nodes`) or about given spec ids (`spec_ids`, exactly one of the two): id, kind, severity, question, status, blocking, `ABOUT` ids, and the answer's text once a clarification exists (its text, else the chosen option's label, else a pointer's answering lines); `status` adds `resolved` gaps. Served by the agent route `GET /agent/gaps` over `GapService.list_gaps(about=...)` with `held_back=None` (agents see the questions held back from the inbox too); an empty list while the gap runner is not configured ([../../mcp-server/tools.md](../../mcp-server/tools.md) "list_gaps") |
| `get_story_brief`, `get_task_brief`, `preview_story` | Unchanged calls; their findings now come from gaps through the plan's findings port (question, `ABOUT` ids, blocking first) and the host adds each gap's `severity`; `get_task_brief` gains `findings` (the open findings about the nodes the task implements), which the host reads through the same port because the plan's `TaskBrief` has none |
| `get_node` | Lists open gaps about the node, with severity |
| `claim_story` | Unchanged call; refused, naming the gap, when a task of the story is held by a blocker gap |

## User interface

| Screen or element | Content |
|---|---|
| Gaps screen, inbox | Open gaps with filters (kind, severity, origin, story, document), sorted by severity and reach; development gaps that hold tasks first; "Apply now"; "Raise a gap" |
| Gaps screen, card | Title, question, why, chips for `ABOUT` and `IMPACTS` nodes, quoted witness lines with their source (document, section, lines), reach (stories, tasks), options with the recommended one marked, "Other" with free text and an edits preview, placement choice, "Not a gap" with a reason, the history (events, follow-up chain), "Withdraw answer" for an answered gap |
| Gaps screen, answered by AI | AI answers with their basis and confidence; Revert per row |
| Gaps screen, noise report | Sections judged tracking or mixed, their signals, "Mark as not spec" and the noise rules (add, remove) |
| Gaps screen, documents | The document view: every document with the banner "N answers are not in the source file yet", its answers linked to their cards and "Download with answers"; the clarifications document marked "Generated by Specflow" |
| Plan screens | Stories and tasks show open gaps and held tasks with a link to the card |

As built: the Gaps screen at `/projects/{pid}/gaps` (it replaced the Decisions screen) has the tabs Inbox, Answered by
AI, Noise report and Documents. The inbox lists gaps of route `ask` with `held_back=false` in the service's order
(severity, reach, newest) with development gaps that hold tasks first; a second read with `held_back=true` fills a
collapsed "More found in this run" section. The card answers through `edits_preview` and `answer` (with the gap's
`updated_at` as read; `409 gap_stale` offers a reload), dismisses with a reason ("Not spec" also offers a section noise
rule), and withdraws an answer through the revert route. The document view is the Documents tab (the app has no
document reader of its own): it reads `GET .../gaps/documents` and downloads through the export route. The plan
screens read the open gaps once (status open, answered, applying; any route) and show on a task the gaps whose `ABOUT`
ids meet the task's implemented spec ids, and on a story those of its tasks, each linking to the card (`?gap=<id>`).
"Mark as spec" for a noise section (no route yet) and a control that starts a full run are not on the screen. The old Decisions URL `/projects/{pid}/decisions`
redirects to the Gaps screen (query kept). A task's gaps match its implemented ids against each `ABOUT` node's id or
`aliases`. The Work tab's findings (the `/findings` alias routes) link each row to its gap card; `?gap=` takes a
gap id or an alias (a migrated `FND::` id).
