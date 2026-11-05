# INDEX.md: table of contents of the graph.plan specification

`graph.plan` turns the spec graph built by `graph.build` (ontology v2.0) into an implementation plan for
coding agents: work areas, stories and an acyclic graph of tasks, stored in the project's graph store (Neo4j,
ArcadeDB or PostgreSQL) next to the spec nodes they implement, kept current from the build change reports, with task progress recorded by git commit.
Agents use it over MCP tools the host exposes. It reuses `graph.core`; the library-wide rules of
[../INDEX.md](../INDEX.md) apply.

# overview.md: purpose, scope, goals, non-goals, actors (MCP agents), boundaries, constraints, key terms
- [overview.md](overview.md)

# how-it-works.md: explainer: why a plan on the graph, a first plan step by step, agents over MCP, spec changes
- [how-it-works.md](how-it-works.md)

# architecture.md: package layout, components, import rules, write ownership, ports, settings, errors
- [architecture.md](architecture.md)

# data-model.md: plan nodes (areas, stories, tasks) and relationships, identity, task status, story progress, build ids, constraints, invariants
- [data-model.md](data-model.md)

# ontology.md: the plan ontology file, the plan rules file, what may change in them
- [ontology.md](ontology.md), [plan-ontology.yaml](plan-ontology.yaml), [plan-rules.yaml](plan-rules.yaml) (copies of the shipped files)

# plan.md: modes, change reports, plan run steps, partition and stories, planners, stitch, review, diff, reuse, failures, result, options
- [plan.md](plan.md)

# lifecycle.md: task transitions, ready tasks, claims for MCP agents, task briefs, agent contract, plan checks
- [lifecycle.md](lifecycle.md)

# api.md: public API: setup, plan runs and estimate, change reports, reads (stories, story work, plan DAG), task lifecycle, ports
- [api.md](api.md)

# requirements.md: requirement units FR-PONT, FR-POWN, FR-PRUN, FR-PCHG, FR-PPRT, FR-PSTY, FR-PPLN, FR-PDAG, FR-PDIF, FR-PLCY, FR-PMCP, FR-PCHK, NFR
- [requirements.md](requirements.md)

# questions.md, later.md: decision record (Q1-Q21, PT1-PT23 after build ontology v2.0), deferred features
- [questions.md](questions.md), [later.md](later.md)
