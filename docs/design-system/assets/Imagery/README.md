---
type: Design
title: "GD Liquid Glass imagery"
description: "The prism-cube backdrop artwork that glass refracts, its source, placement and limits"
status: stable
tags: [design-system]
---
`gd-prism-cubes.webp` is the translucent prism-cube artwork that glass refracts (`.gdg-scene`). Its yellow, coral and sky-blue faces are the brand palette seen through glass. Since D21 it is a **byte copy** of `ai-engineering-grid-dynamics-banner.webp` (the Grid Dynamics website AI-engineering banner, 2560×720, 96,232 B). The two files are kept under both names on purpose: the name `gd-prism-cubes.webp` is the backdrop the CSS and the app point at, the banner name records where it comes from (git stores the duplicate blob once).

- Placement: `bundle.css` shows it with `background-size: cover; background-position: 50% 50%`, the recipe the Grid Dynamics website uses for its hero. The cube cluster spans 53.0 to 89.3 % of the banner width and touches its top and bottom rows; the ground on its left is flat (`rgb(223,238,244)`). With cover and a centred position the cluster's left edge lands at `W/2 + 76.8·s` px (`s = max(W/2560, H/720)`, W and H = the scene's size), so the cubes always start in the right half (about 56 to 60 % of the width on landscape panels) and bleed off the right edge.
- Limit, portrait: the cluster is about 1.3 times as wide as a panel is high, so on a portrait panel (tablet, phone) the cubes are a band of tips at the right edge, or practically absent on a phone. The app targets desktop (SPEC.md: 1280 to 1920 px). The Grid Dynamics website serves separate tablet (1536×1080) and mobile (1440×1792) banners; add them here behind a portrait rule when they are needed.
- Regenerate: copy the banner over `gd-prism-cubes.webp`, then copy the result to `frontend/styles/ds/assets/Imagery/` (no re-encoding, so no generation loss).
- Limit: the source is 720 px high, so scenes taller than 720 CSS px and 2x screens scale it up. Replace the source with a taller original (at least 1440 px high) when one is available.
