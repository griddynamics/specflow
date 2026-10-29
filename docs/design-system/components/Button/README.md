---
type: Design
title: "Button component"
description: "Capsule buttons in Fira Sans Bold UPPERCASE, the shape of the Grid Dynamics website CTAs."
status: stable
tags: [design-system]
---
Capsule buttons in Fira Sans Bold UPPERCASE, the shape of the Grid Dynamics website CTAs.

**Provide:** a short verb-led label (2–4 words, e.g. `LET'S TALK`, `EXPLORE OUR CAPABILITIES`), `onClick`; optional `arrow` for forward-moving CTAs.

- `primary`: tinted, semi-transparent yellow glass: `yellow-glass` fill over a `blur-md` backdrop blur, `yellow-glass-rim` edge, top-left sheen, `on-yellow-glass` text, `shadow-yellow` glow. ONE per view. This deliberately differs from the solid yellow CTA on the Grid Dynamics website so the button belongs to the glass layer; with reduced transparency it falls back to solid `yellow`.
- `glass`: secondary actions on glass; `glass-fill` with rim.
- `plain`: tertiary/inline; `ink` text with a 2px `yellow` underline.
- Sizes `sm` (inside cards, toolbars), `md` (default), `lg` (heroes).

States: primary hover deepens the tint to `yellow-glass-hover` and brightens the rim (`yellow-glass-rim-hover`; in dark theme the rim alone carries it), glass hover goes to `glass-fill-strong`; press scales to 0.97 (`yellow-glass-pressed`); focus shows the 2px `focus-ring`; disabled is 45% opacity. **Don't** set button text in yellow, or put two primary buttons side by side.
