---
type: Design
title: "ChatMessage component"
description: "Messages in the SpecBot conversation: user bubbles on the right, assistant answers on the left with tool steps and source citations."
status: stable
tags: [design-system]
---
Messages in the SpecBot conversation: user bubbles on the right, assistant answers on the left with tool steps and source citations.

**Provide:** `role` (`user`|`assistant`), content as children, optional `name`, `steps` (`{ label, detail?, status: 'running'|'done', icon? }`, shown above the answer as a ToolSteps card), `citations` (`{ label, onClick }`, numbered chips that open the source), `typing`.

Show steps as they happen ("Searching 42 spec files" → "Reading access-control.md"), then the answer, then citations. Keep answers short and quote the exact source line where it matters. Also exported: **ToolSteps**, **Citation**, **TypingIndicator**.
