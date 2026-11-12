---
type: Design
title: "Graph trace"
description: "Directed trace over the spec graph: what a set of nodes is based on and what it affects, the ontology field that drives it, the library call, the plan overlay and the MCP tool"
status: stable
tags: [graph, plan, mcp]
---
# trace.md: directed trace over the spec graph, its ontology field, the library call, the plan overlay and the MCP tool

A coding agent starts from a brief: the nodes its task or story implements, their source quotes and the
neighbours one domain edge away ([plan/lifecycle.md](plan/lifecycle.md) "Task brief"). That slice does
not say what the work relies on two steps away, or what it will break. `trace` answers both questions
by walking the spec graph in one direction, by the meaning of each relation, and returns the path to
every node it reaches. The plan overlay then adds each node's tasks, so the answer also says who built
it, who is building it and who waits for it.

`GraphReader.impact` ([api.md](api.md) "Read and search") stays as it is: it walks every relation in
both directions, so its answer mixes dependencies and dependants. Nothing in the host calls it today.

## Questions it answers

| Question | Call |
|---|---|
| What will my story affect? | `trace(story, direction="affects")` |
| What is my story based on: contracts, data, stores, rules? | `trace(story, direction="based_on")` |
| Who has built, is building or waits for those nodes? | the plan overlay on either answer |
| Which open questions touch them? | the plan overlay's `open_questions` |

## Terms

| Term | Meaning |
|---|---|
| Seeds | The nodes a trace starts from: the given `spec_id`s, or the nodes a story's tasks implement |
| Direction | `based_on`: what the seeds depend on. `affects`: what depends on the seeds |
| Role | How one relation pair carries a change; read from the ontology field `impact` ("Impact roles") |
| Hop | One edge from the frontier. Seeds and owner starts are hop 0; a context node that is no owner start has a hop only when a step reaches it |
| Spine root | A node of a type in the spine's `roots`, a module in the shipped ontology ([ontology.md](ontology.md) "Spine"). Spine roots are the hubs: almost every node reaches one |
| Context | The owners of the seeds, up to the first spine root (inclusive). Listed in `context`, never walked upward. A context node that is no start still appears in `nodes` when a step reaches it |
| Scope step | Going from a reached node to its members (the sources of its owner pairs), one level |
| Owner start | A spine root in the context, also used as a start; nodes first reached from it carry `via_owner` |

## Impact roles in the ontology

Relations get a new field `impact`, and pairs may override it. Code reads the role and never a relation
name, so invariant I4 holds ([architecture.md](architecture.md)): the walk below works for any ontology.

| Role | Meaning for an edge from A to B | `based_on` walks | `affects` walks |
|---|---|---|---|
| `uses` | A depends on B | A → B | B → A |
| `used_by` | B depends on A | B → A | A → B |
| `coupled` | Each depends on the other (an implementation lives on the other end) | both ways | both ways |
| `related` | A weak association the text makes | both ways, marked `weak` | both ways, marked `weak` |
| `contains` | A is part of B | never upward; downward only as the scope step | same |
| `none` | Never walked | - | - |

| Rule | Detail |
|---|---|
| Where | `impact` on a relation is the default for its pairs; `impact` on a pair overrides it. A relation without `impact` is `none`, so an older ontology still loads and `trace` simply does not walk it |
| Symmetric relations | The edge is stored in canonical order, so the role comes from the pair whose `from` and `to` match the edge's end labels as they are met. Both orientations of a symmetric pair are declared (check 10); their roles must be inverses: `uses` with `used_by`, every other role with itself. `contains` is not allowed on a symmetric relation |
| Checks | New loading checks ([ontology.md](ontology.md) "Loading and validation", checks 17 to 19): the value is one of the six roles (an unknown value fails the file's format check); the symmetric inverse rule; `contains` only on a relation that has at least one `owner` pair or is named by a spine path. A failed check raises `OntologyError`, as the others do |
| Version | A new optional field: `format_version` stays 2; the shipped ontology's `version` goes from 2.0 to 2.1 (a minor bump: the field is read-only and changes no extraction, so no project is migrated, A44). The field is part of the ontology structure, so the change is made here first ([ontology.md](ontology.md) "Shipped ontology"). Every project's plan shows `outdated` once after the deploy, because the plan fingerprint includes the ontology version; the next plan run clears it and reuses everything |

### Shipped roles

| Relation | Role | Why |
|---|---|---|
| `DEPENDS_ON` | `uses` | The text says the source needs the target |
| `INTEGRATES_WITH` | `uses` | Stored from the calling module to the called one |
| `DEPLOYED_TO` | `uses` | A module needs the environment it runs in |
| `CARRIES` | `uses` | An interface needs the shape of its payload |
| `CONSUMED_BY` | `used_by` | The consuming module depends on the interface |
| `PROVIDED_BY` | `coupled` | The provider implements the contract: a change of either reaches the other |
| `STORED_IN` | `coupled` | The store holds the entity: a schema change reaches both |
| `PART_OF` | `contains`; pairs from a requirement to a module (owner rank 2): `used_by` | A requirement a module owns directly, with no feature, binds everything in that module |
| `GROUPED_BY`, `REALIZED_BY` | `contains` | Grouping and realisation, not dependency |
| `RELATES_TO` | `related`; requirement → interface and requirement → data entity: `uses` (reverse pairs `used_by`); every open question pair: `none` | A requirement that names an interface or entity relies on it; open questions are reported by the plan overlay, not walked |

## Walk

1. **Seeds.** Unknown ids are listed in `unknown` and skipped; aliases resolve, as in `node`.
2. **Context.** For each seed, follow owner pairs upward to the first spine root, at most as many
   levels as the longest spine path (3 in the shipped ontology), one read per level. Owners that are
   not seeds are the `context`. Being context does not put a node into `nodes`, but a context node
   that a step reaches does appear there (FR-READ-12). Contains edges are never walked upward. The
   basis rule of step 7 applies to this step too.
3. **Starts.** The seeds, plus the spine roots of the context when `include_owner` is true. Only
   these are hop 0; a context node that is no spine root is never a start.
4. **Steps.** Per hop, for each frontier node and each incident edge, take the edge's role and walk it
   only in the way the table above allows for the trace's direction. A node is visited once, at its
   lowest hop. An edge a role walks that is also an owner pair counts as a role step, not a scope
   step; a pair the ontology does not declare takes its relation's role.
5. **Scope step.** From a reached node that is not a spine root, its members (the sources of owner
   pairs that point at it) are reached one hop further, with `step: "scope"`. This applies to seeds
   too, so a trace from a feature alone still covers its requirements, and a spine root that is a
   seed lists its members (T8). The members of a spine root that is no seed are never listed.
6. **Hubs.** From a spine root that is no seed (an owner start or a reached module) the walk follows
   only `uses` and `used_by` edges. A module reached through a store or a provider therefore never
   fans out to every entity it stores, every interface it provides or everything related to it. In
   `affects`, a spine root reached over a `coupled` or `related` edge is listed but not walked on: a
   change of a contract or an entity lands in the module that provides or stores it, and what depends
   on that module through other contracts does not depend on this one (T9). A spine root that is a
   seed is walked like any other seed, every role and the scope step (T8).
7. **Trust.** `basis="trusted"` (the default) walks `stated` and `implied` edges only, the edges stitch
   trusts for waits ([plan/plan.md](plan/plan.md) "Stitch"); an edge without `basis` reads as `stated`.
   The filter covers the context step (2) as well as the role steps. `basis="any"` adds `inferred` edges, each result carrying the rule and confidence.
8. **Order and limits.** Results are ordered by hop, then `spec_id`. `depth` is 1 to 3; at most `limit`
   nodes are returned and `truncated` says whether more were reachable. The parent of a node is the
   frontier node with the lowest `spec_id` that reached it at its hop (then the relation name, then the step); a path
   from a seed wins over one from an owner start at the same hop. The same graph gives the same answer.

The `relations` argument restricts steps 4 and 5 to the listed relations; context (step 2) always uses
the owner pairs.

## Library: `GraphReader.trace`

`trace(project_id, spec_ids, *, direction, depth=2, basis="trusted", relations=None, include_owner=True, limit=200) -> TraceResult`

| Argument | Rule |
|---|---|
| `spec_ids` | 1 to 50 ids, else `invalid_spec_ids`; aliases resolve |
| `direction` | `based_on` or `affects`; anything else raises `ConfigurationError("invalid_direction")` |
| `depth` | 1 to 3, else `invalid_depth` (as `impact`) |
| `basis` | `trusted` or `any`, else `invalid_basis` |
| `relations` | Domain relations of the ontology, else `unknown_relation` |
| `limit` | 1 to 500, else `invalid_limit` |

`TraceResult`: `sources` (resolved seeds), `unknown`, `direction`, `context` (`spec_id`, label, name of
each owner), `nodes`, `truncated`. `TraceNode`: `spec_id`, `label`, `name`, `hops`, `parent`
(`spec_id`), `relation`, `step` (`forward`, `backward` or `scope`: the edge's stored direction as
walked), `role`, `basis`, `confidence` and `rule` (inferred edges only), `weak` (a `related` edge on
the path), `via_owner`, `spine_root`. Results never carry vectors or document text; `get_node` and the
brief carry provenance.

**Queries.** One new store query, `core.read_trace_hop` (Cypher for Neo4j and ArcadeDB, SQL for
PostgreSQL), returns the incident domain edges of a frontier: frontier `uuid`, the other end's `uuid`,
`spec_id`, labels, `name`, the relation, whether the edge leaves the frontier node, `basis`,
`confidence` and `inference_rule`. It takes the relations to read, so the reader asks for the edges of a spine
root that is no seed with the relations that have a `uses` or `used_by` pair only (rule 6) and never
reads such a module's members. Roles are applied in Python from the compiled ontology. Owners (rule 2) take one read
of the owner pairs per level, at most three in the shipped ontology. A hop takes at most two queries:
one for the frontier's spine roots and one for the other nodes.

## Plan overlay: `PlanService.trace`

`trace(project_id, *, story_id=None, spec_ids=None, direction, depth=2, basis="trusted", relations=None, include_owner=True, limit=100) -> PlanTrace`

| Part | Content |
|---|---|
| Seeds | Exactly one of `story_id` and `spec_ids` (else `ConfigurationError("invalid_arguments")`). A story's seeds are its anchor and the nodes its open and completed tasks implement, merged ids resolved, at most 50, the library's `spec_ids` limit (`seeds_truncated` says some were left out). A retired or unknown story raises `PlanReadError("story_not_found")` |
| Per node | The node's tasks, open and completed (closed tasks are history): `task_id`, `story_id`, `status`, `claimed_by` while `in_progress`, `commit` when `completed`. Never a claim token. Context nodes get the same. Also `findings`: the node's open finding count, 0 without a findings port |
| `plan_state` per node | `completed` (every task completed), `in_progress` (any task in progress), `open` (any task pending, failed or blocked), `unplanned` (no task: not plannable, excluded or external) |
| `stories` | The stories of those tasks other than the traced one, in `story_id` order: `story_id`, title, area, derived progress, and the holders of its `in_progress` tasks |
| `summary` | Reached nodes (`nodes`, not the context or the seeds) per `plan_state`; null without a plan |
| `open_questions` | Open questions related to a seed or a reached node, chosen by the same rule as the brief's ([plan/lifecycle.md](plan/lifecycle.md) "Task brief") |

A project without a plan still traces: the plan fields are null, `stories` is empty, and only a
`story_id` argument raises `no_plan`.

**Findings.** The overlay carries each node's open finding count (`findings`), also on the seeds' context.
`graph.plan` never imports `graph.gap`: `PlanService` asks a structural `FindingsReads` port for
`open_counts` over the sources, the context and the reached nodes in one call, and the host fills the port
with `graph.gap.Findings` ([gap.md](gap.md) "Library: Findings"; [plan/api.md](plan/api.md) "Plan service"). A
service built without a port counts 0. The plan reads one graph trace, one plan load and one count call
for the whole answer.

## MCP tool: `trace`

| Argument | Type | Default | Rule |
|---|---|---|---|
| `project_id` | string | required | as for `list_features` ([../mcp-server/tools.md](../mcp-server/tools.md)) |
| `story_id` | string or null | null | exactly one of `story_id` and `spec_ids` |
| `spec_ids` | list of strings or null | null | 1 to 20 ids |
| `direction` | `based_on` or `affects` | required | |
| `depth` | integer | 2 | 1 to 3 |
| `basis` | `trusted` or `any` | `trusted` | |
| `include_owner` | boolean | `true` | also start from the owning module |
| `relations` | list of strings or null | null | relation names of the project's ontology |
| `limit` | integer | 100 | 1 to 200; no cursor: narrow with `depth` or `relations` instead |

```json
{
  "direction": "affects",
  "sources": ["FEAT::CARD_PAYMENT", "REQ::FR::CARD_PAYMENT"],
  "context": [{"spec_id": "MOD::PAYMENTS_SERVICE", "label": "Module", "name": "Payments service"}],
  "nodes": [
    {
      "spec_id": "FEAT::CHECKOUT", "label": "Feature", "name": "Checkout", "hops": 1,
      "parent": "FEAT::CARD_PAYMENT", "relation": "DEPENDS_ON", "step": "backward", "role": "uses", "basis": "stated",
      "via_owner": false, "plan_state": "open", "findings": 0,
      "tasks": [{"task_id": "TASK::WEB_SHOP::LOGIC_CHECKOUT", "status": "pending"}]
    }
  ],
  "stories": [{"story_id": "STORY::WEB_SHOP::CHECKOUT", "title": "Checkout", "progress": "pending", "holders": []}],
  "summary": {"completed": 0, "in_progress": 0, "open": 1, "unplanned": 0},
  "open_questions": [],
  "unknown": [],
  "truncated": false
}
```

The plan fields in this example (`plan_state`, `tasks`, `stories`, `findings`) are illustrative; the
graph fields are the fixture's ("Worked example"). `findings` is the node's open finding count, context
nodes included.

Behavior:

- The membership check runs first, as for every project tool. Read-only, idempotent and closed-world
  annotations (FR-MCP-TOOL-09); one `mcp.tool.completed` audit event.
- The tool checks `story_id`/`spec_ids` and `relations` itself before it calls the library, because
  the library's `ConfigurationError` maps to `internal_error` today.
- Unknown `spec_ids` are listed in `unknown`, not an error; a call where none resolves answers with no
  nodes.

Errors: `project_not_found`, `invalid_arguments` (both or neither of `story_id` and `spec_ids`),
`unknown_relation`, `story_not_found`, `no_plan` (only with `story_id`), `graph_unavailable`,
`database_unavailable`, `internal_error`. `invalid_arguments`, `unknown_relation` and `story_not_found`
are new rows for the Error codes table of [../mcp-server/tools.md](../mcp-server/tools.md).

Description given to the model: [../mcp-server/tools.md](../mcp-server/tools.md#trace).

## Worked example: the shop fixture

On `backend/tests/fixtures/graph/plan/shop.yaml`, seeds `FEAT::CARD_PAYMENT` and
`REQ::FR::CARD_PAYMENT`. Context: `MOD::PAYMENTS_SERVICE` (the feature's owner). These tables are the
expected results of the library call and become its tests.

`direction="based_on", depth=3`:

| Hop | Node | Parent, relation, step | Why |
|---|---|---|---|
| 1 | `IFC::STRIPE_API` | `MOD::PAYMENTS_SERVICE`, `CONSUMED_BY`, backward; `via_owner` | The owning module consumes it |
| 1 | `IFC::UNSHARED` | `MOD::PAYMENTS_SERVICE`, `CONSUMED_BY`, backward; `via_owner` | Same |
| 2 | `MOD::STRIPE` | `IFC::STRIPE_API`, `PROVIDED_BY`, forward | The provider of a contract it relies on (`coupled`) |
| 2 | `REQ::INT::CALL_STRIPE` | `IFC::STRIPE_API`, `PART_OF`, scope | The interface's own requirement |
| 3 | `CON::STRIPE` | `MOD::STRIPE`, `PART_OF`, backward | A rule the Stripe module owns directly: "Stripe rejects repeated payment attempts" |

All five are `via_owner`: each path starts at the owning module. Not reached: `ENT::DUAL_STORED`
(stored in the Payments service; `STORED_IN` is `coupled` and never walked from a module) and
`CON::EU_REGION` (`related` to modules; it reaches agents as a project convention in the brief).

`direction="affects", depth=2`:

| Hop | Node | Parent, relation, step | Why |
|---|---|---|---|
| 1 | `FEAT::CHECKOUT` | `FEAT::CARD_PAYMENT`, `DEPENDS_ON`, backward | Checkout depends on card payment |
| 1 | `MOD::STRIPE` | `MOD::PAYMENTS_SERVICE`, `INTEGRATES_WITH`, backward; `via_owner` | Stripe calls the Payments service |
| 1 | `MOD::WEB_SHOP` | `MOD::PAYMENTS_SERVICE`, `INTEGRATES_WITH`, backward; `via_owner` | The web shop calls the Payments service |
| 2 | `REQ::FR::CHECKOUT` | `FEAT::CHECKOUT`, `PART_OF`, scope | In the scope of an affected feature |
| 2 | `REQ::Q::CHECKOUT_SPEED` | `FEAT::CHECKOUT`, `PART_OF`, scope | Same |

With `include_owner=false` the `based_on` trace is empty and the `affects` trace keeps only
`FEAT::CHECKOUT` and its scope. The fixture states feature-level dependencies rarely; most of what a
feature relies on sits on its module. That is why the owner start is on by default.

## Requirements

The units live in the requirement files of their areas: FR-ONT-07 (impact roles) and FR-READ-11 to
FR-READ-17 (the walk) in [requirements.md](requirements.md), FR-PMCP-08 (the plan overlay) in
[plan/requirements.md](plan/requirements.md), and the units of the `trace` tool in
[../mcp-server/requirements.md](../mcp-server/requirements.md).

## Decisions

Decided 2026-11-11: each proposal below was taken. T8 and T9 were added on 2026-11-12 from the MCP
tooling QA.

| # | Decision | Taken | Alternative |
|---|---|---|---|
| T1 | Where roles live | In the spec ontology, read by `graph.core`: trace is a graph read, useful beyond the plan, and the ontology already defines what each relation means | In `plan-rules.yaml`, as the first design page said; then only the plan could trace |
| T2 | `PROVIDED_BY` and `STORED_IN` | `coupled`, never walked from a module | `used_by`: then a store or provider reached would not lead back to its entities or interfaces at all |
| T3 | Requirements a module owns directly | `used_by`: they bind everything in the module, so `based_on` lists them through the owner (`CON::STRIPE` above) | Report only their count on the module; a module with many direct requirements could fill the limit |
| T4 | Constraints related to modules (`CON::EU_REGION`) | Not walked; they reach agents as conventions in the brief | Walk `related` edges from a module for constraint and quality types only |
| T5 | Owner start | On by default, results marked `via_owner` | Off by default; the fixture shows feature-level traces are often empty |
| T6 | Hubs | Spine roots only | Also any node above a degree threshold; that makes answers depend on graph size |
| T7 | `impact` | Keep unchanged; no host caller | Remove once `trace` lands |
| T8 | A module given as a seed (QA I08: `affects` of a module listed only the modules calling it) | Walked like any seed: its members, the interfaces it provides, the entities it stores and its weak relations, then their dependents | Keep it a hub and tell agents to use `based_on`; the question "what does changing this module touch" stays unanswerable |
| T9 | A module reached in `affects` over `coupled` or `related` (QA: `affects` of `IFC::PAYMENTS_REFUND` listed `MOD::CHECKOUT`, which never calls refund) | Listed, not walked on; the contract's consumers are reached from the contract | Walk it as a hub: every module calling the provider for any contract is listed |

## Out of scope here

`get_node`, `search_spec`, `similar`, `get_evidence`, `path` and `expand_brief`, the other graph tools of
the agent design, are specified in [../mcp-server/tools.md](../mcp-server/tools.md), their library reads
in [api.md](api.md) "Read and search". Code anchors (the
files each task's commit changed) would let a trace name files as well as nodes; they need a repository
connection first.

## Slices

1. Ontology: the `impact` field, loader checks, shipped roles, [ontology.md](ontology.md) tables.
2. Core: `core.read_trace_hop` for each store, `GraphReader.trace`, the fixture tests above, the
   `trace` row of [api.md](api.md) "Read and search" (the other reads of the agent tools are its rows
   `similar` to `expand`).
3. Plan: `PlanService.trace` and its models.
4. MCP: the `trace` tool in the registry, [../mcp-server/tools.md](../mcp-server/tools.md), the
   committed tool manifest, tool tests.
