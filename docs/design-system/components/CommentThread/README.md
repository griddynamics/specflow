---
type: Design
title: "CommentThread component"
description: "A comment thread anchored to highlighted text, with @mentions for people and @specbot."
status: stable
tags: [design-system]
---
A comment thread anchored to highlighted text, with @mentions for people and @specbot.

**Provide:** `quote` (the highlighted text), `comments` (`{ author, time, text, kind?: 'bot', typing? }`), `people` for mentions (`{ name, handle?, role?, kind? }`: include `{ name: 'SpecBot', handle: 'specbot', kind: 'bot' }`), `onSubmit(text)`, optional `onResolve`, `resolved`.

Typing `@` opens suggestions above the box (arrows + Enter or Tab to insert); ⌘/Ctrl+Enter sends. Mentions render as blue chips. SpecBot replies sit on yellow glass so they read as the assistant's; show `typing: true` while it "thinks". Use **MentionInput** on its own wherever else mentions are needed.
