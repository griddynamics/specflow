---
type: Design
title: "FlowCanvas component"
description: "React Flow (`@xyflow/react` 12) wired to the GD diagram look: GD node types, connector styles, markers, legend, dotted canvas, styled controls and minimap, and click-to-trace."
status: stable
tags: [design-system]
---
React Flow (`@xyflow/react` 12) wired to the GD diagram look: GD node types, connector styles, markers, legend, dotted canvas, styled controls and minimap, and click-to-trace.

**Building blocks** (use them in your own `<ReactFlow>`):
- `nodeTypes={GDGlass.flowNodeTypes}`: `gdNode` (DiagramNode; `data`: `kind`, `title`, `subtitle`, `icon`, `badge`, `w`, `h`), `gdStep` (ProcessFlow step; `data.type`: start · step · subprocess · decision · end), `gdGroup` (boundary; `data`: `label`, `icon`, `count`, `collapsed`, `onToggle`; size via node `style`), `gdLane` (swimlane band).
- Every GD node has source and target handles on all four sides with ids `top|right|bottom|left`; set `sourceHandle`/`targetHandle` or let your layout choose.
- `GDGlass.flowEdge(kind, extra)` returns edge props: `smoothstep` with 12px corners, GD classes and the GD arrow marker for `sync` · `async` · `data` · `flow`. Render `<GDGlass.FlowMarkers/>` once inside `<ReactFlow>` so the markers exist.
- `GDGlass.traceSelection(nodes, edges, id)` returns `[nodes, edges]` with the selected node's connectors highlighted and other nodes dimmed.
- Add `className="gdg-rf"` to `<ReactFlow>`; the theme maps React Flow's CSS variables to GD tokens.

**FlowCanvas** wraps all of this for read-only diagrams: `nodes`, `edges` (each with `kind`), `height`, `minimap`, `legend`, `legendKinds`, `controls`, `draggable`. Needs `window.ReactFlow` (or import from `@xyflow/react`) and a `ReactFlowProvider` above it. Lay out with elkjs (layered, left to right, 56–80px spacing) before passing positions; use parent groups for boundaries and collapse them for the overview level.
