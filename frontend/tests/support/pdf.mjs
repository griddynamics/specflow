import { randomBytes } from "node:crypto";

/** A tiny synthetic "PDF" (magic bytes + random body): enough for the stub API, never a real document. */
export const syntheticPdf = (size = 2048) => Buffer.concat([Buffer.from("%PDF-1.7\n"), randomBytes(size), Buffer.from("\n%%EOF\n")]);
