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
| `ensure_gap_schema(source, ontology, llm_config, database=None) -> tuple[str, ...]` | The names of objects it created | The store's `ensure_schema` with the gap DDL (`ddl/gap_schema.cypher`, found by the store), like `ensure_plan_schema`: creates the `graph_gap_*` constraints and indexes of [data-model.md](data-model.md); idempotent (a second call returns `()`); runs the migration of first-slice findings when a project still has `Finding` nodes ([lifecycle.md](lifecycle.md)) |

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
options failed, finds duplicates and runs the second check of a pointer; `route` applies [answers.md](answers.md)
"Severity" and "Routing" (AI route: pointers only until the gates and the valve are built) and holds back the new
`ask` gaps beyond `limits.new_questions_per_run`; `record` writes the gaps, their option rows and pointer
clarifications. The step `close` is a no-op until the close check is built. An `info` report is a `gap_note` (site
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
| `GapContext` | `project_id`, `executor` (core store executor), `reader` (`GraphReader`), `gateway` (core `Gateway`), `ontology`, `rules` (`GapRules`), `records` (`GapRecords`), `amendments` (`Amendments`), `investigator` (`Investigator`), `changes: tuple[GapChangeReport, ...]`, `build: BuildFacts` (the dedup refusals and cycle cuts of the build the run follows), `plan: PlanView \| None`, `settings: GapSettings`, `store` (run artifact store, for resume), `run_id: str \| None`, `on_progress` (optional callback of step name, done, total) |
| `GapSettings` | `enabled` (default `True`), `rules_path` (`None`: shipped rules), `investigator` (`"loop"` or `"agents"`, default `"loop"`), `auto_run_after_build` (default `True`), `ai_answers` (default `True`), `limits` (optional overrides of the rules' `limits`, by name), `apply_quiet_seconds` (overrides the rules' value), `ignore_cache` (default `False`: a `full` run reuses cached verdicts). `gap_settings_from_env` maps the `GAP_*` variables of [architecture.md](architecture.md) "Settings"; `effective_limits` applies the overrides to the rules' limits |
| `GapChangeReport` | The gap-side copy of one build change report: `build_id`, `added`, `removed`, `merged` (old id to new id), `rekeyed` (old id to new id), `changed_meaning`, `changed_links`, `changed_evidence` (spec id tuples), `fragments_changed` (fragment ids by lineage), `documents` (lineage to revision id). The host converts the build's report; `graph.gap` never imports `graph.build` |
| `BuildFacts` | `build_id`, `refused_pairs` (`RefusedPair`: `first_spec_id`, `second_spec_id`, `label`, `guard`: the build result's dedup refusals) and `cut_edges` (`CutEdge`: `relation`, `source_spec_id`, `target_spec_id`, `fragment_ids`: the build result's quarantined `cycle_broken` edges). The host converts them from the build result; CT-VALUE and IN-CYCLE read them ([detection.md](detection.md) "Detector catalogue") |
| `GapRunResult` | `run_id`, `mode`, `build_id`, `status` (`completed`, `degraded` when every investigation failed, `failed`), `counts` (the run metrics of [detection.md](detection.md) "Run metrics": seeds by detector, sites investigated, cached, deferred, gaps new, follow-ups, AI answered, resolved, obsolete, notes), `cost` (calls and tokens per gateway role), `noise` (`NoiseReport`), `deferred_sites`, `held_tasks`, `warnings` |
| `NoiseReport` | Per section: lineage, heading path, relevance, signals, how it was decided (`rules`, `model`, `person`), and the node ids it removed from structural seeds |
| `GapEstimate` | `sites`, `cached`, `calls` and `tokens` per role (expected investigation calls are the loop's bound: `rounds_per_site` plus the forced report call per site, each of at most `context_tokens`) |
| `GapRunRecord` | The run store's record of one run ("Gap runs" above) |
| `GapFinding`, `GapFindingPage` | What `GapService.findings` returns: an open gap as the plan's `FindingLike` (`finding_id`, `kind`, `text`, `blocking`, `about_spec_ids`, `story_id`, `created_by`, `created_at`, `orphaned`) and the page of them |
| `GapView` | What reads return: the gap's properties (`held_back` true while the gap is a new question its run held back from the inbox's default view), `about` and `impacts` (id, type, name), `witness` (fragment id, quote, lines), `options` (from `gap_option`), `clarification` (current one), `history` (events, follow-up chain), `reach` (stories and tasks found by trace and the plan, computed at read time) |
| `AnswerInput` | `gap_id`, `option_rank` or `own_text` (exactly one), `placement` (`amend`, `document`; default from the option's edits), `edits` (optional: edits confirmed from `preview_edits`), `answered_by` |
| `NewGap` | `kind`, `question`, `about` (spec ids), `quotes` (fragment id and quote, optional), `severity` (optional), `origin` (`person` or `development`), `blocking`, `story_id` and `claim_token` (development), `created_by` |
| `ReconcileResult` | Per amendment: `amendment_id`, outcome (`applied`, `reanchored`, `absorbed`, `conflict`, `orphaned`), the new anchor when re-anchored; and the effective text the build must read |
| Rows of the ports | `OptionRow`, `AmendmentRow`, `RunRow`, `SeedRow`, `SiteRow`, `VerdictRow`, `SectionKey`, `RelevanceRow`, `NoiseRuleRow`, `SuppressionRow`, `NoteRow`, `EventRow`: one model per table of [data-model.md](data-model.md) "Relational store" (without `project_id`, which every port call takes); `TaskRef` (`task_id`, `story_id`, `status`) and `HoldResult` (`held`, `in_progress`) of the plan ports |
| Investigation | `SitePack`, `SiteBudget`, `InvestigationResult`, `GapReport` (the report, with `ReportOption`, `Edit`, `EditTarget`, `WitnessRef`, `Quote`), `ReadResult` of the read menu `SiteReads` ([investigation.md](investigation.md)) |

## Gap service

`GapService(executor, reader, ontology, rules, records, amendments, *, gateway=None, investigator=None, holds: StoryHolds | None = None, builds: BuildRequests | None = None, settings: GapSettings | None = None, plan: PlanView | None = None, clock=None)`
serves the inbox, the routes and the MCP tools. `plan` gives the story filter and the stories and tasks of reach;
`clock` gives the instants of the write calls.

| Call | Returns | Errors |
|---|---|---|
| `list_gaps(project_id, *, status=None, kind=None, severity=None, origin=None, route=None, about=None, story_id=None, document=None, held_back=None, cursor=None, limit=50)` | A page of `GapView` (no options, history or reach) and the next cursor, by severity (blocker, major, minor), newest first, then gap id; `status` (default `open`, `answered`, `applying`), `kind`, `severity`, `origin` and `route` take one value or several; `about` filters by spec ids (aliases resolved), `story_id` by the nodes the story's tasks implement (an empty page without a plan), `document` (a document id) by the gaps citing a fragment of it or `ABOUT` a node with evidence in it; `held_back=False` is the inbox's default view (the new questions a run held back are left out until the next run, FR-GRTE-06), `True` lists only those, `None` both | `AnswerInvalidError` (`field` `limit` outside 1 to 200, `cursor`, or a filter value outside its vocabulary) |
| `get_gap(project_id, gap_id)` | `GapView` with options, history (events of `GapRecords.events`, follow-up chain) and reach (trace from the `ABOUT` and `IMPACTS` nodes along `affects`, depth 2; the plan's tasks of all of them and their stories; empty without a plan) | `GapNotFoundError` |
| `preview_edits(project_id, gap_id, *, option_rank=None, own_text=None, placement=None)` | The edits an answer would make, with the target section's current text and the text after; for own text, AI drafts the edits (role `gap_edits`) | `GapNotFoundError`, `AnswerInvalidError` (empty text, too long, anchor not found) |
| `answer_gap(project_id, answer: AnswerInput)` | The clarification id and the scheduled apply time | `GapNotFoundError`, `GapStateError` (gap not `open` or `answered`), `AnswerInvalidError`, `StaleGapError` (the gap's witness changed since it was shown: the person must reload) |
| `dismiss_gap(project_id, gap_id, *, reason, by)` | Nothing | `GapNotFoundError`, `GapStateError` (not `open`) |
| `revert_clarification(project_id, clr_id, *, by, reason)` | The follow-up gap id | `GapNotFoundError`, `GapStateError` (not `applied` or `absorbed`) |
| `raise_gap(project_id, new: NewGap)` | The gap id; with `blocking`, the released claim | `AnswerInvalidError` (unknown ids, quote not found), plan errors of the hold (`TaskStateError`) pass through |
| `apply_now(project_id, *, by)` | The gap ids moved to `applying` and the requested build | `GapStateError` when nothing is pending |
| `open_counts(project_id, spec_ids)` | Open gaps per spec id | Satisfies the plan's `FindingsReads` |
| `findings(project_id, *, spec_ids=None, limit=50)` | A `GapFindingPage` whose items satisfy the plan's `FindingLike` ([lifecycle.md](lifecycle.md) "Plan link"), blocking first, then newest first | Satisfies the plan's `FindingsReads`; `AnswerInvalidError` for `limit` outside 1 to 200 |
| `noise_rules(project_id)`, `add_noise_rule(project_id, scope, value, reason, by)`, `remove_noise_rule(project_id, rule_id, by)` | The rules; the new rule id | `AnswerInvalidError` (pattern does not compile) |

Answers and dismissals write the gap event log. Every write is one transaction in the graph store and one
in the relational store; the relational row is written first and removed again when the graph write fails.

## Reconcile

```python
async def reconcile_document(
    project_id: str,
    lineage: str,
    revision_text: str,
    amendments: Sequence[AmendmentRow],
    *,
    gateway: Gateway,
    rules: GapRules,
) -> ReconcileResult
```

Called by the host's corpus reader when a new revision of a document with active amendments arrives,
before the build reads it ([reconcile.md](reconcile.md)). It decides every amendment's outcome, returns the
effective text, and leaves the writes to the caller, which stores the outcomes through `Amendments` and
then lets the next gap run update the clarifications and open follow-ups. Its only model call is the
same-meaning judge (role `gap_judge`), cached by input hash.

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
    async def suppressed(self, project_id: str, signatures: Sequence[str]) -> frozenset[str]: ...
    async def suppress(self, project_id: str, row: SuppressionRow) -> None: ...
    async def put_notes(self, project_id: str, run_id: str, notes: Sequence[NoteRow]) -> None: ...
    async def log(self, project_id: str, events: Sequence[EventRow]) -> None: ...
    async def events(self, project_id: str, gap_id: str) -> Sequence[EventRow]: ...

class Amendments(Protocol):
    """Stored edits applied by the corpus reader (host: PostgreSQL, beside the documents)."""
    async def for_lineage(self, project_id: str, lineage: str, *, statuses: Sequence[str] = ("active",)) -> Sequence[AmendmentRow]: ...
    async def for_clarification(self, project_id: str, clr_id: str) -> Sequence[AmendmentRow]: ...
    async def put(self, project_id: str, rows: Sequence[AmendmentRow]) -> None: ...
    async def set_status(self, project_id: str, amendment_ids: Sequence[str], status: str, *, revision_id: str | None = None) -> None: ...
    async def pending_lineages(self, project_id: str) -> Sequence[str]: ...

class Investigator(Protocol):
    """Reads around one site and returns a report (investigation.md)."""
    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult: ...

class StoryHolds(Protocol):
    """The plan's hold and release calls (host: PlanService)."""
    async def hold_tasks(self, project_id: str, gap_id: str, spec_ids: Sequence[str]) -> HoldResult: ...
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
"Relational store"). `set_status` changes only the project's amendments among the ids, ignores the others and
moves `updated_at`, and raises `ConformanceError` (`gap_amendment_status_unknown`) for a status outside the
vocabulary; `for_lineage` and `for_clarification` return rows by `created_at`, then id (code-point order), and a
put of an existing amendment replaces it, `created_at` included. A batch that names one key twice keeps its last
row. On PostgreSQL the project id is the project's uuid (`gap_project_id_invalid` otherwise).

`check_block`, `block_story` and `release_gap` map onto the plan service's existing guard, block and unblock
calls; `hold_tasks` is the blocker hold of decision GD2. A host without a plan passes `holds=None` and
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
| `raise_gap`, `add_finding` | `site` for that gap |
| Answers pending past the quiet period, or `apply_now` | No run: amendments are activated and an update build is requested; the run after that build closes the gaps |

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
- **Reads.** The routes use one `GapService` built from the runner's ontology, rules, store, relational ports,
  settings and plan view.

The host also:
- adds the relational tables of [data-model.md](data-model.md) "Relational store" in one migration;
- changes the corpus reader (`PostgresCorpusSource`) to apply active amendments to each document it
  reads, to add the clarifications document (origin `todo_answer`, one section per applied clarification
  placed `document`, its heading naming the parts it is about), to call `reconcile_document` when a
  lineage's newest revision differs from the amendments' `last_revision_id`, and to report as revision id
  the hash of the source revision id and the applied amendment ids, so a build sees a change only when the
  text it reads changes;
- stores each build's change report with the build (the plan prerequisite, [lifecycle.md](lifecycle.md)).

## Web routes

Under `/projects/{pid}`, in the style of the existing project routers. Project membership is the access rule.

| Method and path | Call |
|---|---|
| `GET /projects/{pid}/gaps` | `list_gaps` (query: status, kind, severity, origin, route, about, story_id, document, held_back, cursor, limit; a list filter repeats its parameter) |
| `GET /projects/{pid}/gaps/{gap_id}` | `get_gap` |
| `POST /projects/{pid}/gaps` | `raise_gap` (origin `person`) |
| `POST /projects/{pid}/gaps/{gap_id}/edits_preview` | `preview_edits` |
| `POST /projects/{pid}/gaps/{gap_id}/answer` | `answer_gap` |
| `POST /projects/{pid}/gaps/{gap_id}/dismiss` | `dismiss_gap` |
| `POST /projects/{pid}/gaps/clarifications/{clr_id}/revert` | `revert_clarification` |
| `POST /projects/{pid}/gaps/apply` | `apply_now` |
| `POST /projects/{pid}/gap/runs` | Start a full run (202, run status) |
| `GET /projects/{pid}/gap/runs/latest` | Run status and the last `GapRunResult` |
| `GET /projects/{pid}/gap/runs/{run_id}/noise` | The noise report of a finished run (`404 gap_run_not_found` otherwise) |
| `GET /projects/{pid}/gaps/noise_rules` | `noise_rules` |
| `POST /projects/{pid}/gaps/noise_rules` | `add_noise_rule` |
| `DELETE /projects/{pid}/gaps/noise_rules/{rule_id}` | `remove_noise_rule` |
| `GET /projects/{pid}/documents/{document_id}/export?answers=1` | The document's text with its active amendments applied ([reconcile.md](reconcile.md) "Download with answers") |

A non-member gets `404 project_not_found`, as on every project route. `get_gap` answers `404 gap_not_found` for an
unknown gap; an invalid cursor `400 invalid_cursor`; a full run of a project without a finished build
`409 no_graph_build`. Every gap route answers `503 gap_unavailable` while the gap runner is not configured (no graph
build settings, or `GAP_ENABLED` off).

The existing `/projects/{pid}/findings` routes stay as aliases of `GET .../gaps?origin=development` and of
dismiss for one release, then go.

## MCP tools

| Tool | Change |
|---|---|
| `add_finding` | Kept with its arguments; mapped to `raise_gap` with origin `development` ([lifecycle.md](lifecycle.md) "Development gaps"); the answer returns the gap id |
| `list_gaps` (new, read-only) | Open gaps about a story's nodes (`story_id`, through `PlanService.story_nodes`) or about given spec ids (`spec_ids`, exactly one of the two): id, kind, severity, question, status, blocking, `ABOUT` ids, and the answer's text once a clarification exists (its text, else the chosen option's label); `status` adds `resolved` gaps. Served by the agent route `GET /agent/gaps` over `GapService.list_gaps(about=...)` with `held_back=None` (agents see the questions held back from the inbox too); an empty list while the gap runner is not configured ([../../mcp-server/tools.md](../../mcp-server/tools.md) "list_gaps") |
| `get_story_brief`, `get_task_brief`, `preview_story` | Unchanged calls; their findings now come from gaps through the plan's findings port (question, `ABOUT` ids, blocking first) and the host adds each gap's `severity`; `get_task_brief` gains `findings` (the open findings about the nodes the task implements), which the host reads through the same port because the plan's `TaskBrief` has none |
| `get_node` | Lists open gaps about the node, with severity |
| `claim_story` | Unchanged call; refused, naming the gap, when a task of the story is held by a blocker gap |

## User interface

| Screen or element | Content |
|---|---|
| Gaps screen, inbox | Open gaps with filters (kind, severity, origin, story, document), sorted by severity and reach; development gaps that hold tasks first |
| Gaps screen, card | Title, question, why, chips for `ABOUT` and `IMPACTS` nodes, quoted witness lines with their source, reach (stories, tasks), options with the recommended one marked, "Other" with free text and an edits preview, placement choice, "Not a gap" with a reason |
| Gaps screen, answered by AI | AI answers with their basis and confidence; Revert per row |
| Gaps screen, noise report | Sections judged tracking or mixed, their signals, "Mark as not spec" and the noise rules |
| Plan screens | Stories and tasks show open gaps and held tasks with a link to the card |
| Document view | Banner "N answers are not in the source file yet" and "Download with answers" |

The screen is built from the existing decision components (`components/decisions`: questions, option
cards, severity badge), with a "recommended" mark added to the option cards and a real data port in place of
the unavailable one.
