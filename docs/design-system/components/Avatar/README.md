---
type: Design
title: "Avatar component"
description: "A person (initials or photo) or the SpecBot assistant, in a small glass circle."
status: stable
tags: [design-system]
---
A person (initials or photo) or the SpecBot assistant, in a small glass circle.

**Provide:** `name` (initials and tooltip come from it), optional `src`, `size` (`sm` 24 · `md` 32 · `lg` 44), `kind="bot"` for SpecBot (sparkle on yellow glass), `tone` to override the colour picked from the name. **AvatarGroup:** `people` (`{ name, src?, kind? }`), `max` (default 3, then "+n"), `size`.

Tint is derived from the name so a person keeps the same colour everywhere. Always show the name next to a lone avatar in lists.
