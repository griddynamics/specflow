---
type: Explainer
title: "GD Liquid Glass usage guide"
description: "How coding agents use the GD Liquid Glass tokens, components, React Flow wiring and previews in the frontend"
status: stable
tags: [design-system]
---
# GD Liquid Glass: usage guide for coding agents

This folder is the **GD Liquid Glass** design system: Grid Dynamics' brand (Fira Sans, Mellow Yellow, ink, the prism-cube imagery) expressed as Apple-style Liquid Glass UI. Use it as the single source of truth for every visual decision in the frontend. `README.md` is the brand book (principles, colour, material, type, spacing, motion, diagrams, app patterns); this file explains how to use the code.

## What's here

| Path | What it is | How to use it |
|---|---|---|
| `tokens.json` | All design tokens (colours per light/dark theme, type, spacing, radius, shadow, blur) with usage notes | Source of truth. Never hard-code a colour, font size, radius, shadow or blur that isn't here. |
| `tokens.css` | CSS custom properties generated from `tokens.json` (`--ink`, `--yellow-glass`, `--radius-lg`, …), `@font-face` for Fira Sans, one class per type style (`.title-1`, `.body`, `.eyebrow`, …) | Import once at the app root. Fonts load from `fonts/`, relative to this file. |
| `fonts/` | Fira Sans 300–800 (TTF) | Copy to the app's public/static fonts and keep the `@font-face` paths valid. |
| `components/bundle.css` | All component styles (`gdg-*` classes), the React Flow theme and React Flow 12 base styles | Import after `tokens.css`. If the app imports `@xyflow/react/dist/style.css` itself, delete the marked React Flow base block. |
| `components/bundle.js` | Reference implementation of every component as a browser global (`window.GDGlass`, React 18, no build step) | Read it to see exact markup, class names, states and behaviour. In the app, port components to typed React modules (or load it as-is for a quick prototype). |
| `components/index.d.ts` | Props for every component (`<Name>Props`) | Use these prop names and types for the ported components. |
| `components/index.json` | Machine-readable component list (name, group, summary, README, preview) | Handy for planning and inventories. |
| `components/<Name>/README.md` | Usage rules per component: what to provide, variants, states, do/don't | Read before using a component. |
| `components/<Name>/preview.html` | Example usage fragment (the design-system page preloads tokens, CSS and bundle) | Copy the example data/props. |
| `previews/<Name>.html`, `previews/index.html` | Standalone, offline previews of every component; add `?theme=dark` | Open in a browser or Playwright to compare your implementation visually. |
| `components/lib/` | React 18.3.1, React DOM, `@xyflow/react` 12.11.6 UMD and a tiny `jsx-runtime` shim | Only for the previews. The app installs real npm packages. |
| `assets/Logos/`, `assets/Imagery/` | Full-colour GD logos (horizontal, mark-only; light and dark file) and the prism-cube backdrop (the full banner, cubes on the right) | Copy into the app's static assets. Pass logo paths via `logoSrc` / `logoDarkSrc` / `markSrc`, or set `window.GDGlassAssets` to the asset base URL when using the bundle. |

## Rules that matter most

1. **Tokens only.** Colours, fonts, sizes, radii, shadows and blur come from `tokens.css` variables or type classes.
2. **Glass is for the functional layer** (navigation, controls, overlays, cards). Reading content (rendered Markdown) sits on `ground-alt` (`Prose paper`).
3. **Text on glass:** anything with secondary text, placeholders or small labels uses `glass-fill-strong`; regular `glass-fill` carries only `ink` text.
4. **One yellow moment per view:** the primary action is tinted yellow glass (`Button variant="primary"`); selection is `glass-tint`.
5. **Light theme by default**; dark via `<html data-theme="dark">`. Both are fully specified.
6. **Accessibility:** visible 2px `focus-ring` on everything interactive, modals trap focus and close on Esc, severity/status never by colour alone, `prefers-reduced-motion` and `prefers-reduced-transparency` respected.
7. **Icons:** only the Phosphor Regular glyphs shipped in `Icon` (flagged substitute, MIT). Use `@phosphor-icons/react` (Regular weight) in the app; no emoji, no hand-drawn icons. Vendor logos (for example GitHub and Google Drive) come from each vendor's brand kit.
8. **Voice:** sentence case, short and concrete; UPPERCASE only for buttons and eyebrows.

## Using it in a React app

```tsx
// app root (e.g. main.tsx / layout.tsx)
import 'docs/design-system/tokens.css';            // or copy into src/styles/
import 'docs/design-system/components/bundle.css';
import '@xyflow/react/dist/style.css';          // then remove the React Flow base block from bundle.css
```

Port components as typed React modules that render the same markup and `gdg-*` classes as `bundle.js`, with props from `index.d.ts`. Suggested dependencies for the real app: `@xyflow/react@12`, `elkjs` (auto-layout for diagrams), `@phosphor-icons/react`, a Markdown renderer such as `react-markdown` + `remark-gfm` (render into `<Prose>`), and `zustand` or React context for in-memory demo state.

## Diagrams with React Flow

```tsx
import { ReactFlow, Background, BackgroundVariant, Controls, Panel } from '@xyflow/react';
// nodeTypes: gdNode (architecture box), gdStep (process step), gdGroup (boundary), gdLane (swimlane)
<ReactFlow className="gdg-rf" nodes={nodes} edges={edges.map(e => ({ ...e, ...flowEdge(e.kind, { label: e.label }) }))}
           nodeTypes={flowNodeTypes} fitView>
  <FlowMarkers />                                   {/* GD arrowheads: gd-arrow-base | gd-arrow-data | gd-arrow-hi */}
  <Background variant={BackgroundVariant.Dots} gap={24} size={1.5} />
  <Controls showInteractive={false} position="bottom-right" />
  <Panel position="bottom-left"><DiagramLegend kinds={['sync','async','data']} highlight="Selected path" /></Panel>
</ReactFlow>
```

- Node data: `{ kind, title, subtitle, icon?, badge?, w?, h? }`; kinds: service, app, mobile, gateway, datastore, storage, queue, ai, external, actor, cloud, security, compute, integration.
- Every GD node has source and target handles on all four sides with ids `top | right | bottom | left`.
- Edge kinds: `sync` (solid), `async` (dashed), `data` (sea blue), `flow` (process).
- Click-to-trace: `traceSelection(nodes, edges, selectedId)` returns highlighted edges and dimmed nodes.
- Layout with elkjs (layered, left to right, 56–80px spacing); groups are parent nodes; collapse groups for the overview level.
- See `components/FlowCanvas/README.md`, `components/ArchitectureDiagram/README.md`, `components/ProcessFlow/README.md`.

## Component inventory

| Group | Component | Summary |
|---|---|---|
| Actions | **Button** | Capsule buttons in Fira Sans Bold UPPERCASE, the shape of the Grid Dynamics website CTAs. |
| Chat | **ChatComposer** | The "Ask me" input for SpecBot: a large glass capsule with a yellow-glass send button and optional suggested prompts above it. |
| Chat | **ChatMessage** | Messages in the SpecBot conversation: user bubbles on the right, assistant answers on the left with tool steps and source citations. |
| Content | **CommentThread** | A comment thread anchored to highlighted text, with @mentions for people and @specbot. |
| Content | **DocLinks** | Links to the source documents behind a decision or an answer, opening the Markdown version in the Files view. |
| Content | **Prose** | Styles for rendered Markdown: README views, spec files, linked documents in decisions. |
| Content | **SelectionToolbar** | A floating glass toolbar that appears above selected text in a document: **Edit**, **Comment**, **Ask SpecBot**. |
| Data | **Avatar** | A person (initials or photo) or the SpecBot assistant, in a small glass circle. |
| Data | **ConnectorCard** | A marketplace tile for a connector: vendor logo, name, category, one-line description, status and action. |
| Data | **DecisionList** | The decisions inbox: a glass table of decisions with domain, severity and assignee; each row opens the decision. |
| Data | **FileRow** | One file in an upload list: type icon, name, size, and its state. |
| Data | **StatCard** | A big-number tile: Fira Sans ExtraBold numeral (`stat` style), a half-size unit, and a muted label, with a short yellow bar above. |
| Diagrams | **ArchitectureDiagram** | A data-driven architecture diagram: nodes, boundary groups and routed connectors on a dotted glass canvas, with a legend and click-to-trace selection. |
| Diagrams | **DiagramNode** | One box on an architecture diagram: a glass card with a tinted icon tile, an UPPERCASE kind label, a title and a technology subtitle. |
| Diagrams | **FlowCanvas** | React Flow (`@xyflow/react` 12) wired to the GD diagram look: GD node types, connector styles, markers, legend, dotted canvas, styled controls and minimap, and click-to-trace. |
| Diagrams | **ProcessFlow** | A swimlane process diagram laid out from data: lanes for who does the work, columns for sequence, and typed steps joined by flow connectors. |
| Foundations | **Icon** | Small UI glyphs drawn from Phosphor Icons (Regular weight, MIT licence): a flagged substitute, since the GD brand has no interface icon set of its own. |
| Inputs | **Dropzone** | A dashed glass area for adding files by drag-and-drop or the system file picker. |
| Inputs | **LinkField** | A URL input with a **Test access** button and a live status line, for connecting repositories, drives and other sources. |
| Inputs | **OptionCards** | Large single-choice answers: lettered options (A, B, C) with an optional free-text "Other" (D), or a Yes/No pair. |
| Inputs | **PeoplePicker** | Adds people to something (project contributors, decision assignees) by typing a name, email or role and picking from suggestions; picks become removable chips. |
| Inputs | **QuestionBlock** | One question inside a decision: numbered heading, optional help, an answer input and an optional comment. |
| Inputs | **SearchField** | A capsule search input with a magnifier and a clear button. |
| Inputs | **SegmentedControl** | Switches between 2–5 views of the same content; a glass lens slides to the chosen segment with a slight overshoot. |
| Inputs | **Select** | A dropdown for picking one value, usually a filter (Domain, Owner, Status). |
| Inputs | **Switch** | An on/off toggle: a glass thumb on a clear track that fills `yellow` when on; the thumb stretches while pressed. |
| Inputs | **TextField** | A labelled single-line input on `glass-fill-strong` with an inset shadow, so typed text always meets contrast over imagery. |
| Layout | **Card** | A glass container with an optional eyebrow, title, body and footer row. |
| Layout | **EmptyState** | A centred glass card that explains why a screen is empty and offers the next step. |
| Layout | **GlassSurface** | The Liquid Glass material: a translucent, blurred, saturated layer with a light-catching rim and a top-left specular sheen; every GD Liquid Glass component is made of it. |
| Navigation | **Breadcrumbs** | The path to the current file or page, with each earlier step clickable. |
| Navigation | **FileTree** | A folder tree of project files with expand/collapse, file-type icons and a selected file. |
| Navigation | **NavBar** | A floating glass capsule for top-level navigation: GD logo left, links centre, one primary action right. |
| Navigation | **Sidebar** | App-level navigation in a floating glass panel, expanded with icons and labels or collapsed to an icon rail. |
| Navigation | **TabBar** | A floating glass capsule of 2–5 section tabs for mobile and compact app layouts; the selected tab sits in a yellow-tinted glass lens. |
| Navigation | **TopBar** | The app's top-right control cluster: project switcher, New project button and the user menu, in one floating glass capsule. |
| Overlays | **Menu** | A dropdown of actions or choices anchored to a trigger, on thick glass. |
| Overlays | **Modal** | A centred dialog of thick glass for focused tasks: creating a project, answering a decision, confirming something irreversible. |
| Overlays | **Sheet** | A modal panel of thick glass rising from the bottom (mobile) or centred (desktop) over a `scrim`, for focused tasks and confirmations. |
| Overlays | **Tooltip** | A short glass label that appears on hover and keyboard focus. |
| Status | **ProgressBar** | A thin progress track with a yellow fill, plus `Spinner` for short unknown waits. |
| Status | **SeverityBadge** | Severity of a decision or risk: Critical, High, Medium, Low, shown as a four-step bar glyph plus the word. |
| Status | **Skeleton** | Placeholder shapes that shimmer while content loads. |
| Status | **Tag** | Pill labels for categories and statuses: the only shape the GD deck system has always rounded. |
| Status | **Toast** | A short confirmation or error that appears bottom-right and fades after ~4 s. |

## Previews

Open `previews/index.html`. Pages work from disk; for Playwright checks serve the folder (`npx serve docs/design-system`) and visit `/previews/<Name>.html?theme=dark`.

## Changing the system

Edit `tokens.json` first, then regenerate `tokens.css` (one `--<name>` per token, light under `:root`, dark under `[data-theme="dark"]`, aliases like `{ink}` become `var(--ink)`). Keep component changes in sync between `bundle.js`, `bundle.css`, `index.d.ts` and the component README. The hosted, editable version lives at https://claude.ai/artifact/8jepbM3baJCx5nLcdBgTF9.
