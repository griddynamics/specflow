---
type: Roadmap
title: "Deferred graph gap features"
description: "Features of graph.gap deferred beyond the first slices"
status: draft
tags: [graph, gap]
---
# later.md: graph.gap features deferred beyond the first slices

## Delivery slices

| Slice | Content |
|---|---|
| 0 | Plan prerequisite: the host passes build change reports to the plan run ([lifecycle.md](lifecycle.md) "Plan prerequisite") |
| 1 | Detect: gap ontology and rules, relational tables, relevance filter, slice-1 detectors, sites, runner, `Gap` nodes, noise report; gaps without options; read-only inbox |
| 2 | Investigate: the `Investigator` port with the structured loop, checks, options with edits and a recommendation, routing |
| 3 | Answer: clarifications, amendments, clarifications document, reconcile, apply build, close check, download with answers |
| 4 | Advanced inference: second check, AI answers, review list, revert, valve |
| 5 | Development: coding-agent findings as development gaps, site-mode investigation, blocker holds, release on resolve, migration of first-slice findings |

## Deferred

| Feature | Why later |
|---|---|
| Slice-2 detectors (MS-CONSUMER, MS-ERROR, MS-LIFECYCLE, CT-CROSS, CT-RULE, IN-TERM, IN-PAYLOAD) | Ship after the precision of the slice-1 detectors is measured on the test corpus |
| Agent hypotheses (broken flows across modules, protected data leaving its guard) | Need the tool-using investigator adapter and a re-checked path as witness |
| Plan review issues as seeds (a requirement no task can build, logic before its data) | Needs a stable read of the plan's review results |
| Learning noise rules from dismissals | Needs enough dismissals per project to be safe |
| Writing answers back to Drive or uploaded originals | Decided against for now (GD1); would need write access and an edit review flow |
| Roles for answering (who may answer which severity or area) and an approval workflow (a second person confirms a major answer) | No roles in this release: membership is the only access rule today |
| Notifications (mail or chat) for new blocker gaps | Host feature outside the library |
| Gap metrics over time (open gaps per area, time to answer) | Needs a reporting surface |
