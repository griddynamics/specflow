---
type: Roadmap
title: "File parsers roadmap"
description: "Deferred features of the file parsers and known gaps"
status: stable
tags: [file-parsers]
---
# roadmap.md: deferred features and known gaps

## Deferred features

| Feature | Why deferred |
|---|---|
| OCR for scanned pages and images | Out of scope (D2); pages without a text layer are logged, so the need can be measured ([pdf.md](pdf.md#pages-without-a-text-layer)). |
| Legacy `.doc`, `.ppt`, `.xls` and OpenDocument | Need LibreOffice (MPL-2.0, AMBER, a much larger image). |
| EPUB and email | Outside the format scope. |
| Faster PDF parsing | An external split of page ranges (serverless steps or Cloud Run) (D1); page ranges already scale with cores ([benchmarks](benchmarks/docling-vs-pymupdf4llm.md#pdf-speed-and-memory)). |
| A second engine and an engine config key | [Engine choice per deployment](#engine-choice-per-deployment) (D7). |
| Re-parse of documents made by pymupdf4llm | They keep their Markdown (D5). |
| Documents for Drive files stored before their type was read | Needs a re-import of the source ([formats.md](formats.md#drive-import)). |

Decisions D1–D8: [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md#owner-decisions).

## Engine choice per deployment

Today there is one engine and no config key. With a second engine, `services.document_parsing` joins the cloud
profile, with the rules of `services.object_storage` (`cloud_profile.py`: an unknown key or value stops the
start-up; the environment variable wins), and one adapter:

```yaml
services:
  document_parsing:
    provider: docling | gcp_document_ai          # default docling on every cloud; local is always docling
    gcp_document_ai: { processor_version: pretrained-layout-parser-v1.0-2024-06-03, location: eu }
```

Rules for that adapter (source research): store the raw provider JSON next to the Markdown; map `heading-1` to
`heading-5` to levels; the engine id carries the processor version, so the graph sees a new engine as a
revision ([architecture.md](architecture.md#graph-revision-rule)).

## Known gaps

- Gate G3, and gates G4 and G5 on the target node ([acceptance-gates.md](acceptance-gates.md#status)).
- A byte cap while the Drive import streams a file, so a very large file is not stored first
  ([formats.md](formats.md#drive-import)).
- An image build in CI: `.github/workflows/validate.yaml` builds no Docker image, so only `make images` finds a
  broken `backend/docker/worker-parse/Dockerfile`.
- Text files that Docling's format sniffing skips ([office-web.md](office-web.md#known-limits)).
- A whitespace rule for the space before commas in table cells ([pdf.md](pdf.md#known-defects)).
