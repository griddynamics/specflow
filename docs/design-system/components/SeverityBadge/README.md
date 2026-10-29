---
type: Design
title: "SeverityBadge component"
description: "Severity of a decision or risk: Critical, High, Medium, Low, shown as a four-step bar glyph plus the word."
status: stable
tags: [design-system]
---
Severity of a decision or risk: Critical, High, Medium, Low, shown as a four-step bar glyph plus the word.

**Provide:** `level` (`critical`|`high`|`medium`|`low`), optional `label`.

Critical is the only solid fill (`severity-critical`); High uses a coral tint with danger text, Medium yellow tint, Low neutral. The number of filled bars repeats the level, so it never relies on colour alone.
