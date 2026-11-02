---
type: Design
title: "Parse limits"
description: "The limits a file must stay within to be parsed, and the safety rules of the parse"
status: stable
tags: [file-parsers]
---
# limits.md: parse limits and safety rules

## Parse limits

The parse checks these before it converts a file, and counts them while it reads the file, never from sizes
the file declares about itself. A file over a limit ends as `document_too_large`
([errors.md](errors.md#error-codes)). The values are constants in `file_parsers/limits.py`; none is a setting.

| Limit | Value | Applies to |
|---|---|---|
| Pages | 500 | PDF, counted from the page tree before any conversion |
| Slides | 500 | PPTX |
| Inflated size of all entries | 200 MiB | DOCX, PPTX, XLSX |
| Compression ratio of one entry | 100 to 1, checked once an entry has inflated past 1 MiB | DOCX, PPTX, XLSX |
| Entries in the ZIP archive | 10,000 | DOCX, PPTX, XLSX |
| Cells | 200,000, all sheets together | XLSX, CSV |
| File size on disk | the upload caps of [formats.md](formats.md#accepted-formats) for Office and text files | Drive files; uploads meet the same caps at the API |
| Markdown produced | 5 MiB | Office and web formats (a PDF is bounded by its page count) |

How they are counted:

- ZIP sizes and ratios come from the bytes actually inflated, duplicates and data-descriptor entries
  included, not from the archive's headers.
- XLSX cells are an upper bound over every XML-looking entry of the archive, wherever the workbook places its
  sheets; the sheet dimension tag is ignored. CSV cells use Docling's own dialect detection and line-ending
  rules, so the count matches what Docling will read.
- Slides are the larger of the slide list and the slide relationships.

## Why these values

- Pages and slides (decision D3): the longest allowed PDF must finish well inside one activity attempt
  ([runtime.md](runtime.md#time-limits)).
- The ZIP, cell and Markdown limits (decision N2) protect a single-slot worker with 3 GiB of memory from ZIP
  bombs and huge sheets; the Markdown cap equals the one of `drive-convert`.
- The upload caps match the text the frontend shows.

Decisions D3 and N2: [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md).

## Safety rules

- XML parsers do not resolve external entities. A DOCX or PPTX part declared as XML, and any XML-looking
  entry of an XLSX, must not carry a DTD or be UTF-16 encoded; SVG images are exempt.
- HTML and Markdown are converted without remote or local fetching and without fetching images (Docling's
  defaults, set explicitly); no script runs.
- A corrupt archive (bad CRC, encrypted entry, unsupported compression, truncated file) ends as
  `document_parse_failed`, not as a retried error.
- A file that is not its stored format is refused ([formats.md](formats.md#one-format-per-conversion)).
