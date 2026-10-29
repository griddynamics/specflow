---
type: Design
title: "DiagramNode component"
description: "One box on an architecture diagram: a glass card with a tinted icon tile, an UPPERCASE kind label, a title and a technology subtitle."
status: stable
tags: [design-system]
---
One box on an architecture diagram: a glass card with a tinted icon tile, an UPPERCASE kind label, a title and a technology subtitle.

**Provide:** `kind`, `title`, optional `subtitle` (the technology, e.g. "PostgreSQL 15"), `icon` (overrides the kind's default), `kindLabel`, `badge` (a short count or version), and, when placed by hand inside a `DiagramCanvas`, `x`, `y`, `w`, `h`.

| kind | default icon | tile |
|---|---|---|
| `service`, `app`, `mobile`, `gateway`, `compute`, `integration`, `security`, `cloud` | cube, browser, device-mobile, arrows-split, cpu, plug, shield-check, cloud | `node-tint-service` |
| `datastore`, `storage` | database, hard-drives | `node-tint-data` (sky blue) |
| `queue` | queue | `node-tint-queue` (coral) |
| `ai` | sparkle | `node-tint-ai` (yellow glass) |
| `external` | globe | dashed `group-border`, clear glass |
| `actor` | user | capsule shape |

Colour only reinforces the kind: the icon and kind label always say it too. Default size 200×64; keep titles to 2–3 words and let `subtitle` carry the technology. Selected nodes get the `yellow-glass-rim` plus an `edge-halo` ring; unrelated nodes dim to 45%.
