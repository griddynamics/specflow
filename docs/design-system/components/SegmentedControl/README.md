---
type: Design
title: "SegmentedControl component"
description: "Switches between 2–5 views of the same content; a glass lens slides to the chosen segment with a slight overshoot."
status: stable
tags: [design-system]
---
Switches between 2–5 views of the same content; a glass lens slides to the chosen segment with a slight overshoot.

**Provide:** `options` (`{value, label}`), `label` (accessible group name), `value`/`onChange` or `defaultValue`.

Lens: `glass-tint` with rim and `shadow-glass`, radius `radius-pill`, inset `space-2xs`. Motion: 320ms `cubic-bezier(.3,1.35,.5,1)`; none under reduced motion. Use for view modes and filters, not for navigation between screens (that is TabBar/NavBar).
