# Docling as the default PDF parser (research and design)

- Status: **Draft**, for owner review. After approval, this file is the input for the implementation plan.
- Date of the measurements: 2026-10-31.
- How to review: edit this file in place, or answer the open decisions in §9.

## 0. TL;DR

- **Decision:** replace pymupdf4llm (AGPL-3.0) with **Docling 2.145** (MIT code; weights Apache-2.0 and CDLA-Permissive-2.0, verified on Hugging Face) behind a small `PdfEngine` port in the kept `file_parsers` package. Docling fills the existing `PDFDocument` model and the existing renderer writes the Markdown, so the stored format does not change.
- **Quality: Docling is as good or better, but only with two SpecFlow post-processing steps** (heading levels by font size, glued-word repair; deterministic, pypdfium2, 2 s per 216 pages):
  - fixture (8 pages): headings 9/9 at the exact level, tables 3/3, word F1 0.986 (pymupdf4llm 0.969);
  - 216-page spec: headings 357/358 at the exact level (pymupdf4llm 357/358), table-cell recall **0.90 vs 0.79**, word F1 0.981 vs 0.983.
- **Speed: Docling is 4.7× slower.** The 216-page spec takes **10.3 min** on 2 vCPU (pymupdf4llm 2.2 min). More processes on the same 2 cores do not help. ⟪P4TLDR⟫ So the 5-minute target needs a **fan-out of page ranges across worker-parse replicas** (§4.4), delivered as the step after the engine switch.
- **Memory:** a whole document peaks at 4.7 GB; **10-page ranges cap it at 1.8 GB** → worker-parse at 2 vCPU / 3 GiB.
- **Unstructured (measured as asked):** rejected — `hi_res` is about 3× slower than Docling FAST (8.95 vs 2.86 s/page), flat heading levels, weak tables, a silent empty result on the fixture, and licence gaps (poppler GPL binary, unlicensed layout weights).
- **Per-deployment engine choice (GCP Document AI):** not in this scope. The port keeps it additive: one adapter + one cloud-profile key later (§4.5).
- **Open decisions:** 9, each with a default (§9). The most important: D1 long documents, D2 OCR, D3 page cap.

## 1. Scope

| | |
|---|---|
| In scope | The PDF parse path on worker-parse (`parse_document`). PyMuPDF/pymupdf4llm (AGPL-3.0) leaves every image. Docling becomes the default engine behind a small engine port. |
| Out of scope | Other formats (only `application/pdf` reaches the parser today, `api/routers/documents.py:102`). Managed engines (GCP Document AI, AWS). LLM enrichment changes. |
| Kept | The `file_parsers` package with its LLM enrichers (owner instruction). It loses only its PyMuPDF calls. |
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

Facts that constrain the design:

- PyMuPDF is used in 3 places: `markdown_extractor.py` (the production call), `markdown_extractor._png_size` (`open_pixmap`), and `math_enricher.py` (page render, clip render, `find_tables`; LLM path, CLI only). The typed wrapper is `pymupdf_typed.py`. Other references: `src/pymupdf4llm.pyi`, `validation/python/lint/ruff.toml`, `NOTICE`, and tests that assert `parser == "pymupdf4llm"`.
- pymupdf4llm 2.0 runs OCR by itself on pages it selects (RapidOCR, seen on fixture page 6). The current worker image has no Tesseract.
- The stored figure links point to PNG files in a temporary directory that is deleted after the parse. Production uses only the placeholders, not the images.
- `parser` values in the database: `pymupdf4llm` (PDF) and `drive-convert` (Drive conversion, `temporal/sources/activities.py:98`).
- A changed `revision_id` marks a graph document "revised" and rebuilds it (`graph/build/changes.py:96`).
- Compose runs worker-parse with `mem_limit: 2g` and a 1 GB `/tmp`.

## 3. Benchmarks

### 3.1 Method

- Machine: 4 vCPU, 15 GB. Every run is pinned to **2 cores** (`taskset`), the same as the worker-parse sizing in the ticket, unless the row says otherwise.
- Versions: Docling 2.145.0 (docling-core 2.105.0, docling-ibm-models 4.0.4, docling-parse 7.24.2), torch 2.15.0+cpu; pymupdf 2.0 / pymupdf4llm 2.0 (the locked production versions); unstructured 0.27.36 + unstructured-inference 1.6.17, Tesseract 5.3.4.
- Models were downloaded before the runs. Model load time (Docling ≈ 3–4 s warm) is not in "convert".
- Every engine output is rendered to the same shape: `<!-- source-page: N -->` per page, a figure placeholder per picture, GFM tables.
- **Quality input 1:** `backend/tests/data/specs/extraction.pdf` (8 pages) against `extraction.md`. The golden file is not neutral: its pages 1–5 are pymupdf4llm output, and pages 7–8 are LLM LaTeX. So the evaluator removes all `llm-*` blocks (descriptions, Mermaid) and all math spans before it compares words. Scores on pages 1–5 favour pymupdf4llm by construction.
- **Quality input 2 and speed input:** the 216-page A4 PDF rendered from `specflow-specification.md` with pandoc + Chromium. The source Markdown is exact ground truth for this PDF. Your own rendering (`specflow-specification-8.pdf`, 10.5 MB) did not reach this session (D9). Mine has 216 pages and 1.28 MB; a file with more raster images can be slower.
- Metrics: word recall / precision / F1 (multiset of words), word-order similarity (fixture only), headings found, exact heading level and pairwise heading order, GFM tables and table-cell recall, figure placeholders per page.

### 3.2 Quality: fixture (`extraction.pdf`, 8 pages)

| Engine and configuration | s/page | Word F1 | Order | Headings found | Level exact | Pair order | Tables | Cell recall | Figures (golden 0,1,0,0,1,1,0,0) |
|---|---|---|---|---|---|---|---|---|---|
| pymupdf4llm (production call) | 0.94 | 0.969 | 0.967 | 9/9 | 9/9 | 36/36 | 3/3 | 1.00 | 1,1,0,0,1,1,**7**,0 |
| Docling, tables ACCURATE | 3.15 | 0.985 | 0.966 | 9/9 | 1/9 (flat `##`) | 6/36 | 3/3 | 1.00 | exact |
| Docling + built-in `HeadingHierarchyOptions` | 3.26 | 0.985 | 0.966 | 9/9 | 0/9 | 13/36 | 3/3 | 1.00 | exact |
| Docling, TableFormer **V2** | 3.39 | 0.963 | 0.951 | 9/9 | 0/9 | – | 3/3 | **0.09** | exact |
| Docling, **pypdfium2** backend | 3.37 | 0.993 | 0.971 | 9/9 | 4/9 | – | **2/3** | **0.06** | exact |
| **Docling ACCURATE + SpecFlow post-processing** | 3.15 + 0.01 | **0.986** | 0.968 | 9/9 | **9/9** | **36/36** | 3/3 | 1.00 | exact |
| **Docling FAST + SpecFlow post-processing** | 2.71 + 0.01 | **0.986** | 0.976 | 9/9 | **9/9** | **36/36** | 3/3 | 1.00 | exact |
| Unstructured `hi_res` (+ table structure) | 10.6 | 0.969 | 0.968 | 8/9 | 1/9 | 6/28 | 3/3 | 0.92 | exact |
| Unstructured `fast` | 0.5 | – | – | – | – | – | – | – | **0 elements, no error** |

Notes:

- pymupdf4llm figure count on page 7: the 7 display equations become images. The golden file has LaTeX there (from the LLM path).
- Docling defects that the SpecFlow post-processing fixes: flat heading levels, and glued words (`justinzhao`, `judgesmove`, `fromthe`; 25 repairs on the fixture).
- Docling defects that stay: (a) it joins a paragraph that continues across a page break to the previous page, so that text is cited one page early (seen once: page 3 → 4); (b) `ω = 0.69` becomes `ω = 0▷69` (font glyph map; pymupdf4llm gives `0 _._ 69`, also wrong); (c) running headers and footers are dropped (better for the graph; the golden file keeps them).
- Docling OCR on (RapidOCR, auto) and picture images on: no quality change on this born-digital fixture.

### 3.3 Quality: spec PDF (216 pages, ground truth = source Markdown)

| Engine and configuration | Word F1 | Headings found | Level exact | False headings | Tables (source 228) | Cell recall |
|---|---|---|---|---|---|---|
| pymupdf4llm | 0.983 | 358/358 | 357/358 | 58 | 301 | 0.79 |
| Docling ACCURATE, raw | 0.981 | 357/358 | 44/358 | 45 | 297 | 0.90 |
| **Docling ACCURATE + post-processing** | 0.981 | 357/358 | **357/358** | 45 | 297 | **0.90** |
| **Docling FAST (10-page ranges) + post-processing** | 0.981 | 357/358 | **357/358** | 45 | 297 | **0.90** (0.897) |
| Unstructured `fast` | 0.959 | 299/358 | 7/358 | 3,404 | **0** | 0.00 |

Unstructured `hi_res` was too slow for 216 pages (≈ 32 min), so it ran on pages 1–30. Same 30-page cut for all engines (ground truth = the source prefix with the same word count):

| Engine (pages 1–30) | Word F1 | Headings found | Level exact | False headings | Tables (source 27) | Cell recall |
|---|---|---|---|---|---|---|
| pymupdf4llm | 0.918 | 46/46 | 45/46 | 8 | 39 | 0.84 |
| Docling ACCURATE + post-processing | 0.918 | 45/46 | 45/46 | 8 | 35 | **0.95** |
| Unstructured `hi_res` | 0.797 | 42/46 | 7/46 | 13 | 35 | 0.44 |

Notes: more tables than in the source = a table cut by a page break counts once per page (all engines). Word F1 on the cut is below 1 for every engine because the cut is approximate. Docling writes `<` and `>` as `&lt;`/`&gt;` (`escape_html` default); pymupdf4llm writes them raw and has ligature errors (`efciencyfi`).

### 3.4 Speed and memory: spec PDF (216 pages)

| Run | Cores | Wall time | s/page | Peak RSS | Meets 5 min? |
|---|---|---|---|---|---|
| pymupdf4llm (production call) | 2 | **131 s** (2.2 min) | 0.61 | 0.5 GB | yes |
| Docling ACCURATE, whole document, 1 process | 2 | **907 s** (15.1 min) | 4.20 | **4.7 GB** | no |
| Docling FAST, 10-page ranges, 1 process, `MALLOC_ARENA_MAX=2` | 2 | **618 s** (10.3 min) | 2.86 | **1.8 GB** (flat after the 4th range) | no |
| Docling FAST, split in 2 × 108 pages, 2 processes × 1 thread | 2 | ⟪P2T⟫ | ⟪P2S⟫ | ⟪P2M⟫ | ⟪P2Y⟫ |
| Docling FAST, split in 4 × 54 pages, 4 processes × 1 thread | 4 | ⟪P4T⟫ | ⟪P4S⟫ | ⟪P4M⟫ | ⟪P4Y⟫ |
| Unstructured `hi_res` (pages 1–30, extrapolated) | 2 | ≈ 1,930 s (32 min) | 8.95 | 2.0 GB | no |
| Unstructured `fast` (no tables, see §3.3) | 2 | 48 s | 0.22 | 0.6 GB | yes |

- Docling model load (≈ 4 s per process) is not in the wall time; a worker process loads the models once.
- Run-to-run noise: two identical 8-page runs differed by about 20 %. Some runs shared the machine with another pinned run on the other cores (memory bandwidth); read the numbers as ±20 %.
- In a split run, each process converts its whole range in one call, so its memory grows with the range (about 3 GB for 108 pages). A production split still converts in 10-page ranges inside each process.

### 3.5 Findings that drive the design

1. **Quality:** Docling + two SpecFlow post-processing steps is equal to or better than pymupdf4llm on every structural metric; tables are better (spec cell recall 0.90 vs 0.79). Without the post-processing, heading levels are flat. So the post-processing is required, not optional.
2. **Docling's own switches do not fix the two defects:** `HeadingHierarchyOptions` orders levels wrongly, the pypdfium2 backend breaks tables, TableFormer V2 empties cells.
3. **FAST tables = ACCURATE quality**, 32 % faster.
4. **Speed:** Docling needs 2.86 s/page on 2 vCPU, 4.7× pymupdf4llm. The 216-page spec takes about 10 min on one worker-parse. The 5-minute target needs at least 4 cores working on one document: ⟪P4T⟫.
5. **Splitting on the same cores gains nothing** (584 s vs 618 s). Page-range boundaries are harmless (213/216 pages identical).
6. **Memory:** whole-document conversion peaks at 4.7 GB; 10-page ranges stay flat at 1.8 GB. Ranges are mandatory; 3 GiB per pod is enough.
7. **OCR:** pymupdf4llm OCRs by itself today; Docling OCR-auto costs +50 % even on a born-digital file (it OCRs figure bitmaps).
8. **Unstructured** is slower than Docling and worse on headings and tables, and adds licence gaps (§5).
9. **Image:** the minimal `docling-slim` install is 1.2 GB and has no OpenCV, RapidOCR, onnxruntime or Office libraries. ⟪SLIM⟫

## 4. Decision

### 4.1 Shape: one port, one engine, SpecFlow owns the structure

```
parse_document activity (contract unchanged: original in storage -> documents.markdown + documents.parser)
 └─ pdf_file_to_markdown(path) -> ParsedMarkdown(markdown, parser=<engine id>)
     └─ PDFParser(engine)                                     file_parsers/pdf/parser.py (kept)
         ├─ engine.extract(pdf, page ranges) -> PDFDocument    port `PdfEngine`   (file_parsers/pdf/engines/)
         │     DoclingEngine  (default, the only engine that ships)
         ├─ PdfStructure.apply(PDFDocument, pdf) -> PDFDocument   SpecFlow post-processing, pypdfium2 only
         │     heading levels by font size · glued-word repair · figure placeholder ids
         ├─ [CLI only] enrich(): figure / Mermaid / math LLM enrichers   (kept, PyMuPDF calls -> pypdfium2)
         └─ PDFRenderer (unchanged) -> "<!-- source-page: N -->" Markdown
```

- `PdfEngine` (decision granularity): `engine_id` (string, stored in `documents.parser`) and `extract(pdf, page_range, images: bool) -> PDFDocument`. The engine returns per-page Markdown, figures, and table boxes. It does not rank headings and does not render the document.
- `PDFDocument` stays the intermediate model. It is already the input of `PDFRenderer` and of the LLM enrichers. So the stored Markdown keeps its shape (page markers, `![pN.figK](images/…)`, GFM tables), and a future engine (for example GCP Document AI) only has to produce `PDFDocument`.
- Engine id: `docling@<docling version>+sf<post-processing version>`, for example `docling@2.145.0+sf1`. It changes when Docling or the SpecFlow post-processing changes, and only then.

### 4.2 Docling configuration (pinned, CPU, offline)

| Option | Value | Why |
|---|---|---|
| Package | `docling-slim[convert-core,format-pdf,models-local]==2.145.0` (+ `feat-ocr-rapidocr-onnx` only if D2 = b) | `docling` = `docling-slim[standard]`, which also pulls python-docx/pptx, openpyxl, mail-parser, rapidocr and more. Verify the final tree in gate G6. |
| Backend | docling-parse (default) | pypdfium2 backend breaks tables (§3.2). |
| Tables | TableFormer v1, `do_cell_matching=True`, mode FAST | §3.2–3.4. V2 is not ready. |
| OCR | off (pymupdf4llm OCRs pages by itself today) | Open decision D2. |
| Picture images | off in production; on for the CLI `--enrich` path only | Production keeps only placeholders (§2). |
| Heading hierarchy (Docling) | off | SpecFlow ranks headings (§4.3). |
| Markdown export | per page (`page_no`), `escape_underscores=False`, `escape_html=False` | Same text as today: pymupdf4llm writes `_` and `<`/`>` raw. |
| Threads | `num_threads` = pod vCPU (2) | |
| Models | baked into the image, `artifacts_path`, `HF_HUB_OFFLINE=1`, revisions pinned | No network at run time; reproducible. |
| Forbidden | VLM pipelines, picture description, chart, code and formula enrichment, remote services, external plugins | Licence and determinism. A unit test checks the built options. |
| Unit of work | page ranges of 10 pages, `MALLOC_ARENA_MAX=2` | Memory (§3.4). |

### 4.3 SpecFlow post-processing (`PdfStructure`, deterministic, pypdfium2 only)

1. **Heading levels.** Find each heading's text in the pdfium text layer of its page. Read the effective font size (nominal size × text-matrix scale; the nominal size alone is 1.0 in many PDFs). Rank the distinct sizes (0.5 pt buckets) across the whole document: largest → `#`, next → `##`, up to `######`. If the text is not found, use the numbering depth, else keep `##`. Fixture: 9/9 exact, 36/36 pairs. Spec: 357/358 exact (pymupdf4llm 357/358).
2. **Glued words.** A prose token that is not in the page's pdfium word list, but equals 2–3 consecutive pdfium words, gets the spaced form. Tables and code are not touched. Fixture: 25 repairs, word recall 0.986 → 0.992.
3. **Figures.** Keep the current placeholder shape `![pN.figK](images/…)` so new and old Markdown look the same.

Cost: 0.08 s for 8 pages; 2.2 s for 216 pages. Ranking needs every page, so it runs once after the last page range.

### 4.4 Long documents: the 5-minute target

The numbers (§3.4): on 2 vCPU, every shape takes 584–618 s for the 216-page spec (2.7–2.9 s/page). Two processes on the same 2 cores gain only about 5 %, inside the noise. So only **more cores** reduce the wall time, and they must come from more worker-parse replicas or a bigger pod. Four cores: ⟪P4T⟫.

| Option | Shape | 216-page spec | Cost and risk |
|---|---|---|---|
| S. Sequential ranges | One `parse_document` activity converts 10-page ranges one after the other. | ≈ 10 min on 2 vCPU | Smallest change. 1,000 pages ≈ 48 min, above the 45-min activity timeout, so the cap must drop to about 500 pages. |
| **F. Fan-out across replicas** | The workflow splits the document into ranges of 20 pages and runs them as activities on the `parse` queue, at most L = 4 at once per document; an assemble activity joins them. | ⟪FANOUT⟫ with 4 replicas | Workflow change (new workflow version), range results in object storage, one more activity. Scales out; a failure retries one range only. |
| P. Process pool in a bigger pod | One activity starts K processes in a pod with 2K vCPU. | Same as F for the same cores | Vertical: K × 1.8 GB in one pod, idle cost, no per-range retry. |

**Decision (default for D1): F, delivered in two steps.** Step 3 in §6 ships S (the engine switch alone, cap 500 pages). The next step ships F to meet the 5-minute target. This keeps the behaviour change (engine) apart from the structure change (fan-out).

Shape of F (decision granularity):

```
ParseDocumentWorkflow (new version; in-flight runs replay on the old code path, replay test exists)
 1. plan_parse        -> page count (pypdfium2), ranges of 20 pages
 2. parse_range × N   -> queue "parse", ≤ L=4 in flight per document
      download original -> convert the range in 10-page steps -> PDFDocument JSON + heading font sizes
      -> object storage: documents/<project>/<document>/parse/<run>/<first>-<last>.json   (not workflow history: payload limit)
 3. assemble_parse    -> read all ranges -> PdfStructure (global heading ranks) -> PDFRenderer
      -> documents.markdown + documents.parser -> delete the range objects (bucket lifecycle rule as a safety net)
```

- A document of 20 pages or fewer is one range: the same path, plus one small assemble step.
- Each worker process loads the models once and keeps the converter; a range activity pays no model load.
- Heartbeat after each 10-page step (about 30 s); a cancel waits at most one step.
- Range boundaries change only the paragraphs that continue across them: 213/216 pages were identical between 10-page and 108-page ranges, and the 3 different pages are more correct (the text stays on its own page).
- Replicas scale on the `parse` queue backlog. L bounds one large document, so it cannot take every replica.

### 4.5 Engine choice per deployment (GCP Document AI): later, through the port

- Now: one engine in code. No config key (YAGNI: a key with one allowed value is surface without choice).
- Later (follow-up story, when a deployment needs a managed engine): add `services.document_parsing` to the cloud profile, with the same rules as `services.object_storage` (`cloud_profile.py`: unknown key or value stops the start-up, env var wins), and add one adapter that produces `PDFDocument`:

```yaml
services:
  document_parsing:
    provider: docling | gcp_document_ai          # default docling on every cloud; local is always docling
    gcp_document_ai: { processor_version: pretrained-layout-parser-v1.0-2024-06-03, location: eu }
```

- Rules for that later adapter (from the source research): store the raw provider JSON next to the Markdown, so a renderer change does not bill again; map `heading-1..5` to levels; the engine id carries the processor version; the graph sees a new engine id as a revision.

### 4.6 The `file_parsers` package stays

- `markdown_extractor.py` (pymupdf4llm) is replaced by the Docling engine. `PDFParser`, `PDFRenderer`, the schema, the prompts and the three LLM enrichers stay.
- `pymupdf_typed.py` becomes `pdfium_typed.py` with the same small surface: open a PDF, render a page or a clip at a DPI to PNG, read a PNG size. `find_tables` (math enricher) is replaced by the table boxes that the Docling engine puts into `PDFPageContent`.
- The CLI (`python -m file_parsers.pdf.parser --enrich`) keeps working; it asks the engine for picture images.
- The transcript parser is not touched.

### 4.7 Graph revision rule

`revision_id = content_hash` when `parser` is NULL, `pymupdf4llm` or `drive-convert`; else `revision_id = content_hash + ":" + parser`.

- Why not `content_hash:parser` for every row (ticket): every existing document would get a new revision on deploy, so every project graph would rebuild at once (`graph/build/changes.py:96`).
- Why not the hash of the Markdown: same mass rebuild on deploy.
- Effect: only a document that is parsed again with a new engine id is revised, once.

### 4.8 Images, sizing, operations

- worker-parse gets its own image with the `parse` extra (ticket). The api and worker-agents images never import the parser (the queue registration is imported lazily, `temporal/registry.py`), so they need no code change.
- worker-parse: 2 vCPU, **3 GiB** (`mem_limit`), `/tmp` 1 GB, models in the image (Docling cache measured here: 506 MB, including the unused TableFormer V2), image about +1.5 GB (ticket).
- Logs: one `pdf.stage.done` per page range (pages, duration) through `logger.logger`; the heartbeat carries pages done / pages total.
- Determinism: two runs gave byte-identical Markdown (fixture). Gate G8 keeps it.

## 5. Rejected alternatives

| Alternative | Why it lost |
|---|---|
| Keep pymupdf4llm, buy the Artifex licence | Quote-only and still proprietary terms (ticket). No AGPL exception (ticket). |
| **Unstructured OSS** (`unstructured[pdf]`, measured here) | Speed: `hi_res` is 8.9–10.6 s/page, about 3× Docling. Quality: heading levels are flat (1/9 fixture, 7/46 spec sample); word recall 0.70 and table-cell recall 0.44 on the 30-page spec sample (pymupdf4llm 0.90 / 0.84 on the same cut). `fast` has no tables (0 of 228) and 3,404 false headings on the spec, and it **returns 0 elements without an error** on the fixture, while pdfminer reads 20k characters from the same file. Licence: `hi_res` needs the poppler binary (GPL) through pdf2image and Tesseract in the image; pikepdf is MPL-2.0; the `unstructuredio/yolo_x_layout` weights have no licence metadata on Hugging Face (UNKNOWN). The hosted Unstructured API is external SaaS (the data leaves our account). |
| Docling built-in `HeadingHierarchyOptions` | Wrong order on the fixture: "3.1" ranks above "1 Introduction" (13/36 heading pairs correct). |
| Docling `pypdfium2` backend | Fixes glued words, but breaks table cell matching (2/3 tables, cell recall 0.06). |
| Docling TableFormer V2 | Empty cells on the fixture (cell recall 0.09). So the "drop OpenCV with TableFormer V2" path from the ticket is not ready. |
| Docling VLM pipelines (granite-docling, picture description, chart/code/formula enrichment) | GPU-oriented, slower on CPU, model output is not deterministic, and some presets carry RED weight licences (Chandra, OpenRAIL; source research §1). |
| GCP Document AI Layout Parser now | Owner: probably too much for this scope. Also: $10 per 1,000 pages vs about $0.04 per 1,000 pages of worker CPU (source research, cost model), the data leaves the cluster, the only pinned GA version is `pretrained-layout-parser-v1.0-2024-06-03`, and a residency review is necessary. The port in §4.1 keeps it a later, additive change (§4.5). |
| Marker/Surya, MinerU, opendataloader-pdf, PP-StructureV3, pdf_oxide, markitdown, xberg, Textract | Rejected in licence step 2 (ticket): weight licences, AGPL weights, JVM/MPL, too heavy, no layout, no heading levels. |

## 6. Cutover and rollback

Branch by abstraction. Behaviour change (the engine) and the structural change (the port) ship in separate steps, so that parity is provable.

| Step | Change | Images | Rollback |
|---|---|---|---|
| 1. Interim (ticket) | Move `pymupdf`/`pymupdf4llm` to an optional extra that only the worker-parse image installs. | api and worker-agents lose PyMuPDF (−186 MB each, ticket) and can deploy. worker-parse is unchanged. | Redeploy the previous tags. |
| 2. Port | Add the `PdfEngine` port and `PdfStructure` post-processing. Wrap the current extractor as the first engine. Output must stay byte-identical (existing tests). | No change. | Revert the commit. |
| 3. Engine | Add the Docling engine, the pypdfium2 wrapper and the graph revision rule. Pass gates G1–G9 (§10) on staging with real documents. | New worker-parse image: Docling, no PyMuPDF. PyMuPDF leaves `uv.lock`. | Redeploy the previous worker-parse tag (internal only: an image that contains PyMuPDF must not be distributed). |

- No schema change and no data migration. Documents parsed by Docling before a rollback keep their Markdown; their `parser` value tells which engine made it. So there is no point of no return on data.
- Existing documents keep their pymupdf4llm Markdown. That Markdown is program output in Postgres; no AGPL code runs to serve it. A re-parse of all PDFs is open decision D5.
- Coexistence: a project can hold Markdown from both engines. The graph treats a re-parse as a revision (§4.7). Nothing else reads `parser`.

## 7. Blast radius

| Area | Files | Change |
|---|---|---|
| Dependencies | `backend/pyproject.toml`, `backend/uv.lock` | `docling-slim[...]` in a `parse` extra; torch from the PyTorch **CPU** index (the default index pulls CUDA wheels with proprietary terms); remove `pymupdf*`; deptry map. |
| Images | `backend/worker.Dockerfile` (+ a worker-parse variant), `docker-compose.yml` | Separate worker-parse image with the `parse` extra; models baked in at build (`artifacts_path`), `HF_HUB_OFFLINE=1`, `MALLOC_ARENA_MAX=2`; `mem_limit` 2g → 3g. |
| Parser | `src/file_parsers/pdf/` | New `engines/docling.py` (port + engine), `structure.py` (post-processing); `markdown_extractor.py` replaced; `pymupdf_typed.py` → `pdfium_typed.py`; `math_enricher.py` uses pypdfium2 renders and Docling table boxes; `to_markdown.py` returns the engine id; `schema.py` gains optional table boxes. |
| Activity | `src/temporal/documents/activities.py` | Heartbeat carries pages done / pages total. S: no other change. F: `temporal/documents/workflows.py` (new workflow version; plan, range and assemble activities), `storage/keys.py` (range result keys), replay test. |
| Graph | `src/api/graph_build/corpus.py`, `src/db/sources.py` (`GraphCorpusRow` gains `parser`) | Revision rule §4.7. |
| Lint and stubs | `src/pymupdf4llm.pyi` (delete), `validation/python/lint/ruff.toml` | Remove the pymupdf4llm entries. |
| Tests | `tests/test_specflow_parsing.py`, `tests/test_pdf_parser_logging.py`, new quality test | Expect the Docling engine id; structural quality test on the fixture (D6). Tests that store the literal `"pymupdf4llm"` as sample data (db, drive, cleanup) can stay: it is a legal legacy value. |
| Licence files | `NOTICE` | Remove PyMuPDF; add Docling, docling-parse, docling-core, docling-ibm-models (MIT), model weights (Apache-2.0, CDLA-Permissive-2.0), torch (BSD-3, bundled `libgomp`), OpenCV (Apache-2.0, bundled FFmpeg LGPL), tqdm and certifi (MPL-2.0). |
| Docs | `docs/configuration.md` | Only if D7 adds the config key now. |

## 8. Risks and what would invalidate this decision

| Risk | Effect | Mitigation |
|---|---|---|
| Docling is 3–5× slower than pymupdf4llm | Longer wait for a parsed document; queue latency under load | §4.4 (page ranges, fan-out), page cap, scale worker-parse replicas on queue backlog. |
| Memory: whole-document conversion peaks at 4.4 GB | OOM kill above `mem_limit` | Page ranges of 10 + `MALLOC_ARENA_MAX=2` (§3.4). Gate G5. |
| Scanned PDFs: no OCR by default | Empty pages where pymupdf4llm OCR found text | Open decision D2. Detect pages without a text layer and log them. |
| The font-size heading rule fails where headings use body size (bold only) | Flat or wrong levels | Fallback order: font size → numbering depth (`1`, `1.2`, `1.2.3`) → `##`. Measure on the corpus (gate G3). |
| Glued-word repair changes a real compound word | Rare false split | Repair only tokens that are absent from the pdfium word list and equal 2–3 consecutive pdfium words on the same page; never in tables or code. |
| Docling cross-page paragraph join | Some text is cited one page early | Accepted (minor). Page-range boundaries cut the join anyway. |
| Docling release churn (2.x ships often) | Output drift | Pin the exact version and the model revisions; the engine id holds both; an upgrade is a normal change with gates G1–G2. |
| Torch wheels | CUDA wheels from the default index have proprietary terms; large image | Pin the PyTorch CPU index in `pyproject.toml`; licence gate in CI. |
| Small evaluation set (1 fixture + 1 rendered spec, both born-digital English) | Hidden failure modes | Gate G3: a real corpus before the default switch. |

The decision is invalid if: the corpus gate G3 shows a systematic loss (scans, slide decks, multi-column, forms); a customer or policy requires a managed engine (then the GCP adapter becomes in scope); Docling or its weights change licence; or worker-parse cannot get 3 GiB.

## 9. Open decisions for the owner

Each decision has a default. The default applies if you approve this file without a change.

| # | Decision | Options | Default |
|---|---|---|---|
| D1 | Long-document strategy | (S) sequential 10-page ranges in one activity; (F) fan-out of 20-page ranges across worker-parse replicas; (P) process pool in a bigger pod (§4.4) | S in the engine-switch step, then F as the next step (§4.4). |
| D2 | OCR for pages without a text layer | (a) off, log such pages; (b) Docling RapidOCR only on pages without a text layer; (c) OCR auto everywhere (+50 % time, §3.2) | (b) — keeps today's behaviour for scans (pymupdf4llm OCRs by itself); cost only on scanned pages. Needs a scanned sample in G3. |
| D3 | Page cap | (a) 1,000 pages (ticket); (b) 500; (c) no cap | ⟪D3DEF⟫ |
| D4 | Table mode | (a) FAST; (b) ACCURATE | (a) FAST — the same quality as ACCURATE on both inputs (spec cell recall 0.897 vs 0.900, 196/216 pages byte-identical; fixture equal) and 32 % faster. |
| D5 | Existing PDF documents | (a) keep their pymupdf4llm Markdown; (b) re-parse all PDFs once after the switch | (a) — no licence need, no mass graph rebuild; re-parse stays possible per document. |
| D6 | Regression test for quality | (a) structural thresholds on the fixture (G1); (b) re-baseline `extraction.md` with Docling output and compare exactly | (a) — the golden file is `REVIEW_REQUIRED` and mixes engine output with LLM text; exact Markdown would break on every Docling upgrade. |
| D7 | Engine config key | (a) later, with the second engine; (b) now, one allowed value | (a) (§4.5). |
| D8 | Images | (a) separate worker-parse image; (b) one worker image with Docling for both workers | (a) — worker-agents stays 1.5 GB smaller (ticket). |
| D9 | Your exact speed PDF | Your `specflow-specification-8.pdf` (10.5 MB) did not reach this session (upload limit; the Drive connector cannot return 10 MB). I used my own rendering of the same Markdown. | Share the Drive link as "Anyone with the link" and I re-run §3.4 on your file, or accept the rendering. |

## 10. Acceptance gates for the implementation

The implementation plan turns these into tests or checklist items. The default switch (step 3 in §6) needs all of them.

| Gate | Check | Threshold |
|---|---|---|
| G1 Fixture quality (CI, offline) | `extraction.pdf` through the real `pdf_file_to_markdown` | headings 9/9 with exact levels; tables 3/3, cell recall ≥ 0.95; word F1 ≥ 0.98 (llm-* blocks removed); figure placeholders = golden; 8/8 page markers. |
| G2 Spec quality (manual or nightly) | 216-page spec PDF against its source Markdown | Headings ≥ 357/358 found and at the exact level; table-cell recall ≥ 0.85 (measured 0.90; pymupdf4llm 0.79); word F1 ≥ 0.98; no empty page. |
| G3 Real corpus (owner provides about 20 PDFs: scans, slide decks, multi-column, forms) | Docling vs pymupdf4llm side by side | No page that pymupdf4llm filled is empty; no lost table; heading order reviewed by a person. |
| G4 Speed | 216-page spec PDF on worker-parse sizing | ⟪G4⟫ |
| G5 Memory | Peak RSS on the spec PDF | ≤ 2.5 GB per worker process with 10-page ranges (measured 1.8 GB); pod limit 3 GiB. |
| G6 Licence | `pip-licenses` allowlist in CI on each image; `pip show pymupdf` fails in all three images; NOTICE updated | Pass. |
| G7 Offline | worker-parse parses with network egress blocked | Pass (models baked). |
| G8 Determinism | Same PDF twice | Byte-identical Markdown. |
| G9 Graph | One re-parsed document and one legacy document in a graph build | Re-parsed: revised once; legacy: unchanged revision. |

## Appendix A. Reproduce the measurements

The benchmark scripts were run in a throw-away environment and are not committed (one-file rule for this review). The implementation plan turns gates G1–G2 and G4–G5 into a committed benchmark (§10). The settings that matter:

```python
# Docling (decided configuration)
opts = PdfPipelineOptions()
opts.do_ocr = False                                   # see open decision D2
opts.do_table_structure = True
opts.table_structure_options = TableStructureOptions(do_cell_matching=True, mode=TableFormerMode.FAST)
opts.generate_picture_images = False                  # True only for the CLI --enrich path
opts.heading_hierarchy_options = HeadingHierarchyOptions(enabled=False)   # SpecFlow ranks headings
opts.accelerator_options = AcceleratorOptions(num_threads=2, device="cpu")
opts.artifacts_path = "/opt/docling-models"           # models baked into the image
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
converter.convert(pdf, page_range=(a, b))             # 10-page ranges, MALLOC_ARENA_MAX=2
doc.export_to_markdown(page_no=p, escape_underscores=False)   # one block per page
```

```python
# pymupdf4llm baseline = the production call (markdown_extractor.py)
pymupdf4llm.to_markdown(pdf, page_chunks=True, write_images=True, image_format="png", dpi=170, table_output="markdown")
```

```python
# Unstructured
partition_pdf(filename=pdf, strategy="hi_res" | "fast", infer_table_structure=True, languages=["eng"])
# Title -> heading (category_depth + 1), Table.metadata.text_as_html -> GFM, Image -> placeholder,
# Header/Footer/PageNumber dropped, one block per metadata.page_number
```

Spec PDF rendering: `pandoc specflow-specification.md -f gfm -t html5 -s` with a plain print CSS (A4, 15–18 mm margins, 10.5 pt body, bordered tables), then Chromium `page.pdf(format="A4")` through Playwright. Result: 216 pages, 1.28 MB.
