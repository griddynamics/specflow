import { randomBytes } from "node:crypto";

/** A tiny synthetic "PDF" (magic bytes + random body): enough for the stub API, never a real document. */
export const syntheticPdf = (size = 2048) => Buffer.concat([Buffer.from("%PDF-1.7\n"), randomBytes(size), Buffer.from("\n%%EOF\n")]);

const MAGIC = { ".pdf": "%PDF-1.7\n", ".docx": "PK\x03\x04", ".pptx": "PK\x03\x04", ".xlsx": "PK\x03\x04" };
const TEXT = { ".html": "<!doctype html><title>t</title><p>x</p>\n", ".md": "# Title\n\ntext\n", ".csv": "a,b\n1,2\n" };

/** A tiny synthetic document of the type of `extension` (the first bytes the API checks, then filler); never a real document. */
export const syntheticDocument = (extension, size = 512) => {
  const head = MAGIC[extension] ?? TEXT[extension];
  if (head === undefined) throw new Error(`no synthetic document for ${extension}`);
  const filler = extension in TEXT ? Buffer.from(" ".repeat(size)) : randomBytes(size);
  return Buffer.concat([Buffer.from(head, "latin1"), filler]);
};
