---
type: Design
title: "ProgressBar component"
description: "A thin progress track with a yellow fill, plus `Spinner` for short unknown waits."
status: stable
tags: [design-system]
---
A thin progress track with a yellow fill, plus `Spinner` for short unknown waits.

**ProgressBar:** `value` (0–100), optional `label` (shows the percentage too), `indeterminate`, `tone="danger"` for a failed run. **Spinner:** `size` (default 18), `label`.

Use a bar when progress is measurable (uploads, processing steps) and a spinner for waits under ~3 s (testing access, sending). Both slow down under reduced motion.
