---
type: Ontology
title: "Graph plan ontology"
description: "The plan ontology file, the plan rules file and what may change in them"
status: stable
tags: [graph, plan]
---
# ontology.md: the plan ontology file, the plan rules file, and what may change in them

`graph.plan` uses two shipped files. The ontology file declares what the plan writes, in the library's
ontology format; the rules file says how the spec graph (build ontology v2.0) is turned into a plan.

## Names: what code may know

| Names | Source | In code |
|---|---|---|
| Spec side: build types and groups, relations, attributes and their values | The merged ontology, reached only through the plan rules | Never literals (I4) |
| Plan side: labels `Plan`, `WorkArea`, `Story`, `Task`; relations `IN_AREA`, `IN_STORY`, `IMPLEMENTS`, `WAITS_FOR`, `SUPERSEDES`; the vocabularies of the plan ontology file (phases, task statuses and progress, complexity, area kinds and statuses, interface kinds, `planned_by`, story kinds, statuses and progress, `done_when` sources, `not_buildable` reasons) | Fixed by this spec, declared in the plan ontology file | Allowed as literals in `graph.plan` only, like the built-in names of the build ([../architecture.md](../architecture.md) I4); a test checks that each literal is declared in the shipped plan ontology file |

So `graph.plan` plans any spec ontology without code changes, while its own model is fixed
([questions.md](questions.md) Q18).

The task-order relation is `WAITS_FOR`, not `DEPENDS_ON`: the build ontology v2.0 declares a spec
`DEPENDS_ON` (feature → feature, requirement → requirement), and the core loader rejects a relation
declared in two files ([questions.md](questions.md) PT1).

## Plan ontology file

`graph/plan/ontology.yaml`, copy [plan-ontology.yaml](plan-ontology.yaml), `version` 2.0. It
uses the format of [../ontology.md](../ontology.md) unchanged (`format_version` 2), with `owner: plan`,
and is merged with the build file by `load_ontology` ("Ontology files of submodules"). The template
loads and validates with the current core loader together with the build ontology v2.0.

| Declares | Content |
|---|---|
| Node types | `Plan`, `WorkArea`, `Story`, `Task`; origin `planner`, tier `plan`, no role (not extracted) |
| Relations | `IN_AREA` (module, feature, capability, interface, data entity, requirement → `WorkArea`); `IN_STORY` (`Task` → `Story`); `IMPLEMENTS` (`Task` → module, feature, capability, interface, data entity, requirement; `Story` → feature, interface, data entity); `WAITS_FOR` (`Task` → `Task`); `SUPERSEDES` (`Task` → `Task`). All origin `planner`, tier `plan` |
| Vocabularies | `phase`, `task_status`, `task_progress`, `complexity`, `area_kind`, `area_status`, `interface_kind`, `planned_by`, `story_kind`, `story_status`, `story_progress`, `done_when_source`, `not_buildable_reason` |

Vocabularies:

| Vocabulary | Values |
|---|---|
| `phase` | setup, contract, data, logic, integration, quality, acceptance |
| `task_status` | pending, in_progress, completed, failed, blocked, superseded, retired |
| `task_progress` | done, skipped, todo (reported by a story claim's holder; [lifecycle.md](lifecycle.md) "Story claims") |
| `complexity` | low, medium, high |
| `area_kind`, `area_status` | module, project; active, retired |
| `interface_kind` | api, event, data, library, cli, config |
| `planned_by` | model, rules |
| `story_kind` | feature, interface, entity, core, chapter, setup, closing |
| `story_status` | active, retired (stored) |
| `story_progress` | pending, in_progress, blocked, completed (derived at read time, never stored; [data-model.md](data-model.md) "Story progress") |
| `done_when_source` | spec, planner |
| `not_buildable_reason` | process, governance, release_gate, policy_only |

Properties that are not prompt material (task and story claims, `progress`, commit, `completed_by`,
`blocked_by`, revisions, write stamps, `done_when`, `implements`, `release`, `interfaces`,
`not_buildable`) are not attributes; [data-model.md](data-model.md) lists them. Because the plan types and relations are declared, the core `ensure_schema` creates their
`(group_id, edge_key)` indexes and `OwnedWriter` accepts them for owner `plan` only. Nothing in this
file reaches an extraction prompt, so it does not change build extraction fingerprints.

The merged `Ontology.version` is the build file's. `graph.plan` reads the `version` of its own shipped
file and records it as `plan_ontology_version`; both versions are part of the graph fingerprint.

**Effect on builds.** A build records the merged ontology's build-owned labels and relations at each
`first` build, and an `update` build checks them in `check_frozen`. Decided
([questions.md](questions.md) PT2, Q17 B): `check_frozen` compares only build-owned labels and
relations, so installing or changing the plan ontology never blocks a build.

## Plan rules

`graph/plan/plan-rules.yaml`, copy [plan-rules.yaml](plan-rules.yaml), `version` 2.0.
`load_plan_rules(path=None, *, ontology)` loads the shipped file or another one and validates it against
the merged ontology; any problem raises one `ConfigurationError` that lists every problem.

| Key | Meaning |
|---|---|
| `format_version`, `name`, `version` | Format 1; `version` is part of the graph fingerprint |
| `types` | Plannable spec types (type keys or node groups) with a `role`: `area_anchor` (an internal node anchors a module area), `shared` (placed by the anchor that realises it), `member` (placed in one area and implemented by tasks), `context` (read for briefs, never placed or planned). Per type: `exclude`: attribute values that make a node not plannable (excluded when any listed attribute has one of its listed values); `phase`: default phase of a `member`; `required`: every node must be covered ([data-model.md](data-model.md) PD3; `member` only); `story`: every node anchors a story of that kind, `interface`, `entity` or `feature` (`member` only); `project_input`: shown to the project planner |
| `exclude_all` | Attribute values that make any node not plannable (excluded when any listed attribute has one of its listed values), checked on every type that declares the attribute; a node whose owner (the target of its `containment` edge) matches is excluded too |
| `relations` | The relations that play the roles `containment` (single owner), `grouping`, `realisation` (capability → module), `integration` (caller → callee), `provision` (interface → provider), `consumption` (interface → consumer), `storage` (data entity → storing module), `deploy` (module → the deployment module it runs on), `dependency` (spec dependency between features or requirements), `reference` (symmetric reference) |
| `release` | The attribute that holds a node's release; carried on tasks and stories, a null or missing value taken from the owner |
| `assumption` | The attribute and value that mark a node as assumed rather than committed |
| `acceptance_criteria` | The attribute that holds a node's acceptance criteria; the source of `done_when` items with source `spec` |
| `phases` | Every phase, in order, with its fallback waits (`after`, `after_mode` `first` or `all`) and default `complexity` |
| `phase_guidance` | Text given to the task planner |

Where the roles are used: Partition places nodes by `containment`, `grouping`, `realisation`,
`provision`, `consumption`, `storage`, `integration` and `reference`; Stitch adds waits from
`consumption` (towards the area of the interface), `integration`, `dependency`, `deploy`, and
`containment` with `reference` (data tasks); briefs show
neighbours over every role ([plan.md](plan.md) "Partition", "Stitch").

Checks:

1. Known keys only; values of the right type.
2. Every type, group, relation, attribute and attribute value resolves in the merged ontology; no
   plannable or context type and no relation role is owned by `plan`.
3. Exactly one `area_anchor` type; every `member` type has a `phase`; `required` and `story` only on
   `member` types; a `context` type has no `phase`, `required`, `story`, `project_input` or `exclude`.
4. Every `area_anchor`, `shared` and `member` type is a legal source of `IN_AREA` and a legal target of
   `IMPLEMENTS` from `Task`; every `story` type is a legal target of `IMPLEMENTS` from `Story`.
5. Every relation role is set, and no two roles name the same relation. `realisation`, `integration`,
   `provision`, `consumption`, `storage` and `deploy` each have a pair that ends at the
   `area_anchor` type; `containment` has a pair from every `member` type that is not a `story` type.
6. The `exclude_all` attributes and the `release` and `acceptance_criteria` attributes are declared by at least
   one plannable type; `assumption` is a mapping whose `attribute` a plannable type declares and whose
   `value` is a string.
7. `phases` lists every value of the `phase` vocabulary once; `after` names only earlier phases;
   `complexity` is a `complexity` value.

## What is fixed and what may change

| Part | Status |
|---|---|
| Ontology file structure: labels, relations, pairs, id formats, vocabularies; rules file keys and the roles, phases, `required` and `story` flags of the shipped rules | Fixed by this spec. A change is a spec change, made here first |
| Descriptions in the ontology file, `phase_guidance`, planner prompt files | A starting point. Developers may rewrite them to improve plans; a change raises the minor `version` of its file (prompts: a raised revision in `PROMPT_REVISIONS`), and is accepted when a plan of the test corpus is no worse on the plan metrics and on a reviewed sample of tasks |

When development starts, both templates are copied into `graph.plan` as package data; from then on the
package files are the shipped files and the templates are not kept in step.

A renamed-ontology test plans the same corpus built with the shipped build ontology and with a copy
whose labels, relations, id prefixes and the attribute values the rules use are all renamed (with
rules adjusted to the new names); the two plans must map onto each other.
