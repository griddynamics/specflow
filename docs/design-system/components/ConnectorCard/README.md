---
type: Design
title: "ConnectorCard component"
description: "A marketplace tile for a connector: vendor logo, name, category, one-line description, status and action."
status: stable
tags: [design-system]
---
A marketplace tile for a connector: vendor logo, name, category, one-line description, status and action.

**Provide:** `name`, `category`, `description`, `status` (`connected` · `attention` · `off` · `checking`), `logoSrc` (the vendor's official logo), optional `meta` ("3 repositories"), `actionLabel`, `onAction`.

Without `logoSrc` the card shows a monogram placeholder; replace it with the vendor's official mark from their brand kit, never a redrawn one. Lay cards out in an auto-fill grid (min 260px) under a SearchField and a category SegmentedControl. "Not connected" logos go greyscale.
