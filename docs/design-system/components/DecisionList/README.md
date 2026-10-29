---
type: Design
title: "DecisionList component"
description: "The decisions inbox: a glass table of decisions with domain, severity and assignee; each row opens the decision."
status: stable
tags: [design-system]
---
The decisions inbox: a glass table of decisions with domain, severity and assignee; each row opens the decision.

**Provide:** `DecisionRow` children (`title`, `subtitle?`, `domain`, `severity`, `assignee?`, `status?: 'answered'`, `onClick`), optional `columns`, `label`, `empty` (shown when filters leave nothing), `footer` (e.g. "12 of 28 decisions").

Put the filter row above it in a `.gdg-filters` container: Switches for "Open" and "Assigned to me", a Select for Domain, a SegmentedControl for Severity (All · Critical · High · Medium · Low). Rows are buttons, so the whole row is the click target and is keyboard reachable.
