---
type: Design
title: "Docling engine"
description: "Packages, configuration, models, forbidden features and upgrade rules of the Docling engine"
status: stable
tags: [file-parsers]
---
# docling.md: packages, configuration, models, forbidden features and upgrades of the Docling engine

The Docling engine implements the engine port ([architecture.md](../architecture.md#engine-port)) for every
accepted format, in `file_parsers/engines/docling.py` (PDF) and `docling_office.py` (Office and web).

## Packages

- `docling-slim[convert-core,format-pdf,models-local,format-docx,format-pptx,format-xlsx,format-html,format-markdown]==2.145.0`,
  plus `opencv-python-headless<5`, `pypdfium2`, `defusedxml` and `huggingface-hub`, in the `parse` extra of
  `backend/pyproject.toml`; only the worker-parse image installs it ([runtime.md](../runtime.md#images)).
- `opencv-python-headless` is explicit because TableFormer v1 imports `cv2` without declaring it. With it, the
  minimal install gives the same output as the full `docling` package
  ([benchmarks/docling-vs-pymupdf4llm.md](../benchmarks/docling-vs-pymupdf4llm.md#findings)).
- PyTorch comes from the PyTorch CPU index on Linux: the CUDA wheels of the default index carry proprietary
  terms.
- Licences: Docling's code is MIT; the layout and TableFormer weights are Apache-2.0 and CDLA-Permissive-2.0
  (checked on Hugging Face); PyTorch is BSD-3-Clause with a bundled `libgomp`; OpenCV is Apache-2.0 with a
  bundled FFmpeg (LGPL); the format packages (python-docx, python-pptx, openpyxl, beautifulsoup4, marko and
  their dependencies) are MIT or BSD. `NOTICE` names each component, and the licence gate G6 keeps every
  image's dependency set on an allowlist ([acceptance-gates.md](../acceptance-gates.md)).

## Configuration

| Option | Value | Why |
|---|---|---|
| PDF backend | docling-parse (the default) | The pypdfium2 backend breaks tables ([benchmarks](../benchmarks/docling-vs-pymupdf4llm.md#pdf-quality-on-the-fixture)). |
| Tables (PDF) | TableFormer v1, `do_cell_matching=True`, mode FAST | Decision D4 ([decisions/docling-replaces-pymupdf.md](../decisions/docling-replaces-pymupdf.md#owner-decisions)); TableFormer V2 empties cells. |
| OCR | off, and no OCR package in the image | Out of scope (decision D2, [roadmap.md](../roadmap.md#deferred-features)). |
| Picture images | off; on only for the command line `--enrich` path | Stored Markdown keeps placeholders only ([pdf.md](../pdf.md#structure-pass)). |
| Heading hierarchy | off | SpecFlow ranks PDF headings itself; Docling's `HeadingHierarchyOptions` orders them wrongly. |
| Markdown export | `escape_underscores=False`, `escape_html=False`, `compact_tables=True`; per page for PDF, PPTX, XLSX | `_`, `<` and `>` stay raw; without `compact_tables` Docling pads table cells and the Markdown grows by about 30 %. |
| Allowed formats | exactly the stored format of the file, one converter per format | [formats.md](../formats.md#one-format-per-conversion) |
| HTML and Markdown | `enable_remote_fetch=False`, `enable_local_fetch=False`, `fetch_images=False` | [limits.md](../limits.md#safety-rules) |
| Threads | `num_threads` from the CPU quota | [runtime.md](../runtime.md#threads) |
| Unit of work (PDF) | page ranges ([pdf.md](../pdf.md#pipeline)) | Memory ([runtime.md](../runtime.md#sizing)) |

## Models

- The layout model (`docling-project/docling-layout-heron`) and the TableFormer FAST model
  (`docling-project/docling-models`) are baked into the worker-parse image at pinned revisions, read from
  `artifacts_path` (`DOCLING_ARTIFACTS_PATH`), with `HF_HUB_OFFLINE=1`.
- `backend/scripts/fetch_docling_models.py` is the one place that names the repositories, revisions and files.
  The Dockerfile's revision arguments are pinned equal to it by a test; CI caches the models by the script's
  hash; `make models` fetches them into `backend/.docling-models` for host tests.

## Forbidden features

VLM pipelines, picture description, chart, code and formula enrichment, OCR, speech recognition, remote
services and external plugins stay off: licence, determinism and scope. A unit test checks the built options.

## Upgrades

Docling and the model revisions are pinned exactly. An upgrade changes the engine id
([architecture.md](../architecture.md#engine-id)) and must pass gates G1, G2 and G11 again
([acceptance-gates.md](../acceptance-gates.md)).
