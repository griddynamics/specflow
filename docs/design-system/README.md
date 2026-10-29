---
type: Design
title: "GD Liquid Glass brand book"
description: "Principles, colour, material, type, spacing, motion, diagrams and app patterns of the GD Liquid Glass design system"
status: stable
tags: [design-system]
---
GD Liquid Glass is the product-UI layer of the Grid Dynamics brand: Apple-style Liquid Glass (translucent, blurred, light-catching layers floating over content) dressed in GD's own colour, type and voice. Use it for apps, dashboards and web products. Decks and documents keep following the GD Presentation Design System, which stays flat, white and rectilinear.

## Principles

1. **Content first, glass on top.** Content sits on `ground` (or `ground-alt` for long reading). Glass is reserved for the functional layer that floats above it: navigation (NavBar, TabBar), controls (SegmentedControl, Switch, Button `glass`) and overlays (Sheet, menus). Never make a long table or an article glass.
2. **One yellow moment per view.** Tinted `yellow-glass` marks the primary action; `glass-tint` marks the current selection. Everything else is ink, glass and the brand ground.
3. **Glass needs something to refract.** Put screens on `.gdg-scene` (the GD prism-cube artwork) or `.gdg-wash` (soft yellow, coral and sky-blue light). Glass on flat white looks like a grey box.
4. **Rectilinear content, capsule controls.** Data, images and documents keep GD's square corners (`radius-0`). Controls are capsules (`radius-pill`), like the website CTAs. Glass containers use `radius-lg`/`radius-xl` and nest concentrically.

## Content fundamentals

Voice is the senior engineer in the room: practical, specific, a little understated. Use "we" for Grid Dynamics and address the user as "you" in product copy.

- Sentence case for titles, labels and navigation ("Delivery dashboard", "Active engagements"), no trailing period.
- UPPERCASE only for Button labels (style `button`, tracked 0.04em) and eyebrows (style `eyebrow`, tracked 0.18em): `EXPLORE OUR CAPABILITIES`, `CASE STUDY`.
- Short sentences, one idea each. Concrete numbers beat adjectives: "+18% conversion uplift", "12wk pilot to production", never "best-in-class" or "next-gen".
- Write the wordmark as `Grid Dynamics`. No emoji, ever.
- Real copy from the brand: "AI engineered for your enterprise." "Bring an AI use-case — leave with a delivery plan." "Accelerators, not slideware."

## Colour

- `ground` (#E2EDF3, the Grid Dynamics website hero ground) is the page. `ground-alt` (#F7F7F7) is for flat reading areas.
- `ink` for all text; `ink-2` for secondary; `ink-3` for placeholders and metadata.
- Primary buttons are tinted, semi-transparent yellow glass: `yellow-glass` (hover `yellow-glass-hover`, press `yellow-glass-pressed`) with a warm `yellow-glass-rim` and `on-yellow-glass` text. This intentionally departs from the solid yellow CTA on the website.
- Solid `yellow` (#FFC002) is for the switch on-state, stat bars, accent tags and the cover, with `on-yellow` text. Hover `yellow-hover`, press `yellow-pressed`. Yellow is never a text colour on light grounds. The website renders its CTA slightly warmer (about #F4BB40); the brand-book value #FFC002 is the token.
- `coral`, `sea-blue`, `sky-blue`, `grass` are the GD secondaries: data visualisation and the prism washes only. For text, use the status tokens `info`, `success`, `danger`, always with a word.
- Dark theme: `ground` becomes near-black blue-grey, glass becomes smoked; yellow stays the same and keeps `on-yellow` (ink) text.

## Material: Liquid Glass

| Layer | Token(s) | Use | Text allowed |
|---|---|---|---|
| Regular | `glass-fill`, `blur-md`, `saturate-glass`, `shadow-glass` | nav, tab bars, cards, controls | `ink` any size; `ink-2` at 24px+ |
| Strong | `glass-fill-strong`, `blur-lg`, `shadow-glass-float` | sheets, dialogs, menus, forms, text-heavy panels over imagery | every text token |
| Subtle | `glass-fill-subtle` | tracks and chips inside another glass layer | never alone under text |
| Tint | `glass-tint` | the yellow selection lens | `ink` |
| Yellow glass | `yellow-glass`, `yellow-glass-rim`, `blur-md`, `shadow-yellow` | primary buttons | `on-yellow-glass` |

Every glass shape gets a 1px `glass-rim`, a top-left specular sheen in `glass-sheen`, and a top inner highlight (inside `shadow-glass`). The optional `glass-rib` texture (fine vertical ribbing taken from the brand artwork) is for one hero surface per screen. Stack at most two glass layers. When the user asks for reduced transparency, glass falls back to opaque `ground-alt` (handled in `bundle.css`).

## Type

Fira Sans throughout (Light to ExtraBold ship as files). Headlines are ExtraBold with tight tracking, as on the website hero.

- `display-hero` 72/0.95 for marketing heroes; `display` 48 for screen heroes; `stat` 64 for big numerals.
- `title-1` 32 page titles, `title-2` 24 section and sheet titles, `title-3` 20 card titles, `headline` 17 row titles.
- `body-lg` 18/28 lead paragraphs, `body` 16/24 default, `body-sm` 14/20 dense UI, `caption` 12/16 metadata.
- `button`, `eyebrow`, `control` for labels; set `text-transform: uppercase` on `button` and `eyebrow`.
- `quote` in Georgia italic for pull-quotes only.

## Spacing, radius, elevation

- Spacing keeps the GD deck scale: `space-xs` 8, `space-sm` 14, `space-md` 24, `space-lg` 36, `space-xl` 56, `space-2xl` 80, plus `space-2xs` 4 for dense controls. Card padding `space-md`; screen margins `space-xl` desktop, `space-md` mobile.
- Radius: `radius-0` content, `radius-sm` fields and inner elements, `radius-md` menus, `radius-lg` cards and NavBar, `radius-xl` sheets, `radius-pill` all controls. Inner radius = outer radius − padding.
- Elevation is light, not darkness: `shadow-glass` at rest, `shadow-glass-float` for overlays, `shadow-yellow` (specular edge and warm glow) under yellow-glass primary buttons.

## Motion and states

Glass moves like a liquid: lenses and thumbs travel on `cubic-bezier(.3, 1.35, .5, 1)` over 280–320ms with a slight overshoot; presses scale to 0.97 in 160ms; switch thumbs stretch while pressed. Everything stops under `prefers-reduced-motion`. Focus is a solid 2px `focus-ring` with 2px offset on every interactive element (≥3.3:1 on ground and glass in both themes). Disabled controls drop to 45% opacity.

## Imagery and logo

- `assets/Imagery/gd-prism-cubes.webp` (2560×720, a byte copy of the full AI-engineering banner of the Grid Dynamics website `ai-engineering-grid-dynamics-banner.webp`) is the default backdrop for glass (`.gdg-scene`). It is shown with `cover` at `50% 50%`, so the cubes sit on the right of every landscape panel and bleed off its right edge, as on the Grid Dynamics website. In dark theme it is dimmed with a 62% `ground` overlay.
- Logos are full-colour SVGs, one folder per theme (`assets/Logos/Full color, black/SVG` for the light theme, `Full color, white/SVG` for the dark theme). Use the plain `-horizontal` logo in the NavBar, Sidebar and sign-in, and `-mark` (the mark alone) for the collapsed Sidebar and the favicon. Render both theme files and let the DS rule `.gdg-logo-light` / `.gdg-logo-dark` show one; never invert or recolour them. The four single-ink PNGs in `assets/Logos` stay as legacy assets.

## Iconography

GD's own icons are the 19 two-colour PNG illustrations in the GD Presentation Design System: use them at 56px for feature cards and empty states, never recoloured. For small UI glyphs the brand has no set, so the system ships 64 Phosphor Icons (Regular weight, MIT licence) in the Icon component as a flagged substitute: navigation, diagram kinds and process markers. Add more by copying Phosphor Regular paths, never by drawing new ones. The only drawn glyph is the Button `arrow`, mirroring the website CTA. No emoji or unicode symbols as icons.

## Navigation

Three levels, one selection language (the `glass-tint` lozenge): **Sidebar** for app sections (expanded 264px with icons and text, or a 76px icon rail with glass tooltips), **NavBar** for marketing and simple sites, **TabBar** for mobile. Use one primary navigation per layout.

## Diagrams

Architecture and process diagrams are first-class in this system and share one engine.

- **Canvas:** `canvas` ground with a `canvas-grid` dot every 24px (`space-md`); snap everything to it. Diagrams stay flat and calm; nodes are thick glass (`glass-fill-strong`) so any label passes contrast.
- **Nodes** (DiagramNode): kind label, title and technology subtitle beside a tinted icon tile: `node-tint-service` (neutral), `node-tint-data` (sky blue), `node-tint-queue` (coral), `node-tint-ai` (yellow glass). External systems are dashed and clear; actors are capsules. Colour always repeats what the icon and kind label say.
- **Groups:** dashed `group-border` boundaries on `group-fill` with a label notch: cloud accounts, VPCs, bounded contexts, teams.
- **Connectors:** orthogonal, 12px rounded corners, small filled arrowheads. `edge` solid = synchronous call / flow, dashed = async or event, `edge-data` sea blue = data flow. Labels are one or two words on glass pills. Every diagram with more than one connector kind shows a DiagramLegend.
- **Selection:** clicking a node traces its connectors (ink line over a yellow `edge-halo`) and dims everything else to 45%: the one yellow moment on a diagram.
- **Process flows** (ProcessFlow): swimlanes for who, columns for when; yellow-glass start capsule, numbered steps, double-rimmed subprocesses, glass decision diamonds with Yes/No exits, a flag for the end.
- **Layout:** read left to right; one column per tier (actors → entry points → services → data) or per stage; 56–80px gutters so connector labels have room; at most ~15 nodes per view, split larger systems into linked diagrams.

## App patterns

The demo platform (Mission Control, Decisions, SpecBot, Connections) is assembled from these pieces:

- **Shell:** Sidebar (collapsed by default) + TopBar (project switcher, yellow-glass **New**, avatar menu with Reset demo). Content sits on `ground`, with the brand imagery only on empty and hero states.
- **Overlays:** Menu for lists of actions, Select for filters, Modal for tasks (create project, answer a decision), Sheet for quick mobile choices, Toast for results, Tooltip for icon buttons.
- **Forms:** TextField (also `multiline`), PeoplePicker, LinkField with Test access, OptionCards (A/B/C + Other, Yes/No), Dropzone, Switch. Forms live on `glass-fill-strong` (a Modal, a strong Card).
- **Upload flow:** EmptyState → Dropzone → FileRow list with ProgressBar → processing → views.
- **Documents:** FileTree + Breadcrumbs + Prose (`paper`), with `useTextSelection` → SelectionToolbar → CommentThread (@people, @specbot). Highlighted text uses `highlight-mark`.
- **Decisions:** filter row (`.gdg-filters`: Switches, Select, SegmentedControl) above DecisionList; rows open a Modal with `.gdg-context`, DocLinks and QuestionBlocks. Severity uses SeverityBadge: bars + word, never colour alone.
- **Assistant:** ChatComposer (centred on imagery when empty, pinned to the bottom later), ChatMessage with ToolSteps and Citations, TypingIndicator. SpecBot is always the sparkle avatar on yellow glass.
- **Marketplace:** SearchField + SegmentedControl categories above a grid of ConnectorCards with status Tags (`success`, `warning`, `neutral`, `pending`). Use vendors' official logos.
- **Diagrams in the app:** React Flow with `flowNodeTypes`, `flowEdge(kind)`, `FlowMarkers`, `traceSelection` and the `.gdg-rf` theme; lay out with elkjs. ArchitectureDiagram and ProcessFlow remain the dependency-free versions for static pages.

Rule of thumb for text on glass: anything with secondary text, placeholders or small labels uses `glass-fill-strong`; regular glass carries only `ink` text.

## Using it

Load `tokens.css`, `components/bundle.css`, React 18 and `components/bundle.js`; components are on `window.GDGlass` (`GlassSurface`, `Button`, `Card`, `Sidebar`, `NavBar`, `TabBar`, `SegmentedControl`, `Switch`, `TextField`, `Tag`, `StatCard`, `Sheet`, `Icon`, `DiagramCanvas`, `DiagramNode`, `DiagramGroup`, `DiagramLegend`, `ArchitectureDiagram`, `ProcessFlow`, `ProcessNode`, and the `routeEdge` helper), plus the app components listed under App patterns. Previews also load `@xyflow/react` 12.11.6 (UMD, `window.ReactFlow`) after a small `jsx-runtime` shim; in an app, import `@xyflow/react` and its `style.css` normally. Wrap the app in `.gdg-root` and a backdrop class (`.gdg-scene` or `.gdg-wash`). Without React, the same look comes from the CSS classes alone (`gdg-glass`, `gdg-glass--strong`, `gdg-btn gdg-btn--primary`, …).
