---
type: Design
title: "ProcessFlow component"
description: "A swimlane process diagram laid out from data: lanes for who does the work, columns for sequence, and typed steps joined by flow connectors."
status: stable
tags: [design-system]
---
A swimlane process diagram laid out from data: lanes for who does the work, columns for sequence, and typed steps joined by flow connectors.

**Provide:**
- `lanes` — `{ id, label, icon? }`, top to bottom (roles, teams or systems).
- `steps` — `{ id, type, title, owner?, lane, col }`. `type`: `start` (yellow-glass capsule), `step` (numbered glass card), `subprocess` (double rim: a step that has its own diagram), `decision` (glass diamond, a question ending in "?"), `end` (capsule with flag). `owner` is a short second line such as a duration or role.
- `edges` — `{ from, to, label?, fromSide?, toSide?, kind? }`; label decision exits `Yes` / `No`. Defaults to `kind: 'flow'`; use `async` for hand-offs that wait.
- Optional `laneHeight` (132), `colWidth` (176), `numbered={false}`, `selectable={false}`, `label`.

Steps and subprocesses are numbered automatically in the order they appear in `steps`. Clicking a step traces its inputs and outputs. Write step titles as verb-led phrases in sentence case ("Run discovery workshop"), at most four words. Without `lanes` the flow sits on the dotted canvas.
