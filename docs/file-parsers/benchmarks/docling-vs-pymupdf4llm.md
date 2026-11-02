---
type: Explainer
title: "Docling, pymupdf4llm and Unstructured benchmark"
description: "Quality, speed and memory of Docling, pymupdf4llm and Unstructured on PDF, and of Docling on Office and web formats"
status: stable
tags: [file-parsers]
---
# docling-vs-pymupdf4llm.md: quality, speed and memory of the candidate engines

Measured on 2026-10-31 and 2026-11-01, before the implementation, as the evidence of
[decisions/docling-replaces-pymupdf.md](../decisions/docling-replaces-pymupdf.md). The measurements of the
shipped parser are in [acceptance-gates.md](../acceptance-gates.md#status).

## Method

- Machine: cloud VM, Intel Xeon at 2.10 GHz (exact model not exposed), 4 vCPU, 15 GB. Every run is pinned to
  2 cores (`taskset`), the worker-parse sizing, unless the row says otherwise.
- Versions: Docling 2.145.0 (docling-core 2.105.0, docling-ibm-models 4.0.4, docling-parse 7.24.2), torch
  2.15.0+cpu; pymupdf 2.0 and pymupdf4llm 2.0 (the then locked production versions, in a separate environment
  without any OCR engine, as in production); unstructured 0.27.36 with unstructured-inference 1.6.17 and
  Tesseract 5.3.4.
- Models were downloaded before the runs. Model load time (Docling about 4 s per process) is not in the
  conversion time.
- Every engine output is rendered to the same shape: `<!-- source-page: N -->` per page, a figure placeholder
  per picture, GFM tables.
- Quality input 1: `backend/tests/data/specs/extraction.pdf` (8 pages) against the golden file. The golden file
  is not neutral: its pages 1–5 are pymupdf4llm output and pages 7–8 are LLM LaTeX, so the evaluator removes
  all `llm-*` blocks and math spans before it compares words. Scores on pages 1–5 favour pymupdf4llm by
  construction.
- Quality input 2 and speed input: a 216-page A4 PDF rendered from `specflow-specification.md` with pandoc and
  Chromium (accepted by the owner, D9). The source Markdown is exact ground truth.
- Quality input 3 (Office and web): the same Markdown converted to DOCX and PPTX by pandoc, to XLSX (the first
  40 tables, one sheet each) by openpyxl, plus the pandoc HTML and the Markdown itself; a 3-slide PPTX with real
  title placeholders (python-pptx); a small CSV.
- Metrics: word recall, precision and F1 (multiset of words), word-order similarity (fixture only), headings
  found, exact heading level and pairwise heading order, GFM tables and table-cell recall, figure placeholders
  per page.

## PDF quality on the fixture

| Engine and configuration | s/page | Word F1 | Order | Headings found | Level exact | Pair order | Tables | Cell recall | Figures (golden 0,1,0,0,1,1,0,0) |
|---|---|---|---|---|---|---|---|---|---|
| pymupdf4llm (production call) | 0.45 | 0.975 | 0.973 | 9/9 | 9/9 | 36/36 | 3/3 | 1.00 | 1,1,0,0,1,1,**7**,0 |
| Docling, tables ACCURATE | 3.15 | 0.985 | 0.966 | 9/9 | 1/9 (flat `##`) | 6/36 | 3/3 | 1.00 | exact |
| Docling and built-in `HeadingHierarchyOptions` | 3.26 | 0.985 | 0.966 | 9/9 | 0/9 | 13/36 | 3/3 | 1.00 | exact |
| Docling, TableFormer **V2** | 3.39 | 0.963 | 0.951 | 9/9 | 0/9 | 13/36 | 3/3 | **0.09** | exact |
| Docling, **pypdfium2** backend | 3.37 | 0.993 | 0.971 | 9/9 | 4/9 | 12/36 | **2/3** | **0.06** | exact |
| **Docling ACCURATE and the structure pass** | 3.15 + 0.01 | **0.986** | 0.968 | 9/9 | **9/9** | **36/36** | 3/3 | 1.00 | exact |
| Docling FAST and the structure pass | 2.71 + 0.01 | **0.986** | 0.976 | 9/9 | **9/9** | **36/36** | 3/3 | 1.00 | exact |
| Unstructured `hi_res` (with table structure) | 10.6 | 0.969 | 0.968 | 8/9 | 1/9 | 6/28 | 3/3 | 0.92 | exact |
| Unstructured `fast` | 0.34 | – | – | – | – | – | – | – | **0 elements, no error** |

- pymupdf4llm on page 7: the 7 display equations become images; the golden file has LaTeX there (from the LLM
  path).
- The structure pass of that time fixed flat heading levels and glued words (`justinzhao`, `judgesmove`,
  `fromthe`; 25 repairs on the fixture). Its current rules are in [pdf.md](../pdf.md#structure-pass).
- The defects that stay are listed in [pdf.md](../pdf.md#known-defects); pymupdf4llm reads the broken glyph as
  `0 _._ 69`, also wrong.
- Docling with OCR on auto: the same word scores, order similarity 0.966 to 0.945, 50 % more time. Picture
  images on: no change, 1 % more time.

## PDF quality on the spec PDF

| Engine and configuration | Word F1 | Headings found | Level exact | False headings | Tables (source 228) | Cell recall |
|---|---|---|---|---|---|---|
| pymupdf4llm | 0.983 | 358/358 | 357/358 | 58 | 301 | 0.79 |
| Docling ACCURATE, raw | 0.981 | 357/358 | 44/358 | 45 | 297 | 0.90 |
| **Docling ACCURATE and the structure pass** | 0.981 | 357/358 | **357/358** | 45 | 297 | **0.90** |
| Docling FAST (10-page ranges) and the structure pass | 0.981 | 357/358 | **357/358** | 45 | 297 | **0.90** (0.897) |
| Unstructured `fast` | 0.959 | 299/358 | 7/358 | 3,404 | **0** | 0.00 |

Unstructured `hi_res` was too slow for 216 pages (about 32 min), so it ran on pages 1–30. Same 30-page cut for
all engines (ground truth: the source prefix up to word 8,723, the word count of pymupdf4llm's pages 1–30):

| Engine (pages 1–30) | Word F1 | Headings found | Level exact | False headings | Tables (source 27) | Cell recall |
|---|---|---|---|---|---|---|
| pymupdf4llm | 0.918 | 46/46 | 45/46 | 8 | 39 | 0.84 |
| Docling ACCURATE and the structure pass | 0.918 | 45/46 | 45/46 | 8 | 35 | **0.95** |
| Unstructured `hi_res` | 0.797 | 42/46 | 7/46 | 13 | 35 | 0.44 |

- More tables than in the source: a table cut by a page break counts once per page (all engines).
- Word F1 on the cut is below 1 for every engine because the cut is approximate. Unstructured `hi_res` word
  recall on the cut: 0.70 (pymupdf4llm 0.90).
- Unstructured heading levels are flat partly because of the benchmark adapter (`Title` mapped to
  `category_depth + 1`, no font-size ranking), so tables and speed decided.
- Docling writes `<` and `>` as `&lt;`/`&gt;` by default (`escape_html`); pymupdf4llm writes them raw and has
  ligature errors (`efciencyfi`).

## PDF speed and memory

| Run | Cores | Wall time | s/page | Peak RSS |
|---|---|---|---|---|
| pymupdf4llm (production call, no OCR engine) | 2 | **70 s** (1.2 min) | 0.32 | 0.45 GB |
| Docling ACCURATE, whole document, 1 process | 2 | 907 s (15.1 min) | 4.20 | **4.7 GB** |
| Docling ACCURATE, 10-page ranges, 1 process, `MALLOC_ARENA_MAX=2` | 2 | **835 s (13.9 min)** | 3.87 | **1.9 GB** (1.7 to 1.9 GB over 22 ranges) |
| **Docling FAST, 10-page ranges, 1 process, `MALLOC_ARENA_MAX=2`** (decided, D4) | 2 | **618 s (10.3 min)** | 2.86 | **1.8 GB** (flat after the 4th range) |
| Docling FAST, split in 2 × 108 pages, 2 processes × 1 thread | 2 | 584 s (9.7 min) | 2.70 | 3.2 GB per process |
| Docling FAST, split in 4 × 54 pages, 4 processes × 1 thread | 4 | 292 s (4.9 min) | 1.35 (wall) | 2.5 GB per process |
| Unstructured `hi_res` (pages 1–30, extrapolated) | 2 | about 1,930 s (32 min) | 8.95 | 2.0 GB |
| Unstructured `fast` (no tables) | 2 | 48 s | 0.22 | 0.6 GB |

- The ticket's trial reported 1.3–1.8 s/page and 1.4–1.5 GB without naming its machine or corpus; this spec is
  table-heavy (228 tables). The gap is not explained, so speed is a gate on the real image (G4).
- Run-to-run noise: two identical 8-page runs differed by about 20 %, and some runs shared the machine with
  another pinned run; read the numbers as ±20 %.
- Page splitting scales linearly with cores (2 to 4 cores: 2.1×) and changes only paragraphs that cross a
  range boundary (213 of 216 pages identical between 10-page and 108-page ranges). This supports a later
  external split (D1).

## Office and web formats

| Format (generated from the spec) | Convert | Word recall / precision | Headings found | Level exact | Tables (source 228) | Cell recall | Page markers |
|---|---|---|---|---|---|---|---|
| DOCX (pandoc) | 7.5 s | 0.984 / 1.00 | 358/358 | 0/358 (all one level deep; with the heading shift 358/358) | 228 | 1.00 | none (no pages) |
| PPTX (pandoc, 229 slides) | 10.0 s | F1 0.989 | 1/358 (the pandoc deck has no title placeholders) | – | 228 | 1.00 | 229 pages in the document model |
| PPTX (python-pptx, real title placeholders) | <1 s | all text | 3/3 slide titles as `#` | – | – | – | per-slide export verified |
| XLSX (40 tables, one sheet each) | 0.15 s | – | – | – | 40/40 | 1.00 | 40 pages (one per sheet) |
| HTML (pandoc) | 5.7 s | 0.999 / 1.00 | 358/358 | 358/358 | 228 | 0.95 | none |
| Markdown (the source) | 3.9 s | 0.999 / 1.00 | 358/358 | 358/358 | 228 | 0.95 | none |
| CSV (3 rows) | <1 s | all cells | – | – | 1/1 | 1.00 | none |

- No ML model runs for these formats (XML and HTML backends); peak RSS at most 0.6 GB on these small files
  (the largest, HTML, is 0.8 MB). The runs exported each document in one block; the markers and sheet-name
  headings of [office-web.md](../office-web.md#structure) are checked by G11, not measured here.
- The format extras add python-docx, python-pptx, openpyxl, beautifulsoup4, soupsieve, marko, et-xmlfile,
  xlsxwriter and pylatexenc: 7 MB, all MIT or BSD. Every format above converted in that minimal install.
- HTML and Markdown cell recall 0.95 comes from the space Docling writes before a comma between inline-code
  spans ([pdf.md](../pdf.md#known-defects)); the text is present.

## Findings

1. PDF quality: Docling with the structure pass is equal to or better than pymupdf4llm on every structural
   metric measured, and better on tables. Without the structure pass, heading levels are flat.
2. Docling's own switches do not fix those defects: `HeadingHierarchyOptions` orders levels wrongly, the
   pypdfium2 backend breaks tables, TableFormer V2 empties cells.
3. FAST gives the same quality as ACCURATE on both PDF inputs, in 26 % less time (fixture: 17 %).
4. Speed: Docling FAST is about 9 times slower than pymupdf4llm on 2 vCPU; only more cores reduce the wall time.
5. Memory: a whole-document conversion is far over the pod size; 10-page ranges keep it flat, so ranges are
   mandatory.
6. OCR: production did not OCR before Docling either.
7. Unstructured is slower, weaker on tables, and adds licence gaps.
8. Office and web formats are cheap and accurate in Docling; the only structural fix needed is the DOCX
   heading shift.
9. Image: the minimal `docling-slim` install needs `opencv-python-headless<5` explicitly
   ([engines/docling.md](../engines/docling.md#packages)); with it the environment is 1.5 GB (full `docling`:
   1.7 GB) and the output is byte-identical to the full install. The Docling model cache is 506 MB including the
   unused TableFormer V2.

## Reproduce

G1, G8 and G11 are CI tests; G2, G4 and G5 run with `backend/examples/parse_benchmark.py`
([acceptance-gates.md](../acceptance-gates.md#gates)). The comparison scripts for pymupdf4llm and Unstructured ran
in a throw-away environment and are not committed.

```python
# Docling, PDF, as measured (the engine now also limits allowed_formats to the stored format)
opts = PdfPipelineOptions()
opts.do_ocr = False
opts.do_table_structure = True
opts.table_structure_options = TableStructureOptions(do_cell_matching=True, mode=TableFormerMode.FAST)
opts.generate_picture_images = False
opts.heading_hierarchy_options = HeadingHierarchyOptions(enabled=False)
opts.accelerator_options = AcceleratorOptions(num_threads=2, device="cpu")
opts.artifacts_path = "/opt/docling-models"
converter = DocumentConverter(
    allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX, InputFormat.XLSX,
                     InputFormat.HTML, InputFormat.MD, InputFormat.CSV],
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)},
)
converter.convert(pdf, page_range=(a, b))             # 10-page ranges, MALLOC_ARENA_MAX=2
doc.export_to_markdown(page_no=p, escape_underscores=False, escape_html=False, compact_tables=True)
```

```python
# pymupdf4llm baseline = the production call before Docling, environment without RapidOCR or Tesseract
pymupdf4llm.to_markdown(pdf, page_chunks=True, write_images=True, image_format="png", dpi=170, table_output="markdown")
```

```python
# Unstructured
partition_pdf(filename=pdf, strategy="hi_res" | "fast", infer_table_structure=True, languages=["eng"])
# Title -> heading (category_depth + 1), Table.metadata.text_as_html -> GFM, Image -> placeholder,
# Header/Footer/PageNumber dropped, one block per metadata.page_number
```

Test files: the spec PDF from `pandoc specflow-specification.md -f gfm -t html5 -s` with a plain print CSS (A4,
15–18 mm margins, 10.5 pt body, bordered tables), then Chromium `page.pdf(format="A4")` through Playwright (216
pages, 1.28 MB). DOCX and PPTX from `pandoc specflow-specification.md -f gfm -o spec.docx|spec.pptx`; XLSX from
the first 40 Markdown tables with openpyxl; the PPTX title check from a 3-slide python-pptx deck (layout "Title
and Content"). The cost figure of the rejected GCP alternative applies the source research cost model ($0.10
per worker-hour for 2 vCPU) to the measured FAST speed.
