---
type: Design
title: "Switch component"
description: "An on/off toggle: a glass thumb on a clear track that fills `yellow` when on; the thumb stretches while pressed."
status: stable
tags: [design-system]
---
An on/off toggle: a glass thumb on a clear track that fills `yellow` when on; the thumb stretches while pressed.

**Provide:** `label` (states the setting, not the action: "Email digests"), `checked`/`onChange` or `defaultChecked`.

Off track border is `ink-3` so the control stays visible (≥3:1) on every ground. Use only for settings that apply immediately; anything that needs a Save uses a checkbox in a form.
