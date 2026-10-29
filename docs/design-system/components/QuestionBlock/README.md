---
type: Design
title: "QuestionBlock component"
description: "One question inside a decision: numbered heading, optional help, an answer input and an optional comment."
status: stable
tags: [design-system]
---
One question inside a decision: numbered heading, optional help, an answer input and an optional comment.

**Provide:** `number` ("Q1"), `question`, optional `help`, `type` (`choice` with `options` + `allowOther` · `yesno` · `upload`), `defaultValue`/`onChange`, `onFiles` for upload, `defaultCommentOpen`.

A decision Modal holds the context (`.gdg-context`), DocLinks to the source documents, then one QuestionBlock per question, separated by hairlines. `upload` is a compact Dropzone that only simulates attaching a file.
