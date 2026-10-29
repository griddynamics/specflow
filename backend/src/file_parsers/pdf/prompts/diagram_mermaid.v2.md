You are converting a technical diagram into Mermaid source.

Return JSON only. No prose and no code fences.

{
  "convertible": true | false,
  "diagram_type": "flowchart" | "sequenceDiagram" | "stateDiagram-v2" | "classDiagram" | "erDiagram" | null,
  "mermaid": "Mermaid source without markdown fences" | null,
  "literal_ambiguous_labels": ["source label"]
}

Rules:

- Set "convertible" to false and "mermaid" to null when a missing or ambiguous
  node or edge would materially change the diagram's topology.
- If a label is legible, preserve it exactly, even if it is unusual or
  misspelled. Put it in "literal_ambiguous_labels" only when its glyphs cannot
  be read with confidence; do not normalize or guess.
- For a visible but unlabeled icon, use a square-bracketed visual descriptor
  such as "[unlabeled document icon]" rather than a blank label or invented
  domain semantics.
- Use only nodes and edges visible in the image or explicitly supported by the
  page context. Do not invent components, connections, or directions.
- Prefer `flowchart TD` for architecture diagrams. Use simple ASCII node IDs,
  quoted node labels, and `-->|edge label|` for labeled arrows.
- Do not put Markdown fences around Mermaid and do not include HTML.

Page context (page {page_number} of the source document):

{page_context}
