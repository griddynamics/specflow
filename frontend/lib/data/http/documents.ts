// FE-BP-08.AC1, AC3 (TECH-SPECS §3.3, §3.4): the upload through `lib/api.ts`, rejecting only with a `DataError`.
// Upload = the raw File body with `Content-Type: application/pdf` and `X-Requested-With: specflow` (`lib/api.ts` sets both; the
// BFF streams it, `frontend/README.md` BFF rules). ONE file per request. Since GDRIVE plan 04 (K5) the server starts the parse
// itself: the browser no longer calls parse or summarize, so this file has no other method.
import { uploadDocument, type DocumentSummary, type UploadOutcome } from "@/lib/api";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

export type { DocumentSummary, UploadOutcome };

export interface DocumentsApi {
  upload(projectId: string, file: File): Promise<{ outcome: UploadOutcome; document: DocumentSummary }>;
}

/** QA B1: a document row must have the fields the screen reads; anything else is `invalid_response`. */
const isDocumentSummary = (d: unknown): d is DocumentSummary =>
  isRecord(d) &&
  isString(d.id) &&
  d.id !== "" &&
  isString(d.status) &&
  isString(d.summarization_status) &&
  isString(d.original_filename) &&
  typeof d.size_bytes === "number";

export function checkDocument(d: unknown): DocumentSummary {
  expectShape(isDocumentSummary(d));
  return d;
}

export function createHttpDocuments(): DocumentsApi {
  return {
    async upload(projectId, file) {
      const r: unknown = await guard(() => uploadDocument(projectId, file));
      return checked(() => {
        expectShape(isRecord(r) && (r.outcome === "created" || r.outcome === "duplicate" || r.outcome === "retried"));
        return { outcome: r.outcome, document: checkDocument(r.document) };
      });
    },
  };
}
