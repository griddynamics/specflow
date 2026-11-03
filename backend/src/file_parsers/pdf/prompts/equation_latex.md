You are transcribing one displayed mathematical equation from an image.

Return JSON only. No prose and no code fences.

{
  "latex": "LaTeX source without $ delimiters",
  "confidence": "high" | "medium" | "low",
  "uncertain_tokens": ["literal token"]
}

Rules:

- Preserve every operator, subscript, superscript, Greek letter, accent,
  bracket, and function argument exactly.
- Use standard LaTeX. Use `\begin{aligned} ... \end{aligned}` for multi-line
  derivations, aligning at `&=`.
- Do not add explanatory prose or delimit the result with `$`, `$$`, `\(`, or
  `\[`.
- If any mathematical symbol is unclear, report it in "uncertain_tokens" and
  lower confidence. Do not guess.

Page context (page {page_number} of the source document):

{page_context}
