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
| `ensure_gap_schema(executor) -> tuple[str, ...]` | The names of objects it created | Creates the `graph_gap_*` constraints and indexes of [data-model.md](data-model.md); idempotent (a second call returns `()`); runs the migration of first-slice findings when a project still has `Finding` nodes ([lifecycle.md](lifecycle.md)) |

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

## Models

| Model | Fields |
|---|---|
| `GapContext` | `project_id`, `executor` (core store executor), `reader` (`GraphReader`), `gateway` (core `Gateway`), `ontology`, `rules` (`GapRules`), `records` (`GapRecords`), `amendments` (`Amendments`), `investigator` (`Investigator`), `changes: tuple[GapChangeReport, ...]`, `plan: PlanView \| None`, `settings: GapSettings`, `store` (run artifact store, for resume), `run_id: str \| None`, `on_progress` (optional callback of step name, done, total) |
| `GapSettings` | `enabled` (default `True`), `rules_path` (`None`: shipped rules), `investigator` (`"loop"` or `"agents"`, default `"loop"`), `auto_run_after_build` (default `True`), `ai_answers` (default `True`), `limits` (optional overrides of the rules' `limits`), `apply_quiet_seconds` (overrides the rules' value) |
| `GapChangeReport` | The gap-side copy of one build change report: `build_id`, `added`, `removed`, `merged` (old id to new id), `rekeyed` (old id to new id), `changed_meaning`, `changed_links`, `changed_evidence` (spec id tuples), `fragments_changed` (fragment ids by lineage), `documents` (lineage to revision id). The host converts the build's report; `graph.gap` never imports `graph.build` |
| `GapRunResult` | `run_id`, `mode`, `build_id`, `status` (`completed`, `failed`), `counts` (seeds by detector, sites investigated, cached, deferred, gaps new, follow-ups, AI answered, resolved, obsolete, notes), `cost` (calls and tokens per gateway role), `noise` (`NoiseReport`), `deferred_sites`, `held_tasks`, `warnings` |
| `NoiseReport` | Per section: lineage, heading path, relevance, signals, how it was decided (`rules`, `model`, `person`), and the node ids it removed from structural seeds |
| `GapEstimate` | `sites`, `cached`, `calls` and `tokens` per role |
| `GapView` | What reads return: the gap's properties, `about` and `impacts` (id, type, name), `witness` (fragment id, quote, lines), `options` (from `gap_option`), `clarification` (current one), `history` (events, follow-up chain), `reach` (stories and tasks found by trace and the plan, computed at read time) |
| `AnswerInput` | `gap_id`, `option_rank` or `own_text` (exactly one), `placement` (`amend`, `document`; default from the option's edits), `edits` (optional: edits confirmed from `preview_edits`), `answered_by` |
| `NewGap` | `kind`, `question`, `about` (spec ids), `quotes` (fragment id and quote, optional), `severity` (optional), `origin` (`person` or `development`), `blocking`, `story_id` and `claim_token` (development), `created_by` |
| `ReconcileResult` | Per amendment: `amendment_id`, outcome (`applied`, `reanchored`, `absorbed`, `conflict`, `orphaned`), the new anchor when re-anchored; and the effective text the build must read |

## Gap service

`GapService(executor, reader, ontology, rules, records, amendments, *, gateway, investigator, holds: StoryHolds | None, builds: BuildRequests, settings)`
serves the inbox, the routes and the MCP tools.

| Call | Returns | Errors |
|---|---|---|
| `list_gaps(project_id, *, status=None, kind=None, severity=None, origin=None, route=None, about=None, story_id=None, cursor=None, limit=50)` | A page of `GapView` (no history) and the next cursor; `about` filters by spec ids (aliases resolved), `story_id` by the nodes the story's tasks implement | |
| `get_gap(project_id, gap_id)` | `GapView` with history and reach | `GapNotFound` |
| `preview_edits(project_id, gap_id, *, option_rank=None, own_text=None, placement=None)` | The edits an answer would make, with the target section's current text and the text after; for own text, AI drafts the edits (role `gap_edits`) | `GapNotFound`, `AnswerInvalid` (empty text, too long, anchor not found) |
| `answer_gap(project_id, answer: AnswerInput)` | The clarification id and the scheduled apply time | `GapNotFound`, `GapStateError` (gap not `open` or `answered`), `AnswerInvalid`, `StaleGap` (the gap's witness changed since it was shown: the person must reload) |
| `dismiss_gap(project_id, gap_id, *, reason, by)` | Nothing | `GapNotFound`, `GapStateError` (not `open`) |
| `revert_clarification(project_id, clr_id, *, by, reason)` | The follow-up gap id | `GapNotFound`, `GapStateError` (not `applied` or `absorbed`) |
| `raise_gap(project_id, new: NewGap)` | The gap id; with `blocking`, the released claim | `AnswerInvalid` (unknown ids, quote not found), plan errors of the hold (`TaskStateError`) pass through |
| `apply_now(project_id, *, by)` | The gap ids moved to `applying` and the requested build | `GapStateError` when nothing is pending |
| `open_counts(project_id, spec_ids)` | Open gaps per spec id | Satisfies the plan's `FindingsReads` |
| `findings(project_id, *, spec_ids=None, limit=50)` | A page whose items satisfy the plan's `FindingLike` ([lifecycle.md](lifecycle.md) "Plan link") | Satisfies the plan's `FindingsReads` |
| `noise_rules(project_id)`, `add_noise_rule(project_id, scope, value, reason, by)`, `remove_noise_rule(project_id, rule_id, by)` | The rules; the new rule id | `AnswerInvalid` (pattern does not compile) |

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
| `GapNotFound` | The gap or clarification id is unknown in the project |
| `GapStateError` | The call does not apply to the current status; `status` and `allowed` name why |
| `AnswerInvalid` | An answer, a raised gap or a noise rule fails validation; `field` and `reason` name it |
| `StaleGap` | The gap changed after the caller read it (its `updated_at` differs) |

All are `GraphError` subclasses and carry a stable `code`.

## Ports

```python
class GapRecords(Protocol):
    """The relational rows of graph.gap (host: PostgreSQL)."""
    async def options(self, project_id: str, gap_id: str) -> Sequence[OptionRow]: ...
    async def put_options(self, project_id: str, gap_id: str, rows: Sequence[OptionRow]) -> None: ...
    async def start_run(self, project_id: str, run: RunRow) -> None: ...
    async def finish_run(self, project_id: str, run_id: str, counts: Mapping[str, Any], cost: Mapping[str, Any]) -> None: ...
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
    async def request_update_build(self, project_id: str, reason: str, immediate: bool) -> str: ...

class PlanView(Protocol):
    """What the gap run reads of the plan: tasks per spec node and their status (host: plan reader)."""
    async def tasks_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[TaskRef]]: ...
    async def stories_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[str]]: ...
```

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
| `GET /projects/{pid}/gaps` | `list_gaps` (query: status, kind, severity, origin, route, about, story_id, cursor, limit) |
| `GET /projects/{pid}/gaps/{gap_id}` | `get_gap` |
| `POST /projects/{pid}/gaps` | `raise_gap` (origin `person`) |
| `POST /projects/{pid}/gaps/{gap_id}/edits_preview` | `preview_edits` |
| `POST /projects/{pid}/gaps/{gap_id}/answer` | `answer_gap` |
| `POST /projects/{pid}/gaps/{gap_id}/dismiss` | `dismiss_gap` |
| `POST /projects/{pid}/gaps/clarifications/{clr_id}/revert` | `revert_clarification` |
| `POST /projects/{pid}/gaps/apply` | `apply_now` |
| `POST /projects/{pid}/gap/runs` | Start a full run (202, run status) |
| `GET /projects/{pid}/gap/runs/latest` | Run status and the last `GapRunResult` |
| `GET /projects/{pid}/gap/runs/{run_id}/noise` | The run's noise report |
| `GET /projects/{pid}/gaps/noise_rules` | `noise_rules` |
| `POST /projects/{pid}/gaps/noise_rules` | `add_noise_rule` |
| `DELETE /projects/{pid}/gaps/noise_rules/{rule_id}` | `remove_noise_rule` |
| `GET /projects/{pid}/documents/{document_id}/export?answers=1` | The document's text with its active amendments applied ([reconcile.md](reconcile.md) "Download with answers") |

The existing `/projects/{pid}/findings` routes stay as aliases of `GET .../gaps?origin=development` and of
dismiss for one release, then go.

## MCP tools

| Tool | Change |
|---|---|
| `add_finding` | Kept with its arguments; mapped to `raise_gap` with origin `development` ([lifecycle.md](lifecycle.md) "Development gaps"); the answer returns the gap id |
| `list_gaps` (new, read-only) | Open gaps about a story's nodes or about given spec ids: id, kind, severity, question, status, and the applied answer's text once resolved |
| `get_story_brief`, `get_task_brief`, `preview_story` | Unchanged calls; their findings now come from gaps (severity, question, `ABOUT` ids), blocking first |
| `get_node` | Lists open gaps about the node |
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
