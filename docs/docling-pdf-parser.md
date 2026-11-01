# Docling as the default document parser (research and design)

- Status: **Approved** by the owner on 2026-11-01, with the defaults of N1–N4 and D4 changed to FAST (§9). It is the input of the implementation plan (`plans/`, local only, not committed).
- Measurements: 2026-10-31 and 2026-11-01.
- Terms: **source research** = the owner-provided report "SpecFlow Source-to-Markdown Parsing: Engine Shortlist by Source Type" (2026-10-31). **RED / AMBER / GREEN** = licence classes of the licence audit; "licence step 2" = its alternatives step. **FAST / ACCURATE** = Docling TableFormer modes. **Golden file** = `backend/tests/data/specs/extraction.md` (status `REVIEW_REQUIRED` in its header). **Office/web formats** = DOCX, PPTX, XLSX, HTML, Markdown, CSV.

## 0. TL;DR

- **Decision:** replace pymupdf4llm (AGPL-3.0) with **Docling 2.145** (MIT code; weights Apache-2.0 and CDLA-Permissive-2.0, verified on Hugging Face) behind a small `DocumentEngine` port in the kept `file_parsers` package. **The same engine also parses DOCX, PPTX, XLSX, HTML, Markdown and CSV** (owner, 2026-11-01). The upload API, the Drive import and the frontend accept them. Google Docs, Slides and Sheets keep `drive-convert`.
- **PDF quality: as good or better than pymupdf4llm, but only with two SpecFlow post-processing steps** (heading levels by font size, glued-word repair; deterministic, pypdfium2, 2 s per 216 pages):
  - fixture (8 pages): headings 9/9 at the exact level, tables 3/3, word F1 0.986 (pymupdf4llm 0.975);
  - 216-page spec: headings 357/358 at the exact level (pymupdf4llm 357/358), table-cell recall **0.90 vs 0.79**, word F1 0.981 vs 0.983.
- **Office/web quality:** word recall ≥ 0.984, all 228 tables, every heading (DOCX needs a one-level shift); 0.2–10 s for the whole spec per format; no ML models; the format packages add 7 MB, all MIT/BSD.
- **Speed (PDF): Docling FAST is about 9× slower** than pymupdf4llm (618 s (10.3 min) vs 70 s for the 216-page spec on 2 vCPU; ACCURATE 835 s). The owner does not optimise speed now; a later external split (serverless steps or Cloud Run) will parallelise. This PR: 10-page ranges one after the other in the existing activity, a 500-page cap, and longer Temporal time limits. No new workflow or queue.
- **Memory:** a whole document peaks at 4.7 GB; **10-page ranges cap it at 1.8 GB** → worker-parse at 2 vCPU / 3 GiB.
- **Unstructured (measured as asked):** rejected — `hi_res` is about 3× slower than Docling FAST, weak tables, a silent empty result on the fixture, and licence gaps (poppler GPL binary, layout weights without licence metadata).
- **Graph:** a re-parsed document gets `revision_id = content_hash:parser`; legacy rows keep `content_hash`, so the deploy does not rebuild every graph (D10, a deviation from the ticket).
- **Safety:** byte caps at upload; ZIP, cell and output limits in the parse activity, which both upload and Drive use; no remote or local fetch for HTML/Markdown (N2, §4.5).
- **Out of scope:** OCR (scans, images), legacy `.doc/.ppt/.xls`, OpenDocument, EPUB, email, managed engines (GCP Document AI), parallel split.

## 1. Scope

| | |
|---|---|
| In scope | PyMuPDF/pymupdf4llm (AGPL-3.0) leaves every image. Docling becomes the engine of `parse_document` for PDF and for DOCX, PPTX, XLSX, HTML, Markdown and CSV, through the upload API, the Drive import and the frontend upload. |
| Out of scope | OCR of scanned pages and images (D2). Legacy `.doc/.ppt/.xls` (need LibreOffice, AMBER). OpenDocument, EPUB, email. Managed engines (GCP Document AI, AWS) (D7). Speed work: fan-out or external split (D1). LLM enrichment changes. Google-native files (stay on `drive-convert`). |
| Kept | The `file_parsers` package with its LLM enrichers (owner instruction). It loses only its PyMuPDF calls. The golden file `extraction.md` stays as it is (D6). |
| Settled before this work (ticket) | Docling is the target (licence step 2). PyMuPDF is RED. No Artifex licence, no AGPL exception. |

## 2. Current state (verified in code)

```
API POST /documents/{id}/parse ─► ParseDocumentWorkflow ─► parse_document activity (worker-parse, queue "parse")
  temporal/documents/activities.py: 1 slot per worker, 1 CPU thread, heartbeat 20 s (timeout 60 s), start-to-close 45 min
    └─► file_parsers/pdf/to_markdown.py  pdf_file_to_markdown(path) -> ParsedMarkdown(markdown, parser="pymupdf4llm")
          └─► PDFParser.parse() ─► PdfMarkdownExtractor ─► pymupdf4llm.to_markdown(page_chunks, write_images, dpi=170, table_output="markdown")
              PDFParser.render() ─► PDFRenderer: "<!-- source-page: N -->" + page Markdown, "![pN.figK](images/…)" placeholders
  ─► documents.markdown (Postgres) + documents.parser
  ─► graph build: revision_id = documents.content_hash (SHA-256 of the original upload), api/graph_build/corpus.py:86
```

Parser facts:

- PyMuPDF is used in 3 places: `markdown_extractor.py` (the production call), `markdown_extractor._png_size` (`open_pixmap`), and `math_enricher.py` (page render, clip render, `find_tables`; LLM path, CLI only). The typed wrapper is `pymupdf_typed.py`. Other references: `src/pymupdf4llm.pyi`, `validation/python/lint/ruff.toml`, `NOTICE`, and tests that assert `parser == "pymupdf4llm"`.
- pymupdf4llm 2.0 can OCR, but only when RapidOCR or Tesseract is installed (`pymupdf4llm/helpers/document_layout.py`). The production lock has neither and the worker image has no Tesseract, so **production does not OCR today**.
- The stored figure links point to PNG files in a temporary directory that is deleted after the parse. Production uses only the placeholders.
- `parser` values in the database: `pymupdf4llm` (PDF) and `drive-convert` (`temporal/sources/activities.py:98`). A changed `revision_id` marks a graph document "revised" and rebuilds it (`graph/build/changes.py:96`).

Runtime facts:

- Compose runs worker-parse with `mem_limit: 2g` and a 1 GB `/tmp`.
- The single parse slot is a semaphore **inside** the activity (`temporal/documents/activities.py:152-160`), so the wait for the slot counts against the 45-min start-to-close. The workflow has a 60-min execution timeout (`temporal/documents/workflows.py:36-37`); the Drive import waits up to 60 min for a parse (`temporal/sources/activities.py:97`). `max_concurrent_activities` is unset on purpose (`temporal/worker.py:58`).
- No page cap exists (only `MARKDOWN_CAP` = 1,000,000 characters in the API response, `api/routers/documents.py:43`). No upload size limit exists (`api/uploads.py:3`). Parse failure codes are app-level (`db/repositories.py:34`, `temporal/documents/workflows.py:42`), not a database constraint.

Format facts (for the Office/web scope):

- Uploads are PDF-only in three places: the content type (`api/uploads.py:30`, used by `api/routers/documents.py:101` and `api/routers/source_batches.py:127`), the `%PDF-` magic check (`api/uploads.py:20`), and the frontend (`frontend/components/mission-control/sources/source-logic.ts:250`, `SourcesMissionControl.tsx:223`, `frontend/lib/api.ts:125,142`).
- `documents.media_type` and `documents.original_key` already exist (`db/models.py:202,205`), so no migration is needed. But five places rebuild the key `…/original.pdf` instead of reading `original_key` (`storage/keys.py:58`, used by `temporal/documents/activities.py:151`, `temporal/sources/activities.py:389`, `temporal/cleanup/activities.py:90`, `api/batch_files.py:190`, `db/removal.py:134`).
- Drive categorises by MIME type only (`drive/categories.py`, DATA-GDUI-0001.AC5): Word, PowerPoint and Excel (OOXML and legacy) are `stored_only`; HTML, Markdown and CSV fall into `other_files` (`stored_only`). Only `pdf` and the Google-native categories are `processed`. `connection_items.category` has a CHECK constraint over `ITEM_CATEGORIES` (`db/models.py:62,290`), so a new category needs a migration; the processing class has its own CHECK (`db/models.py:76`).
- More PDF assumptions: `db/repositories.py:256` writes `media_type=PDF_MEDIA_TYPE`; `db/batches.py:399` hardcodes the MIME type and `category="pdf"` for batch items; the Drive import picks a path with `_kind` (`temporal/sources/activities.py:320`) and `_import_pdf` (`:382-405`), and `ItemKind` (`temporal/sources/models.py:14`) knows only PDF and Google files; `db/removal.py:85` re-reads only PDFs; `db/queries/repair/b2_items.sql:13` maps non-PDF files to `other_files`. The Drive import streams files without content checks (`temporal/sources/activities.py:390`).
- Error reasons: `db/source_files.py:155,235,425` map only `pdf_parse_failed` to the reason `read_failed`. Non-retryable parse errors come from `FAILURE_CODES` (`temporal/documents/workflows.py:46`).
- Limits that depend on the parse: `PARSE_RETRY` allows 3 attempts (`temporal/documents/workflows.py:50`); the Drive wait activity has a 65-min start-to-close (`temporal/sources/workflows.py:85`); drive-convert caps its Markdown at 5 MB (`MAX_MARKDOWN_BYTES`, `temporal/sources/activities.py:90`).
- Key cleanup: `db/removal.py:132-135` and `temporal/cleanup/activities.py:90-92` delete fixed key variants (`.pdf`, `.json`, `.bin`), not a prefix.

## 3. Benchmarks

### 3.1 Method

- Machine: cloud VM, Intel Xeon @ 2.10 GHz (exact model not exposed), 4 vCPU, 15 GB. Every run is pinned to **2 cores** (`taskset`), the worker-parse sizing, unless the row says otherwise.
- Versions: Docling 2.145.0 (docling-core 2.105.0, docling-ibm-models 4.0.4, docling-parse 7.24.2), torch 2.15.0+cpu; pymupdf 2.0 / pymupdf4llm 2.0 (the locked production versions, in a separate venv without any OCR engine, as in production); unstructured 0.27.36 + unstructured-inference 1.6.17, Tesseract 5.3.4.
- Models were downloaded before the runs. Model load time (Docling ≈ 4 s per process) is not in "convert".
- Every engine output is rendered to the same shape: `<!-- source-page: N -->` per page, a figure placeholder per picture, GFM tables.
- **Quality input 1:** `backend/tests/data/specs/extraction.pdf` (8 pages) against the golden file. The golden file is not neutral: its pages 1–5 are pymupdf4llm output, and pages 7–8 are LLM LaTeX. So the evaluator removes all `llm-*` blocks and all math spans before it compares words. Scores on pages 1–5 favour pymupdf4llm by construction.
- **Quality input 2 and speed input:** a 216-page A4 PDF rendered from `specflow-specification.md` with pandoc + Chromium (the owner accepted this rendering, D9). The source Markdown is exact ground truth.
- **Quality input 3 (Office/web):** the same Markdown converted to DOCX and PPTX by pandoc, to XLSX (the first 40 tables, one sheet each) by openpyxl, plus the pandoc HTML and the Markdown itself; a 3-slide PPTX with real title placeholders (python-pptx); a small CSV.
- Metrics: word recall / precision / F1 (multiset of words), word-order similarity (fixture only), headings found, exact heading level and pairwise heading order, GFM tables and table-cell recall, figure placeholders per page.

### 3.2 PDF quality: fixture (`extraction.pdf`, 8 pages)

| Engine and configuration | s/page | Word F1 | Order | Headings found | Level exact | Pair order | Tables | Cell recall | Figures (golden 0,1,0,0,1,1,0,0) |
|---|---|---|---|---|---|---|---|---|---|
| pymupdf4llm (production call) | 0.45 | 0.975 | 0.973 | 9/9 | 9/9 | 36/36 | 3/3 | 1.00 | 1,1,0,0,1,1,**7**,0 |
| Docling, tables ACCURATE | 3.15 | 0.985 | 0.966 | 9/9 | 1/9 (flat `##`) | 6/36 | 3/3 | 1.00 | exact |
| Docling + built-in `HeadingHierarchyOptions` | 3.26 | 0.985 | 0.966 | 9/9 | 0/9 | 13/36 | 3/3 | 1.00 | exact |
| Docling, TableFormer **V2** | 3.39 | 0.963 | 0.951 | 9/9 | 0/9 | 13/36 | 3/3 | **0.09** | exact |
| Docling, **pypdfium2** backend | 3.37 | 0.993 | 0.971 | 9/9 | 4/9 | 12/36 | **2/3** | **0.06** | exact |
| **Docling ACCURATE + SpecFlow post-processing** | 3.15 + 0.01 | **0.986** | 0.968 | 9/9 | **9/9** | **36/36** | 3/3 | 1.00 | exact |
| Docling FAST + SpecFlow post-processing | 2.71 + 0.01 | **0.986** | 0.976 | 9/9 | **9/9** | **36/36** | 3/3 | 1.00 | exact |
| Unstructured `hi_res` (+ table structure) | 10.6 | 0.969 | 0.968 | 8/9 | 1/9 | 6/28 | 3/3 | 0.92 | exact |
| Unstructured `fast` | 0.34 | – | – | – | – | – | – | – | **0 elements, no error** |

Notes:

- pymupdf4llm figure count on page 7: the 7 display equations become images. The golden file has LaTeX there (from the LLM path).
- Docling defects that the SpecFlow post-processing fixes: flat heading levels, and glued words (`justinzhao`, `judgesmove`, `fromthe`; 25 repairs on the fixture).
- Docling defects that stay: (a) it joins a paragraph that continues across a page break to the previous page, so that text is cited one page early (seen once: page 3 → 4); (b) `ω = 0.69` becomes `ω = 0▷69` (font glyph map; pymupdf4llm gives `0 _._ 69`, also wrong); (c) running headers and footers are dropped (better for the graph; the golden file keeps them).
- Docling OCR auto: the same word scores, order similarity 0.966 → 0.945, +50 % time. Picture images on: no change, +1 % time.

### 3.3 PDF quality: spec PDF (216 pages, ground truth = source Markdown)

| Engine and configuration | Word F1 | Headings found | Level exact | False headings | Tables (source 228) | Cell recall |
|---|---|---|---|---|---|---|
| pymupdf4llm | 0.983 | 358/358 | 357/358 | 58 | 301 | 0.79 |
| Docling ACCURATE, raw | 0.981 | 357/358 | 44/358 | 45 | 297 | 0.90 |
| **Docling ACCURATE + post-processing** | 0.981 | 357/358 | **357/358** | 45 | 297 | **0.90** |
| Docling FAST (10-page ranges) + post-processing | 0.981 | 357/358 | **357/358** | 45 | 297 | **0.90** (0.897) |
| Unstructured `fast` | 0.959 | 299/358 | 7/358 | 3,404 | **0** | 0.00 |

Unstructured `hi_res` was too slow for 216 pages (≈ 32 min), so it ran on pages 1–30. Same 30-page cut for all engines (ground truth = the source prefix up to word 8,723, the word count of pymupdf4llm's pages 1–30; `evaluate_spec.py --prefix-words 8723`):

| Engine (pages 1–30) | Word F1 | Headings found | Level exact | False headings | Tables (source 27) | Cell recall |
|---|---|---|---|---|---|---|
| pymupdf4llm | 0.918 | 46/46 | 45/46 | 8 | 39 | 0.84 |
| Docling ACCURATE + post-processing | 0.918 | 45/46 | 45/46 | 8 | 35 | **0.95** |
| Unstructured `hi_res` | 0.797 | 42/46 | 7/46 | 13 | 35 | 0.44 |

Notes: more tables than in the source = a table cut by a page break counts once per page (all engines). Word F1 on the cut is below 1 for every engine because the cut is approximate. Docling writes `<` and `>` as `&lt;`/`&gt;` by default (`escape_html`); pymupdf4llm writes them raw and has ligature errors (`efciencyfi`).

### 3.4 PDF speed and memory: spec PDF (216 pages)

| Run | Cores | Wall time | s/page | Peak RSS |
|---|---|---|---|---|
| pymupdf4llm (production call, no OCR engine) | 2 | **70 s** (1.2 min) | 0.32 | 0.45 GB |
| Docling ACCURATE, whole document, 1 process | 2 | 907 s (15.1 min) | 4.20 | **4.7 GB** |
| Docling ACCURATE, 10-page ranges, 1 process, `MALLOC_ARENA_MAX=2` | 2 | **835 s (13.9 min)** | 3.87 | **1.9 GB** (1.7 → 1.9 GB over 22 ranges) |
| **Docling FAST, 10-page ranges, 1 process, `MALLOC_ARENA_MAX=2`** (decided, D4) | 2 | **618 s (10.3 min)** | 2.86 | **1.8 GB** (flat after the 4th range) |
| Docling FAST, split in 2 × 108 pages, 2 processes × 1 thread | 2 | 584 s (9.7 min) | 2.70 | 3.2 GB per process |
| Docling FAST, split in 4 × 54 pages, 4 processes × 1 thread | 4 | 292 s (4.9 min) | 1.35 (wall) | 2.5 GB per process |
| Unstructured `hi_res` (pages 1–30, extrapolated) | 2 | ≈ 1,930 s (32 min) | 8.95 | 2.0 GB |
| Unstructured `fast` (no tables, see §3.3) | 2 | 48 s | 0.22 | 0.6 GB |

- The ticket trial reports 1.3–1.8 s/page and 1.4–1.5 GB. The ticket does not name its machine or corpus; this spec is table-heavy (228 tables). The gap is not explained, so G4 is re-measured on the real worker-parse node.
- Run-to-run noise: two identical 8-page runs differed by about 20 %. Some runs shared the machine with another pinned run on the other cores; read the numbers as ±20 %.
- Page splitting scales linearly with cores (2 → 4 cores: 2.1×) and changes only paragraphs that cross a range boundary (213/216 pages identical between 10-page and 108-page ranges). This supports the owner's later external split (D1).

### 3.5 Office/web quality and speed (whole spec per format, 2 cores)

| Format (generated from the spec) | Convert | Word recall / precision | Headings found | Level exact | Tables (source 228) | Cell recall | Page markers |
|---|---|---|---|---|---|---|---|
| DOCX (pandoc) | 7.5 s | 0.984 / 1.00 | 358/358 | 0/358 (all one level deep; the shift in §4.3 gives 358/358) | 228 | 1.00 | none (no pages) |
| PPTX (pandoc, 229 slides) | 10.0 s | F1 0.989 | 1/358 (the pandoc deck has no title placeholders) | – | 228 | 1.00 | 229 pages in the document model |
| PPTX (python-pptx, real title placeholders) | <1 s | all text | 3/3 slide titles as `#` | – | – | – | per-slide export (`page_no`) verified |
| XLSX (40 tables, one sheet each) | 0.15 s | – | – | – | 40/40 | 1.00 | 40 pages (one per sheet) in the document model |
| HTML (pandoc) | 5.7 s | 0.999 / 1.00 | 358/358 | 358/358 | 228 | 0.95 | none |
| Markdown (the source) | 3.9 s | 0.999 / 1.00 | 358/358 | 358/358 | 228 | 0.95 | none |
| CSV (3 rows) | <1 s | all cells | – | – | 1/1 | 1.00 | none |

- No ML model runs for these formats (XML/HTML backends); peak RSS ≤ 0.6 GB on these small files (the largest, HTML, is 0.8 MB). The format runs exported each document in one block: the markers and the sheet-name headings in §4.3 are design, checked by G11, not measured here.
- Packages: the `docling-slim` extras `format-docx, format-pptx, format-xlsx, format-html, format-markdown` add python-docx, python-pptx, openpyxl, beautifulsoup4, soupsieve, marko, et-xmlfile, xlsxwriter, pylatexenc: **+7 MB**, all MIT or BSD. Every format above converted in that minimal install.
- HTML/Markdown cell recall 0.95: in cells with several inline-code spans, Docling writes a space before the comma (`FR-IAM , FR-PRJ` for `` `FR-IAM`, `FR-PRJ` ``). The text is present; a whitespace rule in the post-processing can remove the space if it matters.

### 3.6 Findings that drive the design

1. **PDF quality:** Docling + two SpecFlow post-processing steps is equal to or better than pymupdf4llm on every structural metric that was measured; tables are better (spec cell recall 0.90 vs 0.79). Without the post-processing, heading levels are flat. So the post-processing is required.
2. **Docling's own switches do not fix the two defects:** `HeadingHierarchyOptions` orders levels wrongly, the pypdfium2 backend breaks tables, TableFormer V2 empties cells.
3. **FAST = ACCURATE quality** on both PDF inputs (spec: same headings, cell recall 0.897 vs 0.90). FAST needs 26 % less time (618 vs 835 s, both in 10-page ranges; fixture pair: 17 %). The owner chose FAST after this measurement (D4).
4. **Speed:** Docling FAST needs 2.86 s/page on 2 vCPU (ACCURATE 3.87), about 9× pymupdf4llm (0.32 s/page). Only more cores reduce wall time (2 processes on 2 cores: +5 %; 4 cores: 2.1×). The owner defers speed to a later external split.
5. **Memory:** whole-document conversion peaks at 4.7 GB; 10-page ranges stay at 1.8 GB (FAST; ACCURATE 1.9 GB). Ranges are mandatory.
6. **OCR:** production has no OCR today; OCR stays out of scope (D2).
7. **Unstructured** is slower and worse on tables, and adds licence gaps (§5).
8. **Office/web formats** are cheap and accurate in Docling; the only structural fix is a one-level heading shift for DOCX.
9. **Image:** the minimal `docling-slim` install needs `opencv-python-headless<5` explicitly, because TableFormer v1 imports `cv2` without declaring it (`docling_ibm_models/tableformer/data_management/tf_predictor.py:10`). With it, the venv is 1.5 GB (full `docling`: 1.7 GB) and the output is byte-identical to the full install. The OpenCV/FFmpeg (LGPL) notice item from the ticket stays.

## 4. Decision

### 4.1 Shape: one port, one engine, SpecFlow owns the structure

```
parse_document activity (contract unchanged: original in storage -> documents.markdown + documents.parser)
 └─ file_to_markdown(path, media_type) -> ParsedMarkdown(markdown, parser=<engine id>)      (was pdf_file_to_markdown)
     ├─ PDF:            PDFParser(engine)                                    file_parsers/pdf/parser.py (kept)
     │                   ├─ engine.extract(pdf, 10-page ranges) -> PDFDocument
     │                   ├─ PdfStructure: heading levels by font size · glued-word repair   (pypdfium2 only)
     │                   ├─ [CLI only] enrich(): figure / Mermaid / math LLM enrichers      (PyMuPDF calls -> pypdfium2)
     │                   └─ PDFRenderer (unchanged) -> "<!-- source-page: N -->" Markdown
     └─ Office/web:     engine.extract(file) -> DoclingDocument -> OfficeStructure (heading shift) -> Markdown
                         markers: "<!-- source-page: N -->" per PPTX slide and per XLSX sheet; none for DOCX/HTML/MD/CSV
```

- **Port `DocumentEngine`** (decision granularity): `engine_id` (stored in `documents.parser`), `formats` (the media types it accepts), and `extract(path, media_type, page_range=None, images=False)`. One implementation ships: `DoclingEngine`. It returns per-page content and does not rank headings or render.
- `PDFDocument` stays the PDF intermediate model (input of `PDFRenderer` and of the LLM enrichers), so the stored PDF Markdown keeps its shape. Office/web formats skip `PDFDocument`: they have no pages to render and no enrichers.
- **Engine id:** `docling@<docling version>+sf<post-processing version>`, for example `docling@2.145.0+sf1`, for every format.
- **Module place:** `file_parsers/engines/docling.py` (the port and the engine, shared by PDF and Office/web), `file_parsers/pdf/structure.py`, `file_parsers/office/` (heading shift and markers). `file_parsers/pdf/` keeps its current modules.

### 4.2 Docling configuration (pinned, CPU, offline)

| Option | Value | Why |
|---|---|---|
| Package | `docling-slim[convert-core,format-pdf,models-local,format-docx,format-pptx,format-xlsx,format-html,format-markdown]==2.145.0` + `opencv-python-headless<5` | Measured: 1.5 GB venv, identical output to the full `docling`, all format packages MIT/BSD (§3.5–3.6). |
| Backend (PDF) | docling-parse (default) | pypdfium2 backend breaks tables (§3.2). |
| Tables (PDF) | TableFormer v1, `do_cell_matching=True`, mode **FAST** (D4) | Same quality as ACCURATE, 26 % less time (§3.4). V2 is not ready. |
| OCR | off, and no OCR package in the image | Out of scope (D2). Production does not OCR today. |
| Picture images | off in production; on for the CLI `--enrich` path only | Production keeps only placeholders. |
| Heading hierarchy (Docling) | off | SpecFlow ranks PDF headings (§4.3). |
| Markdown export | `escape_underscores=False`, `escape_html=False`, `compact_tables=True`; per page (`page_no`) for PDF, PPTX, XLSX | Same text as today: pymupdf4llm writes `_` and `<`/`>` raw. Without `compact_tables`, Docling pads table cells: 30 % larger Markdown on the spec (726,625 vs 560,771 characters); without padding 554,924 (simulated). |
| Threads | `num_threads` = pod vCPU (2) | |
| Models | baked into the image, `artifacts_path`, `HF_HUB_OFFLINE=1`, revisions pinned | No network at run time; reproducible. |
| Allowed formats | `allowed_formats` = PDF, DOCX, PPTX, XLSX, HTML, MD, CSV | Docling refuses every other input. |
| Forbidden | VLM pipelines, picture description, chart, code and formula enrichment, OCR, ASR, remote services, external plugins | Licence, determinism, scope. A unit test checks the built options. |
| Unit of work (PDF) | page ranges of 10 pages, `MALLOC_ARENA_MAX=2` | Memory (§3.4). |

### 4.3 SpecFlow post-processing (deterministic)

PDF (`PdfStructure`, pypdfium2 only):

1. **Heading levels.** Find each heading's text in the pdfium text layer of its page. Read the effective font size (nominal size × text-matrix scale; the nominal size alone is 1.0 in many PDFs). Rank the distinct sizes (0.5 pt buckets) across the whole document: largest → `#`, next → `##`, up to `######`. If the text is not found, use the numbering depth, else keep `##`. Fixture: 9/9 exact, 36/36 pairs. Spec: 357/358 exact (pymupdf4llm 357/358).
2. **Glued words.** A prose token that is not in the page's pdfium word list, but equals 2–3 consecutive pdfium words, gets the spaced form. Tables and code are not touched. Fixture: 25 repairs, word recall 0.986 → 0.992.
3. **Figures.** Keep the placeholder shape `![pN.figK](images/…)`.

Cost: 0.08 s for 8 pages; 2.2 s for 216 pages. Ranking needs every page, so it runs once after the last range.

Office/web (`OfficeStructure`):

1. **Heading shift.** In the measured DOCX, Docling wrote Word "Heading 1" as `##` (it keeps `#` for a Title style). If the output has no `#`, shift every heading up one level: 0/358 → 358/358 exact. The rule rests on one pandoc file; G11 adds a DOCX with a real Title style.
2. **Markers.** `<!-- source-page: N -->` before each PPTX slide and each XLSX sheet (Docling pages); the sheet name follows as a heading. DOCX, HTML, Markdown and CSV have no pages and get no marker.

### 4.4 Long documents and time limits (D1 = sequential, D3 = 500, D11 = longer limits)

- One `parse_document` activity converts a PDF in 10-page ranges, one after the other, and heartbeats after each range with pages done / pages total. No new workflow, queue or activity (owner).
- **Page cap 500 (D3, new):** before any conversion the activity counts pages with pypdfium2 (PDF) or slides (PPTX) and fails with a new non-retryable code `document_too_large`. The code is app-level (new entries in `FAILURE_CODES` and `PARSE_ERROR_CODES`), so no migration.
- **Time limits (D11 = b, values = N4):** the longest allowed parse is 500 pages × 2.86 s ≈ 24 min (+20 % noise ≈ 29 min). The wait for the slot still counts inside the activity, so the limits must also cover a short queue:

Rule: workflow execution ≥ attempts × start-to-close (+ margin); Drive wait ≥ workflow execution; Drive wait activity > Drive wait. A timed-out attempt restarts from page 1, so a third attempt only adds time; two attempts keep one retry for a crashed worker.

| Limit | Today | Proposed (N4) |
|---|---|---|
| `PARSE_START_TO_CLOSE` (`temporal/documents/workflows.py:37`) | 45 min | **120 min** |
| `PARSE_RETRY.maximum_attempts` (`temporal/documents/workflows.py:50`) | 3 | **2** |
| `EXECUTION_TIMEOUT` (`temporal/documents/workflows.py:36`) | 60 min | **250 min** (2 × 120 + 10) |
| Drive import wait `WAIT_LIMIT_SECONDS` (`temporal/sources/activities.py:97`) | 60 min | **250 min** |
| Drive wait activity `WAIT_START_TO_CLOSE` (`temporal/sources/workflows.py:85`) | 65 min | **255 min** |
| Heartbeat timeout | 60 s | 60 s (unchanged: detects a dead worker) |

- 120 min covers one maximum-size parse (29 min) plus about three more waiting for the slot on the same replica; a longer backlog needs more worker-parse replicas. The later external split (owner) removes this limit.

### 4.5 Office/web formats end to end (owner scope: upload + Drive + frontend)

| Area | Change |
|---|---|
| Accepted types | PDF + DOCX, PPTX, XLSX (OOXML), HTML, Markdown, CSV. One table of media types → format → file extension, shared by the API, the Drive import and the parser. |
| Upload API | `is_pdf_content_type` → `accepted_media_type(header, filename)`: the server maps the file extension to a canonical type, because browsers send an empty type for `.md` and `application/vnd.ms-excel` for `.csv` on Windows. The stream checks only what the first bytes show: `%PDF-`, ZIP magic `PK\x03\x04` for OOXML, UTF-8 for text; plus a byte cap counted in `feed` (413). 415 `unsupported_format` names the accepted types. `documents.media_type` stores the type (the column exists; `db/repositories.py:256` and `db/batches.py:399` stop hardcoding PDF). |
| Storage keys | `document_original(pid, did, media_type)` → `original.<ext>`; PDF keeps `original.pdf`, so existing objects stay valid. Code that has the row reads `documents.original_key` (the five places in §2). Removal and cleanup delete by the `documents/{did}/` prefix instead of fixed variants, so no `original.<ext>` object is orphaned. |
| Drive import | Processing class by MIME type inside the existing categories (N1, no migration): OOXML Word/PowerPoint/Excel → `processed`; legacy `.doc/.ppt/.xls` stay `stored_only`; `text/html`, `text/markdown`, `text/csv` stay in `other_files` but become `processed`. `ItemKind`/`_kind` gain a document kind and `_import_pdf` becomes a general file import. Drive matches by MIME only (DATA-GDUI-0001.AC5): a `.md` file that Drive reports as `text/plain` stays `stored_only` (accepted limit). Google-native files keep `drive-convert`. |
| Parse activity | Downloads `original_key`, picks the path by `media_type` (§4.1). It runs the deep checks for both upload and Drive files before conversion (N2): page or slide count, inflated ZIP bytes counted while reading (not the declared sizes), a per-entry compression ratio, XLSX and CSV cells counted while reading (not the sheet dimension tag), and the Markdown size after conversion. New fixed codes `document_too_large` and `document_parse_failed` (N3) with their exception types in `FAILURE_CODES` (non-retryable) and their reasons in `db/source_files.py`; `pdf_parse_failed` stays for PDF. |
| Frontend | `splitUpload` accepts the new types by extension and sends the canonical `Content-Type` (not the browser's `file.type`); the picker `accept` list grows; the texts change: "PDF only" toast (`source-logic.ts:250`), `drive-logic.ts:60`, `HeaderActions.tsx:91`, `Dropzone.tsx:65` ("up to 20 MB" must match N2); the `sources.pdfOnly` flag goes. Read `frontend/AGENTS.md` before the change (custom Next.js version). |
| Limits and safety (N2) | No size limit exists today. Proposed: upload byte caps PDF 100 MB, OOXML 50 MB, HTML/Markdown/CSV 10 MB (the frontend text says the same); OOXML at most 200 MB inflated and a 100:1 ratio per entry; XLSX and CSV at most 200,000 cells; Markdown output at most 5 MB (as drive-convert); PDF and PPTX at most 500 pages or slides (D3). XML parsers must not resolve external entities (G12). HTML and Markdown backends pinned to `enable_remote_fetch=False`, `enable_local_fetch=False`, `fetch_images=False` (the Docling 2.145 defaults, set explicitly); no script runs. |

### 4.6 Engine choice per deployment (GCP Document AI): later, through the port (D7 = a)

- Now: one engine in code, no config key.
- Later (follow-up story): `services.document_parsing` in the cloud profile, with the same rules as `services.object_storage` (`cloud_profile.py`: unknown key or value stops the start-up, env var wins), and one adapter:

```yaml
services:
  document_parsing:
    provider: docling | gcp_document_ai          # default docling on every cloud; local is always docling
    gcp_document_ai: { processor_version: pretrained-layout-parser-v1.0-2024-06-03, location: eu }
```

- Rules for that adapter (source research): store the raw provider JSON next to the Markdown; map `heading-1..5` to levels; the engine id carries the processor version; the graph sees a new engine id as a revision.

### 4.7 The `file_parsers` package stays

- `markdown_extractor.py` (pymupdf4llm) is replaced by the Docling engine. `PDFParser`, `PDFRenderer`, the schema, the prompts and the three LLM enrichers stay.
- `pymupdf_typed.py` becomes `pdfium_typed.py` with the same small surface: open a PDF, render a page or a clip at a DPI to PNG, read a PNG size. `find_tables` (math enricher) is replaced by the table boxes that the Docling engine puts into `PDFPageContent`.
- The CLI (`python -m file_parsers.pdf.parser --enrich`) keeps working; it asks the engine for picture images.
- The transcript parser is not touched.

### 4.8 Graph revision rule (D10 = a)

`revision_id = content_hash` when `parser` is NULL, `pymupdf4llm` or `drive-convert`; else `revision_id = content_hash + ":" + parser`.

- `content_hash:parser` for every row (ticket) would give every existing document a new revision on deploy, so every project graph would rebuild at once (`graph/build/changes.py:96`).
- Effect: only a document that is parsed again with a new engine id is revised, once. New Office/web documents are new lineages.

### 4.9 Images, sizing, operations (D8)

- **Per-role images (owner):** each container gets only its own dependency set and does not reuse another service's image. This PR: a worker-parse image with the `parse` extra (Docling and the format packages); the api and worker-agents images lose PyMuPDF and never get Docling. They need no code change: the queue registration is imported lazily (`temporal/registry.py`). Containers that reuse the api image today (compose `rustfs-init`) are a follow-up.
- worker-parse: 2 vCPU, **3 GiB** (`mem_limit`), `/tmp` 1 GB, models in the image (Docling cache measured: 506 MB including the unused TableFormer V2).
- Logs: one `pdf.stage.done` per page range (pages, duration) through `logger.logger`; the heartbeat carries pages done / pages total.
- Determinism: two runs gave byte-identical Markdown (fixture). Gate G8 keeps it.

## 5. Rejected alternatives

| Alternative | Why it lost |
|---|---|
| Keep pymupdf4llm, buy the Artifex licence | Quote-only and still proprietary terms (ticket). No AGPL exception (ticket). |
| **Unstructured OSS** (`unstructured[pdf]`, measured) | Speed: `hi_res` is 8.9–10.6 s/page, about 3× Docling FAST. Quality: word recall 0.70 and table-cell recall 0.44 on the 30-page spec sample (pymupdf4llm 0.90 / 0.84 on the same cut). Heading levels are flat (1/9 fixture, 7/46 spec sample), partly from the benchmark adapter (`Title` → `category_depth + 1`, no font-size post-processing), so tables and speed decide. `fast` has no tables (0 of 228) and 3,404 false headings on the spec, and **returns 0 elements without an error** on the fixture (cause not diagnosed). Licence: `hi_res` needs the poppler binary (GPL) and Tesseract; pikepdf is MPL-2.0; the `unstructuredio/yolo_x_layout` weights have no licence metadata on Hugging Face (UNKNOWN). The hosted API is external SaaS. |
| Docling built-in `HeadingHierarchyOptions` | Wrong order on the fixture: "3.1" ranks above "1 Introduction" (13/36 pairs correct). |
| Docling `pypdfium2` backend | Fixes glued words, but breaks table cell matching (2/3 tables, cell recall 0.06). |
| Docling TableFormer V2 | Empty cells on the fixture (cell recall 0.09); the "drop OpenCV with V2" path from the ticket is not ready. |
| Docling VLM pipelines (granite-docling, picture description, chart/code/formula enrichment) | GPU-oriented, slower on CPU, not deterministic, some presets carry RED weight licences (Chandra, OpenRAIL; source research §1). |
| Fan-out of page ranges / own `parse-pdf` queue (draft v1) | Owner (D1, D11): no new workflows or queues now; speed comes later from an external split. |
| markitdown for Office/web | A second library with weaker structure mapping (source research §3); Docling already covers the formats with the same document model. |
| LibreOffice for legacy `.doc/.ppt/.xls` and OpenDocument | MPL-2.0 (AMBER), several hundred MB image, out of the owner's format scope. |
| GCP Document AI Layout Parser now | Owner (D7). Also $10 per 1,000 pages vs under $0.10 of worker CPU (source research cost model), data leaves the cluster, residency review needed. Additive later (§4.6). |
| Marker/Surya, MinerU, opendataloader-pdf, PP-StructureV3, pdf_oxide, markitdown (PDF), xberg, Textract | Rejected in licence step 2 (ticket): weight licences, AGPL weights, JVM/MPL, too heavy, no layout, no heading levels. |

## 6. Cutover and rollback

Branch by abstraction: each step changes one thing, so that parity is provable. Delivery (D12): one PR, one commit per implementation session; the sessions in `plans/` follow these steps.

| Step | Change | Images | Rollback |
|---|---|---|---|
| 1. Interim (ticket) | Move `pymupdf`/`pymupdf4llm` to an optional extra that only the worker-parse image installs. | api and worker-agents lose PyMuPDF (−186 MB each, ticket). | Redeploy the previous tags. |
| 2. Port | Add `DocumentEngine` and `PdfStructure`; wrap the current extractor as the first engine. Output must stay byte-identical (existing tests). | No change. | Revert the commit. |
| 3. Docling for PDF | Docling engine (FAST, 10-page ranges), pypdfium2 wrapper, page cap 500, longer limits, graph revision rule. Gates G1–G10. | New worker-parse image: Docling, no PyMuPDF; `pymupdf`, `pymupdf4llm`, `pymupdf-layout` leave `uv.lock`. | Redeploy the previous worker-parse tag (internal only: an image with PyMuPDF must not be distributed). |
| 4. Office/web formats | Upload API, storage keys and prefix cleanup, Drive processing classes, parse path by media type, limits, frontend. Gates G11–G13. | Same worker-parse image (the format packages ship in step 3). | Revert: the API and Drive go back to PDF-only. Office/web documents already parsed keep their Markdown. |

- No schema change and no data migration. A document keeps the Markdown of the engine that made it; `parser` tells which. No point of no return on data.
- Existing PDF documents keep their pymupdf4llm Markdown (D5 = a). That Markdown is program output in Postgres; no AGPL code runs to serve it.
- Drive files already imported as `stored_only` sit at `original.bin` without a document row. New imports after step 4 process them; turning the old ones into documents needs a re-import of the source (the implementation plan checks this path).

## 7. Blast radius

| Area | Files | Change |
|---|---|---|
| Dependencies | `backend/pyproject.toml`, `backend/uv.lock` | `parse` extra with `docling-slim[...]` and `opencv-python-headless<5`; torch from the PyTorch **CPU** index; remove `pymupdf*`; deptry map. |
| Images | `backend/worker.Dockerfile` (+ a worker-parse variant), `docker-compose.yml` | Separate worker-parse image; models baked in (`artifacts_path`), `HF_HUB_OFFLINE=1`, `MALLOC_ARENA_MAX=2`; `mem_limit` 2g → 3g. |
| Parser | `src/file_parsers/` | `engines/docling.py` (port + engine), `pdf/structure.py`, `office/` (heading shift, markers); `pdf/markdown_extractor.py` replaced; `pdf/pymupdf_typed.py` → `pdf/pdfium_typed.py`; `pdf/math_enricher.py` uses pypdfium2 renders and Docling table boxes; `pdf/to_markdown.py` → `file_to_markdown`; `pdf/schema.py` gains optional table boxes. |
| Activity and workflow | `src/temporal/documents/activities.py`, `src/temporal/documents/workflows.py`, `src/temporal/sources/activities.py`, `src/temporal/sources/workflows.py`, `src/temporal/sources/models.py` | Path by media type, read `original_key`, deep checks and caps, progress heartbeat, new codes; the five limits (§4.4); `ItemKind`, `_kind`, a general file import. |
| Upload API and batches | `src/api/uploads.py`, `src/api/routers/documents.py`, `src/api/routers/source_batches.py`, `src/api/batch_files.py`, `src/db/repositories.py`, `src/db/batches.py` | Canonical types by extension, first-byte checks, byte caps (N2); no PDF hardcodes. |
| Storage and removal | `src/storage/keys.py`, `src/temporal/cleanup/activities.py`, `src/db/removal.py` | Key by media type; read `original_key`; delete by prefix; re-read rule not PDF-only. |
| Drive | `src/drive/categories.py`, `src/db/sources.py`, `src/db/queries/repair/b2_items.sql` | Processing class by MIME type inside the existing categories (N1). |
| Errors | `src/db/repositories.py`, `src/temporal/documents/workflows.py`, `src/db/source_files.py` | `document_too_large`, `document_parse_failed` (N3), their exception types and reasons. |
| Graph | `src/api/graph_build/corpus.py`, `src/db/sources.py` (`GraphCorpusRow` gains `parser`) | Revision rule §4.8. |
| Frontend | `frontend/components/mission-control/sources/source-logic.ts`, `SourcesMissionControl.tsx`, `drive-logic.ts`, `HeaderActions.tsx`, `frontend/components/ui/Dropzone.tsx`, `frontend/lib/api.ts`, tests | Types by extension, canonical Content-Type, texts, `pdfOnly` flag removed. No category enum change (N1). |
| Lint and stubs | `src/pymupdf4llm.pyi` (delete), `validation/python/lint/ruff.toml` | Remove the pymupdf4llm entries. |
| Tests | `tests/test_specflow_parsing.py`, `tests/test_pdf_parser_logging.py`, upload and Drive category tests, new format fixtures | Docling engine id; structural quality tests (D6: `extraction.md` stays); one small fixture per new format. Sample data with the literal `"pymupdf4llm"` can stay (legal legacy value). |
| Licence files | `NOTICE` | Remove PyMuPDF; add Docling, docling-parse, docling-core, docling-ibm-models (MIT), model weights (Apache-2.0, CDLA-Permissive-2.0), torch (BSD-3, bundled `libgomp`), OpenCV (Apache-2.0, bundled FFmpeg LGPL), pypdfium2 and PDFium (Apache-2.0 / BSD-3-Clause), python-docx, python-pptx, openpyxl, beautifulsoup4, soupsieve, marko, et-xmlfile, pylatexenc (MIT), xlsxwriter (BSD-2-Clause), lxml (BSD-3-Clause), tqdm and certifi (MPL-2.0). |
| Docs | `docs/configuration.md`, the specification | Accepted formats, limits, new error codes. |

## 8. Risks and what would invalidate this decision

| Risk | Effect | Mitigation |
|---|---|---|
| Docling PDF is about 9× slower than pymupdf4llm | Long waits for big PDFs | Page cap 500, longer limits (§4.4); later external split (owner). |
| Queue backlog: the wait for the slot counts inside the activity | Documents time out when many long PDFs arrive at once | Limits sized for one maximum parse + a short queue; more replicas; alert on parse duration. |
| Memory: whole-document conversion peaks at 4.7 GB | OOM kill above `mem_limit` | 10-page ranges + `MALLOC_ARENA_MAX=2`. Gates G5, G10. |
| Scanned PDFs and images | No text (same as today) | Out of scope (D2). Log pages without a text layer, so the need is measured. |
| The font-size heading rule fails where headings use body size | Flat or wrong levels | Fallback: font size → numbering depth → `##`. Gate G3. |
| Glued-word repair splits a real compound word | Rare false split | Only tokens absent from the pdfium word list that equal 2–3 consecutive pdfium words on the same page; never in tables or code. |
| Large spreadsheets and ZIP bombs | Huge Markdown, memory spikes, OOM of the 3 GiB single-slot worker | Byte caps at upload; inflated bytes, ratio and cells counted while reading in the parse activity (both paths); output cap; G10 with a maximum-size Office file. |
| XML external entities in OOXML | Data leak | Parsers without entity resolution; security fixtures (G12). |
| Docling release churn | Output drift | Exact pins (package and model revisions); the engine id holds both; an upgrade passes G1, G2, G11. |
| Torch wheels | CUDA wheels from the default index have proprietary terms | Pin the PyTorch CPU index; licence gate in CI. |
| Small evaluation set (born-digital English; Office files generated by pandoc) | Hidden failure modes | Gate G3: a real corpus (PDF and Office) before the default switch. |

The decision is invalid if: the corpus gate G3 shows a systematic loss; a customer or policy requires a managed engine (then the GCP adapter becomes in scope); Docling or its weights change licence; or worker-parse cannot get 3 GiB.

## 9. Decisions

### 9.1 Owner decisions (2026-11-01)

| # | Decision | Owner answer |
|---|---|---|
| D1 | Long-document strategy | Speed is not a goal now; a later external split (serverless steps or Cloud Run) will parallelise. This PR: sequential 10-page ranges (§4.4). |
| D2 | OCR for pages without a text layer | Out of scope. |
| D3 | Page cap | 500 pages (new). |
| D4 | Table mode | FAST (first ACCURATE; changed after the 10-page-range measurement: 26 % less time, the same quality). |
| D12 | Delivery | One PR on `fix/docling-pdf-parser`, one commit per implementation session; step 1 of §6 folds into the first session. The implementation plan has no HITL gates (the design is locked) and is not committed. |
| D5 | Existing PDF documents | (a) keep their pymupdf4llm Markdown. |
| D6 | Regression test | (a) structural thresholds; keep `extraction.md` as it is, because LLM steps will be added over time to raise quality. |
| D7 | Engine config key | (a) later, with a second engine. |
| D8 | Images | Separate images: each container has its own SDKs and packages and does not reuse the api image. |
| D9 | Speed PDF | The Chromium rendering of the spec is accepted. |
| D10 | Graph revision rule | (a) `content_hash:parser` only for new engine ids. |
| D11 | Queue wait and timeouts | (b) no new workflows or queues; extend the time limits. |
| S1 | Formats in this PR | PDF + DOCX, PPTX, XLSX + HTML, Markdown, CSV. |
| S2 | Entry points | Upload and Drive files. |
| S3 | Google Docs/Slides/Sheets | Keep `drive-convert`. |
| S4 | Frontend | Changes in this PR. |

### 9.2 Decided questions (the owner approved the defaults, 2026-11-01)

| # | Question | Options | Decided |
|---|---|---|---|
| N1 | Drive: how HTML, Markdown and CSV files become `processed` | (a) processing class by MIME type inside the existing categories (`other_files`); (b) a new category "Web and text files" | (a) — no migration: (b) needs a migration of the `ITEM_CATEGORIES` CHECK constraint, the Drive contract fixture and the frontend enums (`frontend/lib/domain/sources.ts`, `lib/data/http/sources-wire.ts`). The Word/PowerPoint/Excel split (OOXML vs legacy) needs class by MIME anyway. |
| N2 | Limits (none exist today) | (a) the values in §4.5; (b) other values; (c) no limits | (a) — they protect a 3 GiB single-slot worker from ZIP bombs and huge sheets; the upload caps match the frontend text. |
| N3 | Error code for a failed non-PDF parse | (a) new `document_parse_failed`, `pdf_parse_failed` stays for PDF; (b) reuse `pdf_parse_failed`; (c) rename to one `parse_failed` for all | (a) — existing rows and clients keep their values. |
| N4 | New time limits | (a) start-to-close 120 min, 2 attempts, workflow 250 min, Drive wait 250 min, Drive wait activity 255 min (§4.4); (b) other values | (a) — one maximum-size parse (29 min with noise, FAST) plus a short queue, and the retry rule holds. |

## 10. Acceptance gates for the implementation

| Gate | Check | Threshold |
|---|---|---|
| G1 Fixture quality (CI, offline) | `extraction.pdf` through the real `file_to_markdown` | headings 9/9 with exact levels; tables 3/3, cell recall ≥ 0.95; word F1 ≥ 0.98 (llm-* blocks removed); figure placeholders = golden; 8/8 page markers. |
| G2 Spec quality (manual or nightly) | 216-page spec PDF against its source Markdown | Headings ≥ 357/358 found and at the exact level; table-cell recall ≥ 0.85 (measured 0.90; pymupdf4llm 0.79); word F1 ≥ 0.975 (measured 0.981); no empty page. |
| G3 Real corpus (owner provides about 20 files: PDFs incl. slide decks and multi-column, plus real DOCX/PPTX/XLSX) | Docling vs pymupdf4llm (PDF) and vs the source (Office) | No page that pymupdf4llm filled is empty; no lost table; heading order reviewed by a person. |
| G4 Speed | 216-page spec PDF, worker-parse sizing, median of 3 runs | ≤ 13 min on one 2-vCPU replica (measured 618 s (10.3 min), noise ±20 %). |
| G5 Memory | Peak RSS on the spec PDF | ≤ 2.5 GB per worker process with 10-page ranges (measured 1.8 GB); pod limit 3 GiB. |
| G6 Licence | `pip-licenses` allowlist in CI on each image; `pymupdf`, `pymupdf4llm`, `pymupdf-layout` absent from all images; NOTICE updated | Pass. |
| G7 Offline | worker-parse parses with network egress blocked, including HTML and Markdown with remote images | Pass (models baked, no fetch). |
| G8 Determinism | Same file twice, every format | Byte-identical Markdown. |
| G9 Graph | One re-parsed document and one legacy document in a graph build | Re-parsed: revised once; legacy: unchanged revision. |
| G10 Real container | worker-parse image, read-only root filesystem, uid 10001, `mem_limit` 3g (the 1 GB tmpfs counts against it), the full worker process; the spec PDF, one image-heavy PDF and one Office file at each N2 limit | No OOM kill; peak memory and time recorded; no apt package missing. |
| G11 Office/web quality (CI, offline) | One small fixture per format (DOCX with headings and one with a real Title style, PPTX with title placeholders, XLSX, HTML, Markdown, CSV) with known content; upload names `.md`/`.csv` with empty or odd browser types | Word recall ≥ 0.98; every table with cell recall ≥ 0.95; DOCX/HTML/MD heading levels exact; one marker per slide and per sheet, sheet name as heading; canonical types accepted. |
| G12 Security fixtures | OOXML with an external entity, a ZIP bomb with false declared sizes, an XLSX with a false dimension tag, HTML with scripts and remote images; each through the upload path and the Drive path | Refused with a fixed code, or parsed without the entity and without fetching, within the limits. |
| G13 Caps and limits | A 501-page PDF and a 501-slide PPTX; the time-limit settings | `document_too_large`, shown with its reason; execution ≥ attempts × start-to-close and Drive wait ≥ execution (a unit test). |

## Appendix A. Reproduce the measurements

The benchmark scripts ran in a throw-away environment and are not committed (one-file rule for this review). The cost figure in §5 comes from the source research cost model ($0.10 per worker-hour for 2 vCPU), applied to the measured FAST speed. The implementation plan turns gates G1–G2, G4–G5 and G11 into committed tests or benchmarks.

```python
# Docling (decided configuration, PDF)
opts = PdfPipelineOptions()
opts.do_ocr = False                                   # D2: out of scope
opts.do_table_structure = True
opts.table_structure_options = TableStructureOptions(do_cell_matching=True, mode=TableFormerMode.FAST)   # D4
opts.generate_picture_images = False                  # True only for the CLI --enrich path
opts.heading_hierarchy_options = HeadingHierarchyOptions(enabled=False)   # SpecFlow ranks headings
opts.accelerator_options = AcceleratorOptions(num_threads=2, device="cpu")
opts.artifacts_path = "/opt/docling-models"           # models baked into the image
converter = DocumentConverter(
    allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX, InputFormat.XLSX,
                     InputFormat.HTML, InputFormat.MD, InputFormat.CSV],
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)},
)
converter.convert(pdf, page_range=(a, b))             # PDF: 10-page ranges, MALLOC_ARENA_MAX=2
doc.export_to_markdown(page_no=p, escape_underscores=False, escape_html=False, compact_tables=True)
```

```python
# pymupdf4llm baseline = the production call (markdown_extractor.py), venv without RapidOCR/Tesseract
pymupdf4llm.to_markdown(pdf, page_chunks=True, write_images=True, image_format="png", dpi=170, table_output="markdown")
```

```python
# Unstructured
partition_pdf(filename=pdf, strategy="hi_res" | "fast", infer_table_structure=True, languages=["eng"])
# Title -> heading (category_depth + 1), Table.metadata.text_as_html -> GFM, Image -> placeholder,
# Header/Footer/PageNumber dropped, one block per metadata.page_number
```

Test files: the spec PDF from `pandoc specflow-specification.md -f gfm -t html5 -s` with a plain print CSS (A4, 15–18 mm margins, 10.5 pt body, bordered tables), then Chromium `page.pdf(format="A4")` through Playwright (216 pages, 1.28 MB). DOCX and PPTX from `pandoc specflow-specification.md -f gfm -o spec.docx|spec.pptx`; XLSX from the first 40 Markdown tables with openpyxl; the PPTX title check from a 3-slide python-pptx deck (layout "Title and Content").
