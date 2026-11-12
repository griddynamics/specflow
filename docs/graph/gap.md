---
type: Design
title: "Graph gap findings"
description: "graph.gap, first slice: findings written by agents beside the spec graph: ontology file, nodes, identity, limits, dedupe, rebuild, the blocking link and the library API"
status: stable
tags: [graph, gap]
---
# gap.md: graph.gap, first slice: findings beside the spec graph, their ontology file, rules and library API

A coding agent learns things the spec does not say: a gap, two statements that disagree, a question
only the spec owner can answer, or a fact worth keeping. It records them as **findings**: nodes linked
to the spec nodes they are about and to the PR, commit, file or URL they come from. A finding never
changes the spec; people resolve or dismiss it.

Findings are the first slice of `graph.gap`, the submodule [later.md](later.md) reserves for gaps,
contradictions and findings. This slice declares the gap ontology file and its two types with origin
`agent`. SpecGap analysis (origin `analysis`) and findings people enter (origin `human`) come later and
write the same types under the same owner; clarifications stay in [later.md](later.md).

| Rule | Detail |
|---|---|
| Imports | `graph.gap` imports `graph.core` only, never `graph.build` or `graph.plan` ([architecture.md](architecture.md)); the import-linter contract "Graph submodules are independent" covers `graph.gap` too |
| Writers | Agents through the host's `add_finding` tool; people resolve and dismiss in the web app; never an agent |
| Never | Read by a build or by the planner; a write to a spec node, edge, document or fragment |
| Names | Its own labels, relations and vocabulary values may be literals in `graph.gap`, like the plan's ([plan/ontology.md](plan/ontology.md) "Names") |
| Logs | Ids, counts and codes only; finding text, notes and `refers_to` values never |

## Ontology file

`graph/gap/ontology.yaml`, format 2, `owner: gap`, `version` 1.0, shipped as package data and merged by
`load_ontology` ([ontology.md](ontology.md) "Ontology files of submodules"). `OWNER_ORIGINS["gap"]`
becomes {`analysis`, `human`, `agent`}.

| Declares | Content |
|---|---|
| Node types | `Finding` (origin `agent`, tier `governance`), `ExternalRef` (origin `agent`, tier `context`); no role, not extracted |
| Relations | `ABOUT`: `Finding` → every extracted label of the build file (`Module`, `Feature`, `Capability`, `Interface`, `DataEntity`, the requirement group, `OpenQuestion`) and `Fragment`. `REFERS_TO`: `Finding` → `ExternalRef`. Both origin `agent`, tier `governance` |
| Vocabularies | `finding_kind`: gap, conflict, question, note. `finding_status`: open, resolved, dismissed. `external_ref_type`: pr, commit, file, url. `finding_origin`: agent, analysis, human |

The type's declared origin sets its owner only. Each `Finding` also stores its own `origin`, so
analysis findings later share the type.

**Fingerprint.** Extraction fingerprints cover extracted types and relations only, and nothing in this
file is extracted, so adding or changing it leaves every fingerprint unchanged and re-extracts nothing.
`check_frozen` compares build-owned labels and relations only ([plan/questions.md](plan/questions.md)
Q17), so it never blocks a build either.

## Finding

`:Finding`, one per finding; a foreign node ([data-model.md](data-model.md) "Constraints and indexes").

| Property | Meaning |
|---|---|
| `uuid`, `finding_id` | Storage key; external id `FND::` and 12 random upper-case hex digits, unique per project (a clash draws again) |
| `kind` | `gap` (the spec is silent where code needs a decision), `conflict` (statements disagree), `question` (the spec is ambiguous), `note` (a fact learned, nothing to decide) |
| `text` | 1-2000 characters; never logged |
| `origin` | `agent` in this slice |
| `status` | `open`, `resolved` or `dismissed`; a closed finding is history and never reopens |
| `blocking` | True when the finding blocked its story's tasks ("Blocking link"); kept after it closes |
| `about_spec_ids` | 1-20 `spec_id`s, current ids after alias resolution, sorted; mirrors `ABOUT` |
| `story_id` | The story the author named, if any; required when `blocking` |
| `created_by`, `created_at` | Author's user id (1-200 printable characters) and time |
| `resolved_by`, `resolved_at`, `resolution_note` | Who closed it, when, and an optional note (at most 500 characters, never logged) |
| `orphaned` | True while an `about` id resolves to no node ("Rebuild") |
| `dedupe_key` | sha256 hex of the dedupe tuple ("Dedupe"); present only while `open` |
| `updated_at` | Write stamp |

## ExternalRef and relationships

`:ExternalRef`: `uuid`, `type` (`external_ref_type`), `value`; one node per (project, type, value),
shared by the findings that cite it.

| Type | From → To | Properties | Meaning |
|---|---|---|---|
| `ABOUT` | `Finding` → spec node or `Fragment` | `edge_key` | What the finding is about |
| `REFERS_TO` | `Finding` → `ExternalRef` | `edge_key` | Where it comes from |

`edge_key` is `<finding_id>|ABOUT|<spec_id>` and `<finding_id>|REFERS_TO|<ExternalRef uuid>`. When build
merges spec nodes the `ABOUT` edges move to the survivor; when it deletes a node they go with it
([architecture.md](architecture.md) W3, W4).

| Value | Rule |
|---|---|
| `Finding.uuid` | `uuid5(NAMESPACE, "<project_id>\|gap\|<finding_id>")`, the core `NAMESPACE` |
| `ExternalRef.uuid` | `uuid5(NAMESPACE, "<project_id>\|gap\|ref\|<type>\|<value>")` |

**Constraints and indexes.** Names start with `graph_gap_`; DDL at `graph/gap/ddl/gap_schema.cypher`,
discovered by the core `ensure_schema` as the plan's is. Unique `Finding (group_id, finding_id)`
`graph_gap_finding_id`, and the core foreign-label constraints on `(group_id, uuid)` of both labels;
range indexes `Finding (group_id, status)` `graph_gap_finding_status` and `Finding (group_id,
dedupe_key)` `graph_gap_finding_dedupe`; `(group_id, edge_key)` on both relations. Per-store mapping and
queries as for plan nodes ([plan/data-model.md](plan/data-model.md) "Per-store mapping"); SQL ports in
`stores/postgres/queries/gap/`.

## Rules

| Rule | Detail |
|---|---|
| Limits | `text` 1-2000 characters, `about` 1-20 ids, `refers_to` 0-10 entries; at most 50 open findings per author and story (per author in the project without a story), else `finding_limit` |
| About | Every id resolves through the registry aliases to an active spec node, else `node_not_found` naming the ids; stored as the current ids |
| Dedupe | Tuple: author, `kind`, sorted current `about` ids, normalised text (NFKC, case-folded, whitespace runs as one space, trimmed). An `add` whose tuple matches an open finding returns that finding with `created` false and writes nothing. Two identical adds at the same moment may both be written |
| `refers_to` | `pr`: an `http` or `https` URL or a PR number (digits); `commit`: 7-64 hex, stored lower case; `file`: a relative path, at most 500 characters, no `..` segment, no leading `/`, no `\`, no control characters; `url`: `http` or `https` with a host, no user info, at most 2000 characters. Never fetched or opened |
| Transitions | `open` → `resolved` (`resolve`) or `dismissed` (`dismiss`), by a person, with an optional note; anything else is `wrong_state` |

**Rebuild.** After each build that returns a result the host calls `Findings.refresh(project_id)`
(`api/graph_build/runner.py`, `refresh_findings`); a build that raises triggers no refresh. For each open
finding it resolves `about_spec_ids` through the aliases to the current ids, restores a missing `ABOUT`
edge to a node that is back, and sets `orphaned` to whether any id resolves to no node. An orphaned
finding is kept, with the gone id in its list. `refresh` answers `FindingRefresh`: `refreshed` (open
findings looked at) and `orphaned` (those left orphaned). A refresh failure is logged (event
`api.graph_build.findings_refresh_failed`, error code and type only) and never fails the build: findings
never block a knowledge build.

## Blocking link

A blocking finding stops the work it is about: the claimed story's tasks become `blocked` and the claim
ends. `graph.gap` never imports `graph.plan`, so the composition is `graph.gap.FindingFlow`: it takes a
`Findings` and an object with the structural `StoryBlocks` methods (`check_block`, `block_story`,
`unblock_finding`), which the host fills with `PlanService`. The flow is library code, tested end to end
without the API; the API only calls it ([plan/lifecycle.md](plan/lifecycle.md) "Story claims").

| Kind | Block reason |
|---|---|
| `question` | `spec_ambiguous` |
| `conflict` | `spec_contradiction` |
| `gap` | `spec_missing` |
| `note` | Never blocking |

1. `Findings.match`: an open finding with the same dedupe tuple is the answer (`created` false); nothing
   else happens, so a repeat never meets the guard.
2. Guard: `kind` is not `note`; `story_id` and `claim_token` are given, the caller holds the story with
   that token; at least one `about` node is implemented by a task of the story
   (`PlanService.check_block`). A `kind` of `note` or a missing `story_id` is refused by `Findings.add`
   (`invalid_kind`, `invalid_story`) before anything is written. A missing token never matches: the guard
   answers `claim_mismatch`. The guard runs before the add, so a blocking add about an id that names no
   node of the story answers `claim_mismatch`, not `node_not_found`, and nothing is written.
3. `Findings.add(..., blocking=True, story_id=...)`. When another caller wrote the same finding between
   steps 1 and 3, the answer is that finding with `created` false.
4. `PlanService.block_story(story_id, claim_token, reason, finding_id)`.
5. When step 4 fails, `Findings.withdraw` deletes the finding of step 3 and the flow raises step 4's
   error. A failed withdraw is logged (`graph.gap.withdraw_failed`) and does not hide that error.

Resolving or dismissing a blocking finding is followed by `PlanService.unblock_finding(finding_id)`:
tasks still `blocked` with that `blocked_by` return to `pending`. Tasks a plan run already reset (to
`pending`, `blocked_by` cleared) are untouched ([plan/plan.md](plan/plan.md) "Diff"). Closing a finding
that is already closed raises `wrong_state` after repeating the unblock for a blocking one, so a lost
unblock heals.

`FindingFlow(findings, plan)`, exported from `graph.gap` with `NewFinding`, `FlowAdded`, `FlowClosed` and
`StoryBlocks`:

| Method | Contract |
|---|---|
| `add_finding(project_id, new: NewFinding, *, claim_token=None) -> FlowAdded` | `NewFinding` holds `created_by`, `kind`, `text`, `about`, `refers_to`, `story_id`, `blocking`. Not blocking: `Findings.add`. Blocking: steps 1 to 5. `FlowAdded`: `finding`, `created`, `blocked` (the task ids it blocked; none for a repeat). Errors: those of `Findings.add`, and the plan's `claim_mismatch` from the guard or the block |
| `close_finding(project_id, finding_id, *, by, note=None, dismiss=False) -> FlowClosed` | `resolve` or `dismiss`, then `unblock_finding` for a blocking finding. `FlowClosed`: `finding`, `unblocked` (the task ids back to `pending`) |

## Library: Findings

`Findings(store_or_executor, ontology, *, database=None, clock=None)`, exported from `graph.gap`; all methods
`async`. Argument checks run before any query and raise `ConfigurationError`: `invalid_kind`,
`invalid_text`, `invalid_about` (`about` outside 1-20 ids, a `spec_ids` filter of more than 200, or an id that is empty, longer
than 200 or not printable), `invalid_ref`, `invalid_note`, `invalid_author` (`created_by` and `by` as the plan's
`claimed_by`), `invalid_story` (a bad `story_id`, or a blocking finding without one), `invalid_status` (a
`status` filter that is none of `open`, `resolved`, `dismissed`, `all`), `invalid_limit` (a `limit` outside 1-200
or an `after` that is not a cursor this library issued). State refusals raise `FindingError` with a code.

| Method | Contract |
|---|---|
| `add(project_id, *, created_by, kind, text, about, refers_to=(), story_id=None, blocking=False) -> AddedFinding` | Dedupe, then limits, then one transaction: the `Finding`, its `ABOUT` edges, the `ExternalRef` nodes (merged) and `REFERS_TO` edges. `AddedFinding`: `finding`, `created`. `blocking` needs `story_id` and a kind other than `note` (`invalid_kind`) |
| `match(project_id, *, created_by, kind, about, text) -> Finding \| None` | The open finding with the same dedupe tuple |
| `withdraw(project_id, finding_id) -> bool` | Deletes an open finding with its edges, and `ExternalRef` nodes no other finding cites; host compensation only ("Blocking link") |
| `resolve(project_id, finding_id, *, by, note=None) -> Finding`, `dismiss(...)` | The transitions of "Rules"; clears `dedupe_key` |
| `finding(project_id, finding_id) -> Finding \| None` | One finding |
| `findings(project_id, *, status="open", spec_ids=None, story_id=None, created_by=None, blocking=None, limit=50, after=None) -> FindingPage` | Blocking first, then newest first, then `finding_id`; `status` is a finding status or `all`; `spec_ids` (at most 200) match through aliases; `limit` 1-200; `after` is the page's opaque `next` (an encoded position in that order). Filters, sorting and paging run in Python after the database read: finding volume per project is small. Times are stored as ISO text to the millisecond. `FindingPage`: `findings`, `next` (none on the last page) |
| `open_counts(project_id, spec_ids) -> dict[str, int]` | Open findings per asked id (aliases resolve; keys as asked) |
| `refresh(project_id) -> FindingRefresh` | "Rebuild"; `FindingRefresh` holds `refreshed` and `orphaned` |

`Finding` holds the properties above except `uuid` and `dedupe_key`, plus `refers_to` (`type`,
`value`). `FindingError` codes: `node_not_found` (with `spec_ids`), `finding_limit`, `unknown_finding`,
`wrong_state`. Events `graph.gap.finding_added`, `graph.gap.finding_closed`,
`graph.gap.finding_withdrawn` and `graph.gap.findings_refreshed` carry ids, codes and counts.

## Requirements

FR-GAP-01 to FR-GAP-12 in [requirements.md](requirements.md) "Findings".
