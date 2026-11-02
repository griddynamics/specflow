---
type: Design
title: "Accepted formats"
description: "Accepted formats and upload caps, the upload API, storage keys, Drive classes and the frontend"
status: stable
tags: [file-parsers]
---
# formats.md: accepted formats, upload API, storage keys, Drive classes and the frontend

## Accepted formats

`file_parsers/formats.py` holds this table for the API, the Drive import and the parser; the frontend reads the
same rows from `backend/src/api/fixtures/upload-formats.json`, which a backend test keeps equal to it.

| Format | Extension | Canonical media type | Upload cap |
|---|---|---|---|
| PDF | `.pdf` | `application/pdf` | 100 MiB |
| Word | `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | 50 MiB |
| PowerPoint | `.pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | 50 MiB |
| Excel | `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | 50 MiB |
| HTML | `.html` | `text/html` | 10 MiB |
| Markdown | `.md` | `text/markdown` | 10 MiB |
| CSV | `.csv` | `text/csv` | 10 MiB |

Legacy `.doc`, `.ppt` and `.xls`, and every other type, are refused ([roadmap.md](roadmap.md#deferred-features)).

## Upload API

- The file extension picks the format and its canonical type. The `Content-Type` header must agree, except
  for browser quirks: an empty type or `text/plain` for `.md`, `text/plain` or `application/vnd.ms-excel` for
  `.csv`. The stored `documents.media_type` is always the canonical type. With no extension, or one that is
  not in the table (`.txt`, `.htm`), only a header that names an accepted type decides (`notes.txt` sent as
  `text/markdown` is Markdown), and the quirk headers are refused. A legacy extension is always refused.
- The stream checks only what the first bytes show: `%PDF-` for PDF, a ZIP header (`PK\x03\x04`) for Word,
  PowerPoint and Excel, UTF-8 without NUL bytes for text. It counts bytes against the cap while it streams
  the body, with no temporary file and no full buffering. Deeper checks belong to the parse
  ([limits.md](limits.md)).
- A refusal uses `unsupported_format` (415) or `file_too_large` (413) ([errors.md](errors.md#error-codes)).
- Single uploads and batch uploads follow the same rules.

## One format per conversion

A file is converted only as its stored format. Docling picks its converter from the file's bytes, so a
conversion allows only the stored format, and the guard refuses a mismatch before Docling runs: a text format
whose first bytes are a PDF or a ZIP (after an optional UTF-8 byte order mark), or an OOXML file whose
`[Content_Types].xml` declares another format's main document. A mismatch ends as `document_parse_failed`.
Without this rule a PDF stored as Markdown, or a workbook stored as a Word file, would skip the limits of its
real format.

## Storage keys

- The original is stored at `projects/<project>/documents/<document>/original.<extension>`; a PDF keeps
  `original.pdf`, so objects stored before Office and web formats stay valid. Code that has the document row
  reads `documents.original_key`.
- Removal and the cleanup sweep delete everything under the document prefix
  `projects/<project>/documents/<document>/`. `delete_by_prefix` accepts no other prefix shape, so a mistake
  cannot delete a project; every object store (GCS, S3, in-memory) implements it.

## Drive import

- Drive classifies a file by its MIME type only (DATA-GDUI-0001.AC5), inside the existing categories, with
  no migration (decision N1 of [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md#decided-questions)):

| Drive MIME type | Category | Processing class |
|---|---|---|
| PDF | PDF | processed |
| Word, PowerPoint, Excel (OOXML) | Word, PowerPoint, Excel | processed |
| Legacy Word, PowerPoint, Excel | Word, PowerPoint, Excel | stored only |
| `text/html`, `text/markdown`, `text/csv` | other files | processed |
| `text/plain` (even a `.md` file), images, video, everything else | as before | stored only |
| Google Docs, Slides, Sheets | Google | converted by `drive-convert`, not by the file parsers |

- A processed file is imported as a document with its canonical type and key, and deduplicated by content
  hash like a PDF.
- The Drive import does not apply the upload caps while it downloads; the guard holds the Office and text
  caps again before conversion, and a Drive PDF is limited by its page count ([limits.md](limits.md#parse-limits),
  [roadmap.md](roadmap.md#known-gaps)).
- Files imported as stored only before the parser read their type keep their key and get no document row; a
  re-import of the source turns them into documents.

## Frontend

- The Local files picker accepts the formats by extension, case-insensitive, and sends the canonical
  `Content-Type`, never the browser's `file.type`. Other files are not sent; one toast names them and lists
  the accepted types and their caps.
- A 413 shows which files are over which cap (`details.max_bytes`).
- The texts of the Local files menu and the Drive import summary name the read formats.
