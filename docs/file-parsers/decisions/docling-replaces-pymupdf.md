---
type: Design
title: "Docling replaces pymupdf4llm"
description: "Decision record: Docling becomes the document parser for PDF and parses Office and web formats"
status: stable
tags: [file-parsers]
---
# docling-replaces-pymupdf.md: Docling replaces pymupdf4llm and parses Office and web formats

## Status

Approved by the owner on 2026-11-01, with the answers to N1–N4 below and the table mode changed to FAST (D4). Implemented; gate status in [acceptance-gates.md](../acceptance-gates.md#status).

## Context

### Licence

PyMuPDF and pymupdf4llm are AGPL-3.0, class RED in the licence audit. Licence step 2 had already chosen
Docling as the target; an Artifex licence is quote-only with proprietary terms, and there is no AGPL
exception.

### Before this change

- The parse called `pdf_file_to_markdown(path)`, which ran `pymupdf4llm.to_markdown` per page and rendered
  `<!-- source-page: N -->` pages with figure placeholders; `documents.parser` held `pymupdf4llm` or
  `drive-convert`, and `revision_id` was the content hash.
- Production did not OCR: pymupdf4llm OCRs only when RapidOCR or Tesseract is installed, and neither was.
  Figure links pointed at a temporary directory that was deleted after the parse; only the placeholders
  mattered.
- PyMuPDF ran in the production extractor, in a PNG size helper and in the math enricher (page and clip
  renders, `find_tables`), through `pymupdf_typed.py` and a `pymupdf4llm.pyi` stub.
- worker-parse ran with 2 GB of memory; the parse activity had 45 min start-to-close and 3 attempts, the
  workflow 60 min, the Drive wait 60 min (activity 65 min). There was no page cap and no upload size limit.
- Uploads, storage keys, batches, removal, the Drive import and the frontend assumed PDF: the content type and
  `%PDF-` check, five places that rebuilt the key `original.pdf` instead of reading `original_key`, batch
  items hardcoded as PDF, a Drive import path for PDFs only, removal and cleanup of fixed key variants, and
  Drive classes that stored Office, HTML, Markdown and CSV files without reading them.

## Decision

- Replace pymupdf4llm with Docling behind the `DocumentEngine` port in the kept `file_parsers` package
  ([architecture.md](../architecture.md)), configured as in [engines/docling.md](../engines/docling.md).
- Keep PDF quality at least equal to pymupdf4llm with SpecFlow's structure pass ([pdf.md](../pdf.md)); the
  evidence is [benchmarks/docling-vs-pymupdf4llm.md](../benchmarks/docling-vs-pymupdf4llm.md).
- Parse DOCX, PPTX, XLSX, HTML, Markdown and CSV with the same engine, through the upload API, the Drive
  import and the frontend ([formats.md](../formats.md), [office-web.md](../office-web.md)), within new limits
  ([limits.md](../limits.md)).
- Accept the slower PDF parse for now: page ranges in the one activity, a page cap and longer time limits
  ([runtime.md](../runtime.md)); faster parsing comes later from an external split.
- Revise only documents parsed again with a new engine id ([architecture.md](../architecture.md#graph-revision-rule)).

## Owner decisions

| # | Decision | Owner answer |
|---|---|---|
| D1 | Long-document strategy | Speed is not a goal now; a later external split (serverless steps or Cloud Run) will parallelise. Now: sequential 10-page ranges in the one activity. |
| D2 | OCR for pages without a text layer | Out of scope ([roadmap.md](../roadmap.md#deferred-features)). |
| D3 | Page cap | A cap, new ([limits.md](../limits.md#parse-limits)). |
| D4 | Table mode | FAST. ACCURATE first; changed after the page-range measurement showed the same quality in less time. |
| D5 | Existing PDF documents | (a) Keep their pymupdf4llm Markdown. |
| D6 | Regression test | (a) Structural thresholds; `extraction.md` stays as it is, because LLM steps will be added over time to raise quality. |
| D7 | Engine config key | (a) Later, with a second engine ([roadmap.md](../roadmap.md#engine-choice-per-deployment)). |
| D8 | Images | Separate images: each container has its own SDKs and packages and does not reuse the api image ([runtime.md](../runtime.md#images)). |
| D9 | Speed PDF | The Chromium rendering of the spec is accepted. |
| D10 | Graph revision rule | (a) `content_hash:parser` only for new engine ids. |
| D11 | Queue wait and timeouts | (b) No new workflows or queues; extend the time limits. |
| D12 | Delivery | One PR, one commit per implementation session; step 1 of [Cutover and rollback](#cutover-and-rollback) folds into the first session. The implementation plan has no human gates and is not committed. |
| S1 | Formats | PDF, DOCX, PPTX, XLSX, HTML, Markdown, CSV. |
| S2 | Entry points | Upload and Drive files. |
| S3 | Google Docs, Slides, Sheets | Keep `drive-convert`. |
| S4 | Frontend | Changes in the same PR. |

## Decided questions

| # | Question | Options | Decided |
|---|---|---|---|
| N1 | Drive: how HTML, Markdown and CSV files become processed | (a) processing class by MIME type inside the existing categories; (b) a new category "Web and text files" | (a): no migration. (b) needs a migration of the `ITEM_CATEGORIES` CHECK constraint, the Drive contract fixture and the frontend enums; the OOXML and legacy split needs a class by MIME type anyway ([formats.md](../formats.md#drive-import)). |
| N2 | Limits (none existed) | (a) the proposed values; (b) other values; (c) no limits | (a) ([limits.md](../limits.md)). |
| N3 | Error code for a failed non-PDF parse | (a) new `document_parse_failed`, `pdf_parse_failed` stays for PDF; (b) reuse `pdf_parse_failed`; (c) rename to one `parse_failed` | (a): existing rows and clients keep their values ([errors.md](../errors.md)). |
| N4 | Time limits | (a) the proposed values; (b) other values | (a): one maximum-size parse plus a short queue, and the retry rule holds ([runtime.md](../runtime.md#time-limits)). |

## Rejected alternatives

| Alternative | Why it lost |
|---|---|
| Keep pymupdf4llm and buy the Artifex licence | Quote-only, proprietary terms; no AGPL exception. |
| Unstructured OSS (`unstructured[pdf]`, measured) | Slower than Docling FAST and weaker on tables; `fast` finds no tables and returns no elements, without an error, on the fixture (cause not diagnosed) ([benchmarks](../benchmarks/docling-vs-pymupdf4llm.md)). Licence: `hi_res` needs the poppler binary (GPL) and Tesseract, pikepdf is MPL-2.0, and the layout weights have no licence metadata. The hosted API is external SaaS. |
| Docling's `HeadingHierarchyOptions` | Wrong order on the fixture ("3.1" above "1 Introduction"). |
| Docling's pypdfium2 backend | Fixes glued words but breaks table cell matching. |
| Docling TableFormer V2 | Empty cells on the fixture; dropping OpenCV with V2 is not ready. |
| Docling VLM pipelines (granite-docling, picture description, chart, code and formula enrichment) | GPU-oriented, slower on CPU, not deterministic; some presets carry RED weight licences, such as Chandra and OpenRAIL (source research). |
| Fan-out of page ranges or an own parse queue | D1 and D11: no new workflows or queues now. |
| markitdown for Office and web | A second library with weaker structure mapping (source research); Docling covers the formats with one document model. |
| LibreOffice for legacy Office and OpenDocument | MPL-2.0 (AMBER), an image several hundred MB larger, outside the format scope. |
| GCP Document AI Layout Parser now | D7. Also about $10 per 1,000 pages against under $0.10 of worker CPU (source research cost model at the measured FAST speed), data leaves the cluster, a residency review is needed. Additive later ([roadmap.md](../roadmap.md#engine-choice-per-deployment)). |
| Marker/Surya, MinerU, opendataloader-pdf, PP-StructureV3, pdf_oxide, markitdown (PDF), xberg, Textract | Rejected in licence step 2: weight licences, AGPL weights, JVM/MPL, too heavy, no layout, no heading levels. |

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Docling PDF is much slower than pymupdf4llm | Long waits for big PDFs | Page cap and longer limits ([runtime.md](../runtime.md#time-limits)); later external split. |
| The wait for the parse slot counts inside the activity | Documents time out when many long PDFs arrive at once | Limits sized for one maximum parse and a short queue; more replicas; alert on parse duration. |
| Whole-document conversion needs more memory than the pod has | OOM kill | Page ranges and `MALLOC_ARENA_MAX=2`; gates G5 and G10. |
| Scanned PDFs and images | No text, as before | Out of scope (D2); pages without a text layer are logged ([pdf.md](../pdf.md#pages-without-a-text-layer)). |
| The font-size heading rule fails where headings use body size | Flat or wrong levels | Fallback to the numbering depth, then Docling's level; gate G3. |
| The word repairs change a correct word | Rare wrong split or join | Repairs only on pdfium evidence of the same page, counted; never in code ([pdf.md](../pdf.md#structure-pass)). |
| Large spreadsheets and ZIP bombs | Memory spikes, OOM of the single-slot worker | The guard's limits on both paths; output cap; G10 with files at the limits. |
| XML external entities in OOXML | Data leak | No entity resolution, no DTDs; gate G12. |
| A file that is not its stored format | Limits of the real format skipped | One format per conversion ([formats.md](../formats.md#one-format-per-conversion)). |
| Docling release churn | Output drift | Exact pins of packages and model revisions; the engine id holds both ([engines/docling.md](../engines/docling.md#upgrades)). |
| Torch wheels | CUDA wheels of the default index carry proprietary terms | The PyTorch CPU index; licence gate G6. |
| Small evaluation set (born-digital English PDFs, generated Office files) | Hidden failure modes | Gate G3 with a real corpus. |

## When the decision no longer holds

- Gate G3 shows a systematic loss against pymupdf4llm.
- A customer or a policy requires a managed engine (then the GCP adapter of
  [roadmap.md](../roadmap.md#engine-choice-per-deployment) comes into scope).
- Docling or its weights change licence.
- worker-parse cannot get 3 GiB.

## Cutover and rollback

### Steps

Branch by abstraction: each step changes one thing, so that parity is provable.

| Step | Change | Images | Rollback |
|---|---|---|---|
| Interim | `pymupdf` and `pymupdf4llm` move to an optional extra that only the worker-parse image installs. | api and worker-agents lose PyMuPDF (about 186 MB each). | Redeploy the previous tags. |
| Port | `DocumentEngine` and `PdfStructure`; the old extractor wrapped as the first engine, byte-identical output. | No change. | Revert the commit. |
| Docling for PDF | Docling engine, pypdfium2 wrapper, page cap, longer limits, graph revision rule; gates G1–G10. | New worker-parse image with Docling and without PyMuPDF; `pymupdf`, `pymupdf4llm`, `pymupdf-layout` leave `uv.lock`. | Redeploy the previous worker-parse tag (internal only: an image with PyMuPDF must not be distributed). |
| Office and web formats | Upload API, storage keys and prefix cleanup, Drive classes, the parse path by media type, limits, frontend; gates G11–G13. | Same worker-parse image. | Revert: the API and Drive go back to PDF only; Office and web documents already parsed keep their Markdown. |

### Data

- No schema change and no data migration. A document keeps the Markdown of the engine that made it, and
  `parser` tells which; there is no point of no return on data.
- Existing PDF documents keep their pymupdf4llm Markdown (D5). That Markdown is program output in Postgres; no
  AGPL code runs to serve it.
- Drive files imported as stored only before keep their `original.bin` key and no document row; a re-import of
  the source turns them into documents.

### Deploy order

The general rule is in [runtime.md](../runtime.md#deploy-order). For this change: an older parse worker reads only
`original.pdf`, and older Drive workers reject the `document` item kind. Office and web originals stored as `original.<ext>` are removed
by document-prefix cleanup only in the new code, so an older `api` leaves them behind after a removal. Parses
started before the deploy keep the old 60-minute execution limit while the stale-upload limit is already
250 minutes, so a long old parse can read as failed once.

## Changed areas

| Area | Where | Change |
|---|---|---|
| Dependencies | `backend/pyproject.toml`, `backend/uv.lock` | `parse` extra; PyTorch from the CPU index; PyMuPDF removed ([engines/docling.md](../engines/docling.md#packages)). |
| Images | `backend/worker-parse.Dockerfile` (new), `backend/worker.Dockerfile`, `docker-compose.yml`, `Makefile` | A worker-parse image with baked models; 3 GiB ([runtime.md](../runtime.md#images)). |
| Parser | `backend/src/file_parsers/` | Formats table, facade, limits, engines, structure pass and word repairs, pypdfium2 wrapper, guard and Office structure; PyMuPDF modules and the stub removed ([architecture.md](../architecture.md#modules)). |
| Parse runtime | `backend/src/temporal/documents/`, `backend/src/temporal/sources/` | Pipeline by media type, `original_key`, progress, new codes, time limits, a general Drive file import. |
| Upload API and batches | `backend/src/api/uploads.py`, the documents and source-batch routers, `backend/src/api/batch_files.py`, `backend/src/db/repositories.py`, `backend/src/db/batches.py` | Canonical types, first-byte checks, byte caps, no PDF hardcodes ([formats.md](../formats.md#upload-api)). |
| Storage and removal | `backend/src/storage/`, `backend/src/db/removal.py`, `backend/src/temporal/cleanup/activities.py` | Keys by media type, prefix delete, re-read of every processed format. |
| Drive | `backend/src/drive/categories.py`, `backend/src/db/sources.py`, `backend/src/db/queries/repair/b2_items.sql` | Processing class by MIME type (N1). |
| Errors | `backend/src/db/source_files.py`, `backend/src/temporal/documents/workflows.py` | The new codes and their reasons ([errors.md](../errors.md)). |
| Graph | `backend/src/api/graph_build/corpus.py` | The revision rule. |
| Frontend | `frontend/lib/domain/upload-formats.ts`, the sources components, `frontend/lib/api.ts`, tests | Types by extension, canonical `Content-Type`, size messages ([formats.md](../formats.md#frontend)). |
| Licence | `NOTICE`, `validation/ci/licence_gate.py`, `backend/licence-gate.toml`, CI | PyMuPDF removed from `NOTICE`, Docling and its components added; gate G6. |
| Development | `backend/scripts/fetch_docling_models.py`, `make models`, `backend/tests/conftest.py` | Pinned models for host tests and CI. |
| Tests | `backend/tests/` | Structural quality gates (D6: `extraction.md` stays), the engine id, one fixture per format, hostile files; sample data with the literal `pymupdf4llm` stays, a legal legacy value. |
| Docs | `docs/configuration.md`, `QUICKSTART.md`, this bundle | Deployment settings; the parser documentation. |
