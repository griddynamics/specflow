---
type: Design
title: "FileRow component"
description: "One file in an upload list: type icon, name, size, and its state."
status: stable
tags: [design-system]
---
One file in an upload list: type icon, name, size, and its state.

**Provide:** `name`, `size` (bytes or text), `status` (`ready` · `uploading` + `progress` 0–100 · `processing` · `done` · `error`), `onRemove` (shows ✕ while ready or failed), optional `icon`, `doneText`, `errorText`.

Stack rows with `space-xs` gaps under a Dropzone. While files upload, hide the Upload and Create buttons and let the progress bars carry the screen; when all are done, move on automatically.
