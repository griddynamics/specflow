---
type: Design
title: "FileTree component"
description: "A folder tree of project files with expand/collapse, file-type icons and a selected file."
status: stable
tags: [design-system]
---
A folder tree of project files with expand/collapse, file-type icons and a selected file.

**Provide:** `nodes` (`{ id, name, type: 'folder'|'file', children?, badge? }`), `defaultExpanded` (folder ids), `selected`/`onSelect` or `defaultSelected`, optional `title` and `actions` (e.g. an "Add files" button) for the header.

Badges count open comments on a file. Put it in a 280–320px glass column beside the document (Prose).
