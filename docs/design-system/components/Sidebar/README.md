---
type: Design
title: "Sidebar component"
description: "App-level navigation in a floating glass panel, expanded with icons and labels or collapsed to an icon rail."
status: stable
tags: [design-system]
---
App-level navigation in a floating glass panel, expanded with icons and labels or collapsed to an icon rail.

**Provide:** `items` — `{ value, label, icon, badge? }` entries, plus `{ section: 'Label' }` entries to start a group; `defaultValue`/`value` + `onChange` for the active item; optional `user` (`{ name, role }`), `defaultCollapsed`/`collapsed` + `onToggle`.

- **Expanded** (264px): the full-colour GD horizontal logo of the theme (`logoSrc` light, `logoDarkSrc` dark; both are rendered as `.gdg-logo-light` / `.gdg-logo-dark` and the theme shows one), UPPERCASE section labels (`eyebrow`), 20px icon + 15px label rows, count badges in solid `yellow`.
- **Collapsed** (76px): the full-colour GD mark (`markSrc`, the same in both themes), icons only, section labels become hairline rules, labels appear as glass tooltips on hover and focus, badges shrink onto the icon corner. Every icon button keeps its label as `aria-label`.
- The active item gets the `glass-tint` lozenge, the same selection language as NavBar, TabBar and SegmentedControl.

Float it `space-sm` from the window edges with `radius-xl` so content passes behind it. Use 5–9 items; more belongs in a secondary panel. Collapse automatically below 1024px. Icons come from the Icon component.
