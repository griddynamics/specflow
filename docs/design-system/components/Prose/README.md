---
type: Design
title: "Prose component"
description: "Styles for rendered Markdown: README views, spec files, linked documents in decisions."
status: stable
tags: [design-system]
---
Styles for rendered Markdown: README views, spec files, linked documents in decisions.

**Provide:** rendered HTML as `html` (from your Markdown renderer) or children; `paper` puts it on the flat `ground-alt` page (reading surfaces are not glass); `innerRef` for selection handling.

Styled elements: h1–h4, paragraphs, lists (yellow markers), links (ink with yellow underline), inline and block code, tables, blockquotes (yellow rule), rules, images and `<mark>` for commented text. Line length is capped at 72 characters. Pair with `useTextSelection` + SelectionToolbar for review.
