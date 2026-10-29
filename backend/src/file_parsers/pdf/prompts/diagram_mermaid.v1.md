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
- If a label is visible but unusual or misspelled, preserve it literally. Put
  that literal label in "literal_ambiguous_labels"; do not normalize or guess.
- Use only nodes and edges visible in the image or explicitly supported by the
  page context. Do not invent components, connections, or directions.
- Prefer `flowchart TD` for architecture diagrams. Use simple ASCII node IDs,
  quoted node labels, and `-->|edge label|` for labeled arrows.
- Do not put Markdown fences around Mermaid and do not include HTML.

Page context (page {page_number} of the source document):

{page_context}
