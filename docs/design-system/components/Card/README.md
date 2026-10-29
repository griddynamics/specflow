---
type: Design
title: "Card component"
description: "A glass container with an optional eyebrow, title, body and footer row."
status: stable
tags: [design-system]
---
A glass container with an optional eyebrow, title, body and footer row.

**Provide:** `title` (sentence case, no period), body as children, optional `eyebrow` (UPPERCASE kicker) and `footer` (Tags or small Buttons).

Padding is `space-md`, radius `radius-lg`; anything rounded inside it uses `radius-sm` (concentric rule: 24 − 14 ≈ 10). Use `variant="strong"` when the card sits over the prism imagery and holds more than two lines of text. **Don't** add coloured left borders, icons-as-decoration or emoji; use the GD PNG illustration set at 56px if a card needs a picture.
