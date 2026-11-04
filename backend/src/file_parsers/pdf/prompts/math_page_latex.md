You are reconstructing one mathematics-heavy textbook page as Markdown.

Return JSON only. No prose and no code fences.

{
  "markdown": "complete page Markdown",
  "confidence": "high" | "medium" | "low",
  "uncertain_regions": ["description"]
}

Rules:

- Preserve every visible logical paragraph, definition, remark, proposition,
  theorem, proof, equation, figure caption, and table caption. Do not
  summarize.
- Convert every expression to standard LaTeX: use `$...$` for inline math and
  `$$...$$` for displayed equations. Use `\begin{aligned} ... \end{aligned}`
  for multi-line derivations.
- Do not include image references, HTML superscript tags, raw OCR formula
  fragments, running headers, or printed page numbers.
- If a token cannot be read, insert `[UNCERTAIN]` at that exact point and list
  the region in "uncertain_regions"; do not guess.

Deterministic Markdown from the same page may be incomplete around formula
images. Use it only to cross-check prose and labels:

{source_markdown}
