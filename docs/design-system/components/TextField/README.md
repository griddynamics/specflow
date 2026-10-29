---
type: Design
title: "TextField component"
description: "A labelled single-line input on `glass-fill-strong` with an inset shadow, so typed text always meets contrast over imagery."
status: stable
tags: [design-system]
---
A labelled single-line input on `glass-fill-strong` with an inset shadow, so typed text always meets contrast over imagery.

**Provide:** `label` (always visible, sentence case), optional `placeholder` (an example, in `ink-3`), `help`, or `error` (replaces help, turns border and message `danger`), plus any native input props. Set `multiline` (and `rows`, default 3) for a resizable text area: comments, descriptions, "Other" answers.

Radius `radius-sm` because fields usually sit inside a 24px-radius card. Group fields on a `Card variant="strong"` or Sheet, never on regular glass.
