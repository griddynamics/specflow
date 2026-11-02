"""Office and web formats (DOCX, PPTX, XLSX, HTML, Markdown, CSV): input guard, content model, structure.

``guard`` inspects a file before any conversion (inflated bytes, per-entry ratio, cells, slides, DTDs, text
encoding), ``model`` is what an engine returns for these formats, ``structure`` turns it into the stored
Markdown (heading shift, page and sheet markers). Nothing here imports Docling.
"""
