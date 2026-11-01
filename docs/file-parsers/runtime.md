---
type: Design
title: "Parser runtime"
description: "The parse activity, its time limits, images, sizing, threads, logs and determinism"
status: stable
tags: [file-parsers]
---
# runtime.md: the parse activity, time limits, images, sizing, threads, logs and determinism

## Parse activity

- One `parse_document` activity converts one file on the `worker-parse` queue. There is no extra workflow,
  queue or activity per page range (decisions D1 and D11 of
  [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md#owner-decisions)).
- It downloads `documents.original_key`, picks the pipeline by `documents.media_type`
  ([architecture.md](architecture.md#the-parse-path)), and stores the Markdown and the engine id.
- A background heartbeat every 20 s carries pages done and pages total; the PDF pipeline updates them after
  each page range, the other formats once at the end.
- One parse runs at a time per `worker-parse` replica. The slot is a semaphore inside the activity, so the
  wait for it counts against the activity's start-to-close limit; a backlog needs more replicas.

## Time limits

These are constants in the code, not settings. They follow one rule: the workflow execution limit is at least
the attempts times the start-to-close limit plus a margin; the Drive wait is at least the execution limit; the
Drive wait activity limit is larger than the Drive wait. A unit test checks the rule.

| Limit | Value |
|---|---|
| Parse activity, start to close | 120 min |
| Parse activity, attempts | 2 (a timed-out attempt starts again at page 1) |
| Parse activity, heartbeat timeout | 60 s (detects a dead worker) |
| Parse workflow, execution | 250 min |
| An upload still `uploaded` or `processing` after | 250 min reads as failed (`unknown_error`) |
| Drive import, wait for one parse | 250 min |
| Drive import, wait activity, start to close | 255 min |
| Drive import, one file, start to close | 30 min (3 attempts) |
| Drive import, listing a folder tree | 20 min |
| Drive import, soft deadline and hard execution limit | 11 h and 12 h |

- The longest allowed parse, a PDF at the page cap of [limits.md](limits.md#parse-limits), takes about 28 min
  at the speed measured in the real image (gate G4, [acceptance-gates.md](acceptance-gates.md#status)), 33 min
  with 20 % noise. The rest of the 120 minutes covers about two more parses waiting for the slot on the same
  replica.
- Two attempts keep one retry for a crashed worker; a third would only add time, because a timed-out attempt
  starts again from page 1.
- The values are decision N4 ([decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md#decided-questions)).

## Images

Each service image carries only its own dependencies (decision D8): Docling, PyTorch, OpenCV and the models are
in the worker-parse image only. The api and worker-agents images need no code change for that, because the
queue registration is imported lazily (`temporal/registry.py`). What the image contains, its variables, its
memory limit, its filesystem and its grace period are deployment settings:
[configuration.md](../configuration.md#the-worker-parse-image). Containers that still reuse the api image
(compose `rustfs-init`) are a follow-up ([roadmap.md](roadmap.md#deferred-features)).

## Sizing

The memory limit holds because a PDF always converts in page ranges ([pdf.md](pdf.md#pipeline)), with the
allocator setting of [configuration.md](../configuration.md#the-worker-parse-image): a whole-document conversion
needs far more
([benchmarks/docling-vs-pymupdf4llm.md](benchmarks/docling-vs-pymupdf4llm.md#pdf-speed-and-memory)). The
measured peak in the real image is gate G5 ([acceptance-gates.md](acceptance-gates.md#status)). Nothing is
downloaded at run time ([engines/docling.md](engines/docling.md#models)).

## Deploy order

Roll out `worker-agents` and `worker-parse` before `api` whenever the parse contract changes (a new format, a new
storage key, a new Drive item kind): an `api` that accepts a file the workers cannot read must not run ahead of
them. On a rollback, revert `api` first. The first such deploy, Docling with Office and web formats, has its own
notes in [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md#deploy-order).

## Threads

Docling's thread count is the smaller of the CPU affinity and the cgroup CPU quota (`file_parsers/engines/cpu.py`,
cgroup v2 `cpu.max` or v1 `cpu.cfs_quota_us`). A `docker run --cpus 2`, a Kubernetes CPU limit or a Cloud Run
CPU setting limits CPU time, not the CPUs the process sees; more threads than the quota makes the parse slower
and holds more memory.

## Logs

| Event | When |
|---|---|
| `pdf.range.done` | After each PDF page range (first and last page, total pages) |
| `pdf.stage.start`, `pdf.stage.done` | Around the extraction and structure stages, with durations |
| `pdf.pages.no_text_layer` | Pages without a text layer in a range ([pdf.md](pdf.md#pages-without-a-text-layer)) |
| `pdf.structure.done` | After the structure pass, with the counts of headings and of each word repair ([pdf.md](pdf.md#structure-pass)) |

## Determinism

The same file gives byte-identical Markdown on every run, for every format: no model sampling, no OCR, no
network, pinned packages and model revisions. Gate G8 checks it ([acceptance-gates.md](acceptance-gates.md)).
