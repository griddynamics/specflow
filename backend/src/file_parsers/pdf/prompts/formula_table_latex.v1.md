You are transcribing a formula table from an image into GitHub-flavored
Markdown.

Return JSON only. No prose and no code fences.

{
  "markdown": "one complete GitHub-flavored Markdown table",
  "confidence": "high" | "medium" | "low",
  "uncertain_cells": ["row / column / issue"]
}

Rules:

- Preserve every visible row, section code, description, and formula. Do not
  invent rows.
- Put every mathematical expression inside inline `$...$` delimiters using
  standard LaTeX. Do not use display `$$...$$` inside a table.
- Use `<br>` only for intentional line breaks inside a table cell.
- Copy ordinary explanatory text outside the `$...$` delimiters.
- If a cell is unreadable, record it in "uncertain_cells" and lower confidence
  instead of guessing.

Deterministic table extraction, which may have fragmented formulas:

{source_table}
