---
type: Questions
title: "Open questions of the graph library"
description: "Open questions of the graph library specification"
status: draft
tags: [graph]
---
# questions.md: open questions of the graph library specification

| # | Question | Default until answered |
|---|---|---|
| Q1 | How do `graph.gap` and `graph.plan` read build results (spec changes, metrics, inference lists): through the host, a core port, or the graph? | The host passes the build result; decided with `graph.gap` |
| Q3 | Should a build report a "rebuild recommended" signal when update builds have drifted far from a fresh extraction? | No signal; rebuilds are manual |
