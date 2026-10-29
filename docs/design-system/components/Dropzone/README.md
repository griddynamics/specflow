---
type: Design
title: "Dropzone component"
description: "A dashed glass area for adding files by drag-and-drop or the system file picker."
status: stable
tags: [design-system]
---
A dashed glass area for adding files by drag-and-drop or the system file picker.

**Provide:** `onFiles(File[])`, optional `title`, `hint` (accepted types and size), `buttonLabel`, `accept`, `multiple`, `primary` (yellow-glass button, for an empty project's main action), `compact` (one-row version inside forms and questions).

Dragging over turns it yellow-tinted with a solid border. It only hands back the File objects; uploading, progress and errors are shown with FileRow.
