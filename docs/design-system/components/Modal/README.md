---
type: Design
title: "Modal component"
description: "A centred dialog of thick glass for focused tasks: creating a project, answering a decision, confirming something irreversible."
status: stable
tags: [design-system]
---
A centred dialog of thick glass for focused tasks: creating a project, answering a decision, confirming something irreversible.

**Provide:** `title`, optional `eyebrow` and `description`, body as children, `footer` (actions, primary right-most), `onClose`, `open`, `size` (`sm` 440 · `md` 640 · `lg` 880).

The body scrolls; header and footer stay put. Esc, the close button and the scrim all call `onClose`; focus moves into the dialog and is trapped there, and returns to the trigger on close. Group long forms with `.gdg-section-title` headings. For a quick confirmation use `size="sm"`; for one-line choices on mobile use Sheet.
