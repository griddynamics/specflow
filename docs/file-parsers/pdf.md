---
type: Design
title: "PDF pipeline"
description: "Page ranges, the structure pass, known engine defects and LLM enrichment of PDF files"
status: stable
tags: [file-parsers]
---
# pdf.md: the PDF pipeline

## Pipeline

1. Count the pages from the page tree with pypdfium2 and refuse a PDF over the page cap
   ([limits.md](limits.md#parse-limits)) before any conversion.
2. Convert page ranges of 10 pages, one after the other, through the engine
   ([engines/docling.md](engines/docling.md#configuration)); report progress after each range. Ranges keep the
   memory flat ([runtime.md](runtime.md#sizing)); a paragraph that crosses a range boundary is the only output
   that can change with the range size.
3. Run the structure pass once over the whole document, after the last range.
4. Render with `PDFRenderer`: `<!-- source-page: N -->` before each page, the page Markdown, figure
   placeholders. The stored Markdown keeps the shape it had before Docling.

## Structure pass

`PdfStructure` (`file_parsers/pdf/structure.py`, `word_repair.py`) repairs the engine's Markdown against the
pdfium text layer of the same page. It is deterministic, uses no model and takes about 0.08 s for 8 pages and 2 s for 216 pages.
Without it, Docling's heading levels are flat
([benchmarks/docling-vs-pymupdf4llm.md](benchmarks/docling-vs-pymupdf4llm.md#findings)).

### Heading levels

Find each heading's text in the pdfium text layer of its page and read its effective font size (nominal size
times the text-matrix scale; the nominal size alone is 1.0 in many PDFs). Rank the distinct sizes, in 0.5 pt
buckets, across the whole document: largest `#`, next `##`, down to `######`. A heading whose text is not found
takes its numbering depth (`1 Intro` is `##`); an unnumbered one keeps the level Docling gave it.

### Line-break hyphens

pdfium marks a hyphen at a line break (U+FFFE, or `-` and the line break), so the page text tells which words
were broken, and how many times; each break is evidence for one repair only.

- Hyphen and space. Docling writes `FR- EXT-001` or `read- only` for `FR-EXT-001`, `read-only`, mostly in
  wrapped table cells. A `left- right` whose sides meet at a line-break hyphen becomes `left-right`, in prose,
  headings and table rows, never inside a code span or an indented code line.
- Joined compounds. Docling sometimes joins the sides and drops the hyphen (`thirdparty`). The hyphen comes
  back only when both parts are words written whole elsewhere in the document, each at least 3 letters (an
  identifier-like part such as `FR` may be shorter), and the joined word is never written whole. Anything else
  was a syllable break and stays joined (`database`, `display`).

### Glued words

A prose token that is not a word of the page, but equals two or three consecutive words of the page, gets the
spaced form (`judgesmove` becomes `judges move`). A word joined across a line-break hyphen counts as a word of
the page, so it is never split. Tables, code, comments and image lines are not touched.

### Figures

Figures keep the placeholder shape `![pN.figK](images/…)`. Production writes no image files; the command line
writes them for the enrichers.

## Pages without a text layer

A page with no text layer (a scan or an image) gives no text, as before Docling; the engine logs those pages
per range (`pdf.pages.no_text_layer`), so the need for OCR can be measured ([roadmap.md](roadmap.md#deferred-features)).

## Known defects

Docling defects the structure pass does not repair, seen on the measured files
([benchmarks/docling-vs-pymupdf4llm.md](benchmarks/docling-vs-pymupdf4llm.md#pdf-quality-on-the-fixture)):

- A paragraph that continues across a page break is joined to the previous page, so its text is cited one
  page early.
- A font with a broken glyph map can turn a character into another (`0.69` read as `0▷69`).
- Running headers and footers are dropped (better for the graph; the golden file keeps them).
- In table cells with several inline-code spans, Docling writes a space before the comma (`FR-IAM , FR-PRJ`).

## LLM enrichment on the command line

`python -m file_parsers.pdf.parser --enrich` asks the engine for picture images and table boxes, then runs the
figure, Mermaid and math enrichers, which render pages and clips with pypdfium2 (`pdfium_typed.py`). The
production parse never calls them.
