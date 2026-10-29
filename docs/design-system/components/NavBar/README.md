---
type: Design
title: "NavBar component"
description: "A floating glass capsule for top-level navigation: GD logo left, links centre, one primary action right."
status: stable
tags: [design-system]
---
A floating glass capsule for top-level navigation: GD logo left, links centre, one primary action right.

**Provide:** `links` (`{label, href, current}`, 4–6 items, sentence case as on the website: Consulting, Engineering, Platforms…), `action` (usually `Button variant="primary" size="sm"`).

Float it `space-sm` below the top edge with `space-md` side margins so content scrolls *under* the glass; that pass-through is what sells the material. The current page gets the `glass-tint` lozenge. The logo is the full-colour horizontal logo; the light and dark files (`logoSrc`, `logoDarkSrc`) are both rendered as `.gdg-logo-light` / `.gdg-logo-dark` and the theme shows one. On narrow screens collapse links into a TabBar or a menu Sheet.
