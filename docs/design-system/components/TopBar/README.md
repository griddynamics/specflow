---
type: Design
title: "TopBar component"
description: "The app's top-right control cluster: project switcher, New project button and the user menu, in one floating glass capsule."
status: stable
tags: [design-system]
---
The app's top-right control cluster: project switcher, New project button and the user menu, in one floating glass capsule.

**Provide:** `projects` (`{ id, name, meta? }`), `projectId` + `onProjectChange`, `onNew`, `user` (`{ name, email }`), optional `userItems` (Menu items; defaults to Settings, Reset demo, Sign out) and `children` for the left side (page title or Breadcrumbs).

Order left to right: project switcher (folder icon + name + caret), a hairline, the yellow-glass **New** button, the avatar. The project menu lists every project with a check on the current one; the user menu opens with the person's name and email. Place it in the content column (not over the Sidebar), `space-sm` from the top.
