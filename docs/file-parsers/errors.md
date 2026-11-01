---
type: API
title: "Parser error codes"
description: "Error codes of the upload API and the parse, and how parse failures are classified"
status: stable
tags: [file-parsers]
---
# errors.md: error codes of the upload API and the parse

## Error codes

| Code | Where | Meaning |
|---|---|---|
| `file_too_large` | Upload API, HTTP 413 | The body is over the cap of its type ([formats.md](formats.md#accepted-formats)). `details.max_bytes` holds the cap. |
| `unsupported_format` | Upload API, HTTP 415 | The type or the first bytes do not match an accepted format. The message lists the accepted types. |
| `document_too_large` | Parse, stored on the document | A limit of [limits.md](limits.md#parse-limits) was passed. Final: the parse is not retried. |
| `document_parse_failed` | Parse, stored on the document | A file that is not a PDF could not be read: malformed, corrupt, unsupported content, or not its stored format. Final. |
| `pdf_parse_failed` | Parse, stored on the document | A PDF could not be read. Unchanged from before Docling. |

A Drive file that fails in the parse shows as a failed file with the same reason (`read_failed`).

## How failures are classified

- `file_parsers/limits.py` defines `DocumentTooLargeError` and `DocumentParseFailedError`; the parse activity
  maps them, with the PDF parser's error, to the codes above as non-retryable failures, so an attempt that
  hits a limit or a broken file does not run again.
- Only exception type names reach the stored codes; no file content or internal message is stored.
- The codes are application-level values, so adding one needs no migration. Decision N3 kept
  `pdf_parse_failed` for PDF so that existing rows and clients keep their values
  ([decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md#decided-questions)).
