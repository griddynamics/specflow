---
type: Design
title: "ChatComposer component"
description: "The \"Ask me\" input for SpecBot: a large glass capsule with a yellow-glass send button and optional suggested prompts above it."
status: stable
tags: [design-system]
---
The "Ask me" input for SpecBot: a large glass capsule with a yellow-glass send button and optional suggested prompts above it.

**Provide:** `onSend(text)`, optional `placeholder` (default "Ask me"), `suggestions` (strings) + `onSuggestion`, `value`/`onChange`, `disabled` while a reply streams.

Enter sends, Shift+Enter adds a line; the box grows with its content. On an empty conversation, centre it on the brand imagery with a greeting above and 2–3 suggestions; once messages exist, pin it to the bottom.
