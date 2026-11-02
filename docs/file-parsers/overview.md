---
type: Overview
title: "File parsers overview"
description: "Purpose, scope, entry points and key terms of the file parsers"
status: stable
tags: [file-parsers]
---
# overview.md: purpose, scope, entry points and key terms of the file parsers

## Purpose

SpecFlow builds its knowledge graph from Markdown. The file parsers turn each uploaded or imported file into
that Markdown, once, in the `parse_document` activity of the `worker-parse` service, and store it with the
id of the engine that made it (`documents.parser`). Docling is the engine; the decision and its evidence are
in [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md).

## Scope

| | |
|---|---|
| Parsed | PDF, Word (DOCX), PowerPoint (PPTX), Excel (XLSX), HTML, Markdown and CSV ([formats.md](formats.md)). |
| Not parsed here | Google Docs, Slides and Sheets: the Drive import converts them itself (`drive-convert`). Legacy `.doc/.ppt/.xls`, OpenDocument, EPUB, email, scanned pages and images: see [roadmap.md](roadmap.md). |
| Kept beside the parser | The `file_parsers` package also holds the LLM enrichers of the PDF command line (figures, Mermaid, math) and the transcript parser ([architecture.md](architecture.md#kept-parts-of-the-package)). |

## Entry points

- Upload: the API accepts the formats of [formats.md](formats.md), stores the original and starts a parse.
- Google Drive: the Drive import stores each file and starts a parse for the formats it reads
  ([formats.md](formats.md#drive-import)).
- Frontend: the Local files picker sends only accepted types ([formats.md](formats.md#frontend)).
- Command line: `python -m file_parsers.pdf.parser` parses one PDF, with `--enrich` for the LLM enrichers
  ([pdf.md](pdf.md#llm-enrichment-on-the-command-line)).

## Key terms

| Term | Meaning |
|---|---|
| Engine | An implementation of the `DocumentEngine` port ([architecture.md](architecture.md#engine-port)). |
| Engine id | The value stored in `documents.parser` ([architecture.md](architecture.md#engine-id)). |
| Page range | The unit of work of a PDF conversion ([pdf.md](pdf.md#pipeline)). |
| Structure pass | SpecFlow's deterministic repair of the engine's PDF Markdown ([pdf.md](pdf.md#structure-pass)). |
| Guard | The checks an Office or web file passes before conversion ([office-web.md](office-web.md#guard)). |
| FAST / ACCURATE | The two TableFormer table modes of Docling ([engines/docling.md](engines/docling.md#configuration)). |
| Golden file | `backend/tests/data/specs/extraction.md`, the expected output of the PDF fixture (status `REVIEW_REQUIRED` in its header). |
| Source research | The owner-provided report "SpecFlow Source-to-Markdown Parsing: Engine Shortlist by Source Type" (2026-10-31). |
| RED / AMBER / GREEN | Licence classes of the licence audit; "licence step 2" is its alternatives step. |

## Reading order

1. [architecture.md](architecture.md) for the shape of the parse path.
2. [formats.md](formats.md), [limits.md](limits.md) and [errors.md](errors.md) for what goes in and what
   comes out.
3. [pdf.md](pdf.md) or [office-web.md](office-web.md) for one pipeline; [engines/docling.md](engines/docling.md)
   for the engine settings.
4. [runtime.md](runtime.md) before you size or deploy `worker-parse`.
5. [acceptance-gates.md](acceptance-gates.md) before you change the parser.
