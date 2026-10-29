---
type: Design
title: "Icon component"
description: "Small UI glyphs drawn from Phosphor Icons (Regular weight, MIT licence): a flagged substitute, since the GD brand has no interface icon set of its own."
status: stable
tags: [design-system]
---
Small UI glyphs drawn from Phosphor Icons (Regular weight, MIT licence): a flagged substitute, since the GD brand has no interface icon set of its own.

**Provide:** `name` (one of `GDGlass.Icon.names`), optional `size` (default 20; use 16, 20 or 24) and `label` when the icon stands alone with meaning (otherwise it is hidden from assistive tech).

Icons inherit `currentColor`, so they take `ink` or `ink-2` from their parent; never colour them yellow on light grounds. Use the GD PNG illustration set (Presentation Design System) for feature cards and empty states; use these only inside controls, navigation and diagram nodes. To add an icon, copy its Regular path from Phosphor into the bundle; don't draw your own.
