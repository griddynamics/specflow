---
type: Requirements
title: "Parser acceptance gates"
description: "The gates a parser change must pass, where each runs, and their current status"
status: stable
tags: [file-parsers]
---
# acceptance-gates.md: the gates a parser change must pass

## Gates

A change of the engine, its configuration, its version or the structure pass passes these gates again.

| Gate | Check | Threshold | Where it runs |
|---|---|---|---|
| G1 Fixture quality | `backend/tests/data/specs/extraction.pdf` through the real `file_to_markdown`, against the golden file with its `llm-*` blocks and math removed | headings 9/9 at the exact level; tables 3/3 with cell recall ≥ 0.95; word F1 ≥ 0.98; figure placeholders equal the golden file; 8/8 page markers | CI, offline: `backend/tests/test_pdf_quality.py` |
| G2 Spec quality | The 216-page spec PDF against its source Markdown | headings ≥ 357/358 found and at the exact level; table-cell recall ≥ 0.85; word F1 ≥ 0.975; no empty page | Manual: `backend/examples/parse_benchmark.py` |
| G3 Real corpus | About 20 real files: PDFs including slide decks and multi-column layouts, real DOCX, PPTX and XLSX; Docling against pymupdf4llm (PDF) and against the source (Office) | no page that pymupdf4llm filled is empty; no lost table; heading order reviewed by a person | Manual, with the owner's files |
| G4 Speed | The spec PDF in the worker-parse image on 2 vCPU, median of 3 runs | ≤ 13 min | Manual: `parse_benchmark.py` in the container |
| G5 Memory | Peak RSS of the worker process on the spec PDF | ≤ 2.5 GB per process; the pod limit is 3 GiB | Manual, with G4 |
| G6 Licence | The dependency set of every image against an allowlist; `pymupdf`, `pymupdf4llm`, `pymupdf-layout` absent; `NOTICE` complete | pass | `make validate` and the CI job `static-licences` (`validation/ci/licence_gate.py`) |
| G7 Offline | worker-parse parses with network egress blocked, including HTML and Markdown with remote images | pass | Manual, `docker run --network none` |
| G8 Determinism | The same file twice, every format | byte-identical Markdown | CI: `test_pdf_quality.py`, `test_office_quality.py` |
| G9 Graph | One re-parsed and one older document in a graph build | re-parsed revised once; older unchanged ([architecture.md](architecture.md#graph-revision-rule)) | CI: `backend/tests/test_parse_graph_revision.py` |
| G10 Real container | The worker-parse image, read-only root filesystem, uid 10001, 3 GiB with the 1 GB tmpfs; the spec PDF, an image-heavy PDF and one Office file at each limit | no OOM kill; peak memory and time recorded; no system package missing | Manual |
| G11 Office and web quality | One small fixture per format with known content (a DOCX with headings and one with a Title style, a PPTX with title placeholders, XLSX, HTML, Markdown, CSV); uploads of `.md` and `.csv` with empty or odd browser types | word recall ≥ 0.98; every table with cell recall ≥ 0.95; DOCX, HTML and Markdown heading levels exact; one marker per slide and per sheet, the sheet name as heading; the canonical types accepted | CI: `backend/tests/test_office_quality.py` |
| G12 Hostile files | OOXML with an external entity, a ZIP bomb with false declared sizes, an XLSX with a false dimension tag, HTML with scripts and remote images, a file that is not its stored format, a corrupt archive; through the upload and the Drive path | refused with a fixed code, or parsed without the entity and without fetching, within the limits | CI: `test_office_guard.py`, `test_office_format_mismatch.py`, `test_office_guard_zip_errors.py`, `test_drive_import_hostile.py`, `test_api_upload_formats.py` |
| G13 Caps and limits | A 501-page PDF and a 501-slide PPTX; the time-limit rule ([runtime.md](runtime.md#time-limits)) | `document_too_large` with its reason; the rule holds | CI: `backend/tests/test_specflow_parse_runtime.py`; manual through the real workflow |

## Status

Measured when Docling became the default ([decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md)).
The container gates ran in the worker-parse image with `--network none --read-only --tmpfs /tmp --user
10001:10001 --memory 3g --cpus 2`, on a 4-vCPU development machine limited to 2 CPUs, not on the target node; the design-time measurements are in
[benchmarks/docling-vs-pymupdf4llm.md](benchmarks/docling-vs-pymupdf4llm.md).

| Gate | Status | Result |
|---|---|---|
| G1, G8 | met | 9/9 heading levels, tables 3/3, word F1 0.990; byte-identical runs |
| G2 | met | 357/358 headings, table-cell recall 0.967, word F1 0.986, 216 page markers, no empty page |
| G3 | open | waits for the owner's files |
| G4 | met | 721.5, 699.2 and 723.5 s; median 12.0 min |
| G5 | borderline | peak RSS 2338, 2575 and 2150 MiB: the median (2.45 GB) meets 2.5 GB, one run reached 2.70 GB; no run reached the 3 GiB limit |
| G6 | met | api 103, worker-agents 103 and worker-parse 154 packages clean; a set with pymupdf4llm fails |
| G7, G10 | met | the spec PDF, a 96 MB image-heavy PDF, an XLSX with 199,980 cells, a 49 MB DOCX and a 500-slide PPTX converted under the limits; no fetch, no OOM kill |
| G9, G11 | met | CI tests pass; G11 rests on generated fixtures |
| G12 | met | parser, upload and Drive paths in CI; not run against a live API |
| G13 | met | a 501-page PDF and a 501-slide PPTX end as `document_too_large` through the real parse workflow (Temporal, Postgres, S3-compatible storage) |
