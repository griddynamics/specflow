---
type: Design
title: "Office and web pipeline"
description: "Guard, conversion and structure of DOCX, PPTX, XLSX, HTML, Markdown and CSV files"
status: stable
tags: [file-parsers]
---
# office-web.md: the Office and web pipeline

## Pipeline

1. The guard checks the file ([Guard](#guard)).
2. The engine converts it as its stored format only, with no ML model
   ([engines/docling.md](engines/docling.md#configuration)): one section for DOCX, HTML, Markdown and CSV; one
   per slide (PPTX) or sheet (XLSX).
3. `OfficeStructure` applies the heading shift and the markers ([Structure](#structure)).
4. The Markdown size is checked against its cap ([limits.md](limits.md#parse-limits)).

## Guard

`file_parsers/office/guard.py` reads the real bytes before Docling sees them and refuses a file that breaks a
limit (`document_too_large`) or a rule (`document_parse_failed`):

- the file size on disk, for files that did not come through the upload API;
- for DOCX, PPTX and XLSX: inflated bytes, per-entry ratio and entry count, read from the streams;
  slides; cells; DTDs and UTF-16 in XML parts; corrupt archives;
- for CSV: cells, counted with Docling's own dialect detection;
- for every format: a file that is not its stored format ([formats.md](formats.md#one-format-per-conversion)).

The values and the counting rules are in [limits.md](limits.md); the codes in [errors.md](errors.md).

## Structure

### Heading shift

Docling writes Word "Heading 1" as `##` and keeps `#` for a Title style. When a DOCX has no `#` heading, every
heading moves up one level, so "Heading 1" becomes `#`. HTML and Markdown headings keep their levels; PPTX slide
titles are `#`.

### Markers

`<!-- source-page: N -->` comes before each PPTX slide and each XLSX sheet, and the sheet name follows as a
heading. DOCX, HTML, Markdown and CSV have no pages and get no marker.

## Known limits

- Docling sniffs the format from the first bytes before it looks at the extension. A Markdown or CSV file whose
  first bytes look like another file signature (`BM`, `ID3`, `MZ`, `%!`, `{\rtf`) is skipped by Docling and
  ends as `document_parse_failed` ([roadmap.md](roadmap.md#known-gaps)).
- The quality evidence comes from generated files (pandoc, python-pptx, openpyxl); real Office files are gate
  G3 ([acceptance-gates.md](acceptance-gates.md)).
