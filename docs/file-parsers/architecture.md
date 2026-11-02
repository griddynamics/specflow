---
type: Architecture
title: "File parsers architecture"
description: "The parse path, the engine port, engine ids, modules and the graph revision rule"
status: stable
tags: [file-parsers]
---
# architecture.md: the parse path, the engine port, engine ids, modules and the graph revision rule

## The parse path

```
parse_document activity (worker-parse): original in storage -> documents.markdown + documents.parser
 └─ file_to_markdown(path, media_type) -> ParsedMarkdown(markdown, parser=<engine id>)
     ├─ PDF:         page count and cap -> engine.extract(pdf, page ranges) -> PDFDocument
     │               -> PdfStructure (structure pass, pypdfium2) -> PDFRenderer -> "<!-- source-page: N -->" Markdown
     └─ Office/web:  guard -> engine.extract_office(file) -> OfficeContent
                     -> OfficeStructure (heading shift, markers) -> Markdown -> Markdown size cap
```

- The activity, its limits and its progress heartbeat: [runtime.md](runtime.md).
- The two pipelines: [pdf.md](pdf.md) and [office-web.md](office-web.md).
- `PDFDocument` is the PDF intermediate model, the input of `PDFRenderer` and of the LLM enrichers, so stored
  PDF Markdown keeps one shape whatever the engine. Office and web formats skip it: they have no pages to
  render and no enrichers.

## Engine port

`DocumentEngine` (`file_parsers/engines/port.py`) is the only thing a pipeline knows about an engine:

| Member | Meaning |
|---|---|
| `engine_id` | The value stored in `documents.parser` ([Engine id](#engine-id)). |
| `formats` | The canonical media types the engine accepts ([formats.md](formats.md#accepted-formats)). |
| `extract(path, media_type, page_range=None, *, images=False, assets_dir=None, on_progress=None)` | One PDF page range (or the whole file) as `PDFDocument`. `images=True` writes figure PNGs into `assets_dir` (the command line only). |
| `extract_office(path, media_type, *, on_progress=None)` | An Office or web file as `OfficeContent`: one section, or one per slide or sheet. Nothing is fetched and no image is written. |

An engine returns per-page content only: it does not rank headings, repair words or render. One engine
ships:

| Engine | Formats | Document |
|---|---|---|
| Docling | all accepted formats | [engines/docling.md](engines/docling.md) |

## Engine id

`<engine>@<engine version>+sf<post-processing version>`, for example `docling@2.145.0+sf1`, for every format.
`POST_PROCESSING_VERSION` (`file_parsers/engines/docling.py`) counts changes of SpecFlow's own repairs; bump
it when a released change alters the Markdown of a file that parsed before, so that a re-parse is visible as a
new engine id ([Graph revision rule](#graph-revision-rule)).

## Modules

| Module | Role |
|---|---|
| `file_parsers/formats.py` | The one table of accepted formats: media type, extension, aliases, legacy types. Standard library only, so the API can import it. |
| `file_parsers/convert.py` | `file_to_markdown`, the facade the activity calls; picks the pipeline by media type. |
| `file_parsers/limits.py` | The limit values and the two error types of the parse ([limits.md](limits.md), [errors.md](errors.md)). |
| `file_parsers/engines/` | The port, the Docling engine (`docling.py`, `docling_office.py`), the default engine and the CPU count (`cpu.py`). |
| `file_parsers/pdf/` | `PDFParser`, the structure pass (`structure.py`, `word_repair.py`), the pypdfium2 wrapper (`pdfium_typed.py`), `PDFRenderer`, the schema and the LLM enrichers. |
| `file_parsers/office/` | The guard (`guard.py`, `kind.py`), the content model and `OfficeStructure`. |
| `file_parsers/transcript/` | The transcript parser (not part of the document parse). |

Import rule: `file_parsers.convert` imports Docling at module import, so only `worker-parse` code imports it.
The API imports `file_parsers.formats` only; the api and worker-agents images carry no Docling
([runtime.md](runtime.md#images)).

## Kept parts of the package

- `PDFParser`, `PDFRenderer`, the schema, the prompts and the three LLM enrichers (figures, Mermaid, math)
  stay. The enrichers render pages and clips with pypdfium2 and take table boxes from the engine
  ([pdf.md](pdf.md#llm-enrichment-on-the-command-line)).
- The transcript parser is not touched by the document parse.

## Graph revision rule

`revision_id = content_hash` when `parser` is NULL, `pymupdf4llm` or `drive-convert`; otherwise
`revision_id = content_hash + ":" + parser` (`api/graph_build/corpus.py`).

- A document parsed again with a new engine id is revised once, and its graph document is rebuilt
  (`graph/build/changes.py`). Rows made before Docling keep their revision, so a deploy does not rebuild every
  project graph at once.
- New Office and web documents start new lineages.
- The rule is decision D10 of [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md#owner-decisions).
