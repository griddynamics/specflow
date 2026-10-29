---
type: Design
title: "DocLinks component"
description: "Links to the source documents behind a decision or an answer, opening the Markdown version in the Files view."
status: stable
tags: [design-system]
---
Links to the source documents behind a decision or an answer, opening the Markdown version in the Files view.

**Provide:** `links` (`{ title, path, section?, onClick? }`) or a shared `onOpen(link)`.

Show 1–4 links under the decision context, titled by the document's heading, with the path and section underneath.
