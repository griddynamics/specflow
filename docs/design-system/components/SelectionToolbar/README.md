---
type: Design
title: "SelectionToolbar component"
description: "A floating glass toolbar that appears above selected text in a document: **Edit**, **Comment**, **Ask SpecBot**."
status: stable
tags: [design-system]
---
A floating glass toolbar that appears above selected text in a document: **Edit**, **Comment**, **Ask SpecBot**.

**Provide:** `x`, `y` (position inside a `position: relative` container, usually from `useTextSelection(containerRef)`, which returns `[selection, clear]` with `text`, `range`, `x`, `y`), `onAction(id)`, optional `actions` (`{ id, label, icon, accent? }`).

On **Comment**, wrap the range in `<mark class="gdg-mark">` and open a CommentThread; on **Edit**, make the block `contenteditable`; on **Ask SpecBot**, prefill the chat with the quote. The toolbar keeps the selection alive (it prevents mousedown blur).
