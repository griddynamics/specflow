You are describing a figure extracted from a document for readers who cannot
see the image.

Return JSON only. No prose and no code fences.

{
  "kind": "chart" | "diagram" | "equation" | "screenshot" | "photo" | "other",
  "is_decorative": boolean,
  "summary": "one sentence, at most 30 words",
  "details": ["sentence", "sentence"]
}

Rules:

- Put 2 to 5 complete sentences in "details".
- Classify a standalone mathematical expression or derivation as "equation".
  Do not transcribe its LaTeX in this response; a dedicated step handles that.
- Spell every name, label, axis and identifier exactly as the document spells
  it. Match anything read in the image against the vocabulary and page context
  below; the document wins because small figure text is often misread.
- Never invent precise numeric values. Describe trends, structure and
  relationships instead.
- For a diagram, name the nodes and state how they connect.
- For a chart, state what each axis plots and what the series are.
- Set "is_decorative" to true only for logos, rules or ornaments carrying no
  information.

Vocabulary used across the whole document — these are the correct spellings:

{glossary}

Page context (page {page_number} of the source document):

{page_context}
