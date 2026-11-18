# INDEX.md: table of contents of the graph.gap specification

`graph.gap` finds gaps in the spec graph built by `graph.build`: missing information, ambiguities,
contradictions, inconsistencies, stated open questions and unconfirmed assumptions. Detectors find entry
points, an investigating agent turns them into gaps with options and a recommendation, a person or AI
answers, and the answer returns as corpus text through a normal update build, so only the affected part of
the graph and the plan changes. Gaps and answers are nodes in the project's graph store; options,
amendments and run data live in the host's relational store. It reuses `graph.core`; the library-wide rules
of [../INDEX.md](../INDEX.md) apply.

# overview.md: purpose, scope, goals, non-goals, actors, boundaries, constraints, key terms
- [overview.md](overview.md)

# how-it-works.md: explainer: one gap from detection to the re-planned story, noise, re-uploads, coding agents
- [how-it-works.md](how-it-works.md)

# architecture.md: package layout, components, run steps, import rules, write ownership, ports, settings, errors, outside changes
- [architecture.md](architecture.md)

# data-model.md: Gap and Clarification nodes and relations, identity, signatures, statuses, relational store, invariants
- [data-model.md](data-model.md)

# ontology.md: the gap ontology file, the gap rules file, what may change in them
- [ontology.md](ontology.md), [gap-ontology.yaml](gap-ontology.yaml), [gap-rules.yaml](gap-rules.yaml) (copies of the shipped files)

# detection.md: gap kinds, run modes and scope, relevance filter, detector catalogue, seeds, corroboration, sites, limits
- [detection.md](detection.md)

# investigation.md: the Investigator port and adapters, context pack, read menu, report, checks, second check, verdict cache
- [investigation.md](investigation.md)

# answers.md: severity, routing and gates, the question card, clarifications, edits, placement, applying, AI answers, revert
- [answers.md](answers.md)

# reconcile.md: amendments against new source revisions: applied again, re-anchored, adopted, conflict, orphaned; answers moved into the sources
- [reconcile.md](reconcile.md)

# lifecycle.md: gap and clarification transitions, close check, rekeys, follow-ups, dismissals, the plan link, development gaps, migration
- [lifecycle.md](lifecycle.md)

# api.md: public API: setup, gap runs, service calls, ports, host runner, web routes, MCP tools, user interface
- [api.md](api.md)

# requirements.md: requirement units FR-GONT, FR-GOWN, FR-GRUN, FR-GREL, FR-GDET, FR-GINV, FR-GRTE, FR-GANS, FR-GCOR, FR-GREC, FR-GCLS, FR-GPLN, FR-GDEV, FR-GWEB, NFR
- [requirements.md](requirements.md)

# questions.md, later.md: decision record (GD1-GD3 and the decided topics), deferred features
- [questions.md](questions.md), [later.md](later.md)
