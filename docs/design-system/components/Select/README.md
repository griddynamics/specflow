---
type: Design
title: "Select component"
description: "A dropdown for picking one value, usually a filter (Domain, Owner, Status)."
status: stable
tags: [design-system]
---
A dropdown for picking one value, usually a filter (Domain, Owner, Status).

**Provide:** `options` (`{ value, label, description? }`), `value`/`onChange` or `defaultValue`, optional visible `label`, `icon`, `placeholder`.

Include an "All …" option first for filters. It shares the Menu popover and the glass pill look of SegmentedControl, so a filter row reads as one family. Use SegmentedControl instead when there are 2–5 short options that should stay visible.
