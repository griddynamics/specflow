---
type: Design
title: "Sheet component"
description: "A modal panel of thick glass rising from the bottom (mobile) or centred (desktop) over a `scrim`, for focused tasks and confirmations."
status: stable
tags: [design-system]
---
A modal panel of thick glass rising from the bottom (mobile) or centred (desktop) over a `scrim`, for focused tasks and confirmations.

**Provide:** `title`, body as children, `actions` (one primary Button right-most, a glass Button to cancel), `onClose` (scrim click and Esc), `open`.

Material `glass-fill-strong` + `blur-lg` + `shadow-glass-float`, radius `radius-xl`. Move focus into the sheet on open and back on close.
