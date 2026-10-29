---
type: Design
title: "StatCard component"
description: "A big-number tile: Fira Sans ExtraBold numeral (`stat` style), a half-size unit, and a muted label, with a short yellow bar above."
status: stable
tags: [design-system]
---
A big-number tile: Fira Sans ExtraBold numeral (`stat` style), a half-size unit, and a muted label, with a short yellow bar above.

**Provide:** `value` (the digits), optional `unit` (`%`, `+`, `wk`, `×`), `label` (what it counts, 2–5 words).

Concrete numbers only, never filler stats. Lay several out in a row with `space-md` gutters; use `variant="strong"` over imagery.
