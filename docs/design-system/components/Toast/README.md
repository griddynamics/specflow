---
type: Design
title: "Toast component"
description: "A short confirmation or error that appears bottom-right and fades after ~4 s."
status: stable
tags: [design-system]
---
A short confirmation or error that appears bottom-right and fades after ~4 s.

**Toast** (presentational): `title`, optional `message`, `tone` (`neutral` · `success` · `info` · `danger`), `action`, `onClose`. **ToastProvider** + **useToast()**: wrap the app once, then call `toast({ title, message, tone, duration })` from anywhere.

Use for results of an action the user just took ("Answers submitted", "Access confirmed", "Not available in this demo"). Errors that block work belong inline, next to the field.
