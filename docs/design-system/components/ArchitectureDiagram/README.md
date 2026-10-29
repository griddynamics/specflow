---
type: Design
title: "ArchitectureDiagram component"
description: "A data-driven architecture diagram: nodes, boundary groups and routed connectors on a dotted glass canvas, with a legend and click-to-trace selection."
status: stable
tags: [design-system]
---
A data-driven architecture diagram: nodes, boundary groups and routed connectors on a dotted glass canvas, with a legend and click-to-trace selection.

**Provide:**
- `nodes` — `{ id, kind, title, subtitle?, icon?, badge?, x, y, w?, h? }` (positions in px; snap to the 24px grid, default size 200×64).
- `groups` — `{ label, icon?, x, y, w, h, tone? }` for boundaries such as a cloud account, VPC, bounded context or team (`tone: 'plain'` for a solid hairline instead of dashed).
- `edges` — `{ from, to, kind?, label?, fromSide?, toSide?, fromOffset?, toOffset?, bend?, bidirectional?, highlight? }`. `kind`: `sync` (solid `edge`), `async` (dashed), `data` (`edge-data` sea blue). Routing is orthogonal with 12px rounded corners and picks sides automatically; override with `fromSide`/`toSide` (`top|right|bottom|left`) and spread parallel lines with offsets.
- `width`, `height`, `label` (accessible description), optional `legendPosition`, `legend={false}`, `selectable={false}`, `defaultSelected`.

Clicking a node highlights every connector touching it (ink line over a yellow `edge-halo`) and dims the rest: a quick "what talks to this?" trace. Lay out left-to-right in request order (actors → entry points → services → data), one column per tier, 56px (`space-xl`) gutters, and keep edge labels to one or two words. Put external systems outside the boundary group. For hand-built layouts compose `DiagramCanvas`, `DiagramGroup`, `DiagramNode` and `GDGlass.routeEdge` directly.
