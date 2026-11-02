# INDEX.md: table of contents of the file parsers documentation

The file parsers (Python package `file_parsers`, run by the `worker-parse` service) turn uploaded and Drive
files into the Markdown that the knowledge graph is built from. Each fact is stated in one document; the
others link to it.

# overview.md: purpose, scope, entry points, key terms, reading order
- [overview.md](overview.md)

# architecture.md: the parse path, the engine port, engine ids, modules, the graph revision rule
- [architecture.md](architecture.md)

# formats.md: accepted formats and upload caps, upload API, storage keys, Drive classes, frontend
- [formats.md](formats.md)

# limits.md: parse limits and safety rules
- [limits.md](limits.md)

# errors.md: error codes of the upload API and the parse, and how failures are classified
- [errors.md](errors.md)

# runtime.md: the parse activity, time limits, sizing, threads, logs, determinism
- [runtime.md](runtime.md)

# pdf.md: the PDF pipeline: page ranges, the structure pass, known defects, LLM enrichment
- [pdf.md](pdf.md)

# office-web.md: the Office and web pipeline: guard, structure, known limits
- [office-web.md](office-web.md)

# engines/: one document per engine
- [engines/docling.md](engines/docling.md): Docling packages, configuration, models, forbidden features, upgrades

# acceptance-gates.md: the gates a parser change must pass, and their current status
- [acceptance-gates.md](acceptance-gates.md)

# decisions/: one record per decision, with its context, options and consequences
- [decisions/docling-replaces-pymupdf.md](decisions/docling-replaces-pymupdf.md): Docling replaces
  pymupdf4llm and parses Office and web formats

# benchmarks/: one record per measurement campaign, with its method and results
- [benchmarks/docling-vs-pymupdf4llm.md](benchmarks/docling-vs-pymupdf4llm.md): Docling, pymupdf4llm and
  Unstructured on PDF; Docling on Office and web formats

# roadmap.md: deferred features and known gaps
- [roadmap.md](roadmap.md)

# Where new content goes
- A new engine: a file in `engines/`, a row in the engine table of [architecture.md](architecture.md).
- A new format: a row in [formats.md](formats.md); its pipeline in the pipeline document it shares, or a new
  one next to [pdf.md](pdf.md) and [office-web.md](office-web.md).
- A new Drive class or upload rule: [formats.md](formats.md).
- A new or changed limit, error code or time limit: [limits.md](limits.md), [errors.md](errors.md) or
  [runtime.md](runtime.md); never a copy elsewhere.
- A decision: a new record in `decisions/`, with its options, risks and rollback; it links to the documents it
  changes.
- A new gate, or a changed threshold: [acceptance-gates.md](acceptance-gates.md).
- A deployment setting (image variable, memory, grace period): [configuration.md](../configuration.md), not
  this bundle.
- A measurement: a new record in `benchmarks/`; gates and decisions link to it.
- A deferred idea or a known gap: [roadmap.md](roadmap.md); remove it when it ships.
