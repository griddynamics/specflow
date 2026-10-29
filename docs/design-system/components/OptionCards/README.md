---
type: Design
title: "OptionCards component"
description: "Large single-choice answers: lettered options (A, B, C) with an optional free-text \"Other\" (D), or a Yes/No pair."
status: stable
tags: [design-system]
---
Large single-choice answers: lettered options (A, B, C) with an optional free-text "Other" (D), or a Yes/No pair.

**Provide:** `type` (`choice`|`yesno`), `options` (`{ value, label, description? }`) for choice, `allowOther` (+ `otherLabel`, `otherPlaceholder`, `onOtherChange`), `value`/`onChange` or `defaultValue`, `label` (the question, for assistive tech).

The chosen card gets the yellow-glass rim and halo; the "Other" card opens a text box when picked. Arrow keys move the choice. Use for decisions and questionnaires; use Select or SegmentedControl for filters and settings.
