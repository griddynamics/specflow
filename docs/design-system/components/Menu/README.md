---
type: Design
title: "Menu component"
description: "A dropdown of actions or choices anchored to a trigger, on thick glass."
status: stable
tags: [design-system]
---
A dropdown of actions or choices anchored to a trigger, on thick glass.

**Provide:** `trigger` (content for the default pill trigger, or a render function `({ open, toggle })` for a custom one), `items` — `{ label, icon?, avatar?, description?, checked?, tone?: 'danger', shortcut?, onSelect }`, `{ section }` or `{ divider: true }` — optional `header`, `align` (`start`|`end`), `label`.

`checked` turns items into a single-choice list (project switcher, filters). Opens below the trigger, closes on choice, outside click or Esc; arrow keys move between items. Keep it under ~10 items; longer lists become a Select with search or a Modal.
