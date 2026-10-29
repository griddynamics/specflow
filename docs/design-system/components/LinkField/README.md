---
type: Design
title: "LinkField component"
description: "A URL input with a **Test access** button and a live status line, for connecting repositories, drives and other sources."
status: stable
tags: [design-system]
---
A URL input with a **Test access** button and a live status line, for connecting repositories, drives and other sources.

**Provide:** `label`, `placeholder` (an example URL), optional `icon`, `defaultValue`, `onTest(url) → Promise<boolean>` (defaults to `GDGlass.simulateAccess`: ~1.4 s, then success for any http(s) URL that does not contain "private"), `onStatus`, `onRemove` (for repeatable rows), `okText`/`failText`.

States: idle (help text), checking (pending tag with spinner), connected (success tag), no access (danger tag + red border, with a fix-it hint). Editing the URL resets the status. Stack several rows with an "Add another" plain button for multiple repos.
