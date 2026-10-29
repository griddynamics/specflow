---
type: Design
title: "PeoplePicker component"
description: "Adds people to something (project contributors, decision assignees) by typing a name, email or role and picking from suggestions; picks become removable chips."
status: stable
tags: [design-system]
---
Adds people to something (project contributors, decision assignees) by typing a name, email or role and picking from suggestions; picks become removable chips.

**Provide:** `people` (`{ id, name, email?, role? }`), `value`/`onChange` (array of ids) or `defaultValue`, `label`, optional `help` and `placeholder`.

Arrow keys move through suggestions, Enter adds, Backspace on an empty input removes the last chip. Suggestions show avatar, name, role and email. Out of scope for now: inviting someone who is not in the list.
