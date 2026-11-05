---
type: Roadmap
title: "Deferred graph plan features"
description: "Features of graph.plan deferred beyond the first release"
status: stable
tags: [graph, plan]
---
# later.md: graph.plan features deferred beyond the first release

Out of scope, not deferred: an executor. Users connect their own coding agents to the app over MCP, and
the MCP server (host code) exposes the plan's reads and lifecycle calls as tools ([questions.md](questions.md)
Q1, PT23). The cost estimate (`estimate_plan`) is in scope ([api.md](api.md), PT17).

| Feature | What it adds |
|---|---|
| Host endpoint and runtime | `plan_run` endpoint and in-process runner like `graph_build`, the MCP tools over the plan API, and a host plan runner that reads build change reports and settlement; later a Temporal workflow with Postgres-backed `PlanStore` (Q15, PT23) |
| Findings hold tasks back | With `graph.gap`: a task that implements a spec node with an open finding or open question is not ready; `block_task` can name a finding; a clarification unblocks or triggers a plan run (PT13) |
| Release filter | A plan option `releases` that plans only the given releases; later releases stay unplanned until added (PT12 E) |
| Release ordering | Tasks of a later release wait for the same area's earlier-release tasks; needs a release order the text does not give (PT12 F) |
| Confidence floor for placement | A minimum edge confidence (for example 0.8) for inferred edges used in placement (PT8 D) |
| Interface-based cascade | Supersede dependants in other areas when the interfaces they use changed (Q4) |
| Review revisions | An area with a blocking review issue is planned once more with the issues (Q19) |
| Capability areas | Areas for capabilities realised by several modules, with their contracts built first (Q20) |
| Cross-cutting coverage | Module areas plan their share of system-wide constraints instead of one project-area task (Q8) |
| Deployment-aware integration | A service area's integration tasks wait for the deployment area it is `DEPLOYED_TO`, once agents deploy for tests (PT5 C) |
| Cleanup tasks | Tasks that remove or adapt code whose requirements were removed (Q14) |
| Workspace-aware project planner | The project planner reads an existing repository (read-only) to fit the profile and setup tasks to it |
| Plan export to files | Markdown export of the plan in dependency order for humans; `plan_dag` already returns JSON and Mermaid |
| Agent graph tool | A read-only tool a coding agent can call to walk the spec graph beyond its brief |
| Critical path | Ready tasks ordered by the length of the work that waits on them |

Rejected, not deferred: forced claims that ignore prerequisites (PT22 C). A human who wants to override
a prerequisite completes or retires it explicitly.
