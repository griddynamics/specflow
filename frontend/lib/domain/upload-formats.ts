// The document types Local files accepts. Mirrors the API table `backend/src/file_parsers/formats.py`
// (canonical media type per extension) and the upload caps of `backend/src/api/uploads.py` (DESIGN N2). The extension decides:
// browsers send an empty type for `.md` and `application/vnd.ms-excel` for `.csv` on Windows, so `file.type` is never used.
// The frontend deliberately refuses names outside this table (e.g. `.htm`, `.txt`) although the API accepts some of them by header.
// Legacy `.doc`, `.ppt` and `.xls` are recognised only to be refused.

const MB = 1024 * 1024;

export interface UploadFormat {
  readonly extension: string; // lower case, leading dot
  readonly mediaType: string; // canonical: the `Content-Type` the API expects
  readonly label: string;
  readonly maxBytes: number; // the API's 413 cap (N2)
}

export const UPLOAD_FORMATS: readonly UploadFormat[] = [
  { extension: ".pdf", mediaType: "application/pdf", label: "PDF", maxBytes: 100 * MB },
  {
    extension: ".docx",
    mediaType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    label: "Word",
    maxBytes: 50 * MB,
  },
  {
    extension: ".pptx",
    mediaType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    label: "PowerPoint",
    maxBytes: 50 * MB,
  },
  {
    extension: ".xlsx",
    mediaType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    label: "Excel",
    maxBytes: 50 * MB,
  },
  { extension: ".html", mediaType: "text/html", label: "HTML", maxBytes: 10 * MB },
  { extension: ".md", mediaType: "text/markdown", label: "Markdown", maxBytes: 10 * MB },
  { extension: ".csv", mediaType: "text/csv", label: "CSV", maxBytes: 10 * MB },
];

const BY_EXTENSION = new Map(UPLOAD_FORMATS.map((f) => [f.extension, f]));

/** The extension with its dot, lower case, from the last path segment; "" when there is none. */
function extensionOf(filename: string): string {
  const name = filename.split(/[\\/]/).pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot).toLowerCase() : "";
}

/** The accepted format of a file name, or `null` (unknown and legacy extensions are refused). */
export const formatOf = (filename: string): UploadFormat | null => BY_EXTENSION.get(extensionOf(filename)) ?? null;

/** The canonical `Content-Type` for an upload. An unaccepted name gets a generic type, so the API refuses it (415). */
export const contentTypeFor = (filename: string): string => formatOf(filename)?.mediaType ?? "application/octet-stream";

/** The file picker `accept` list: extensions and canonical types. */
export const UPLOAD_ACCEPT = UPLOAD_FORMATS.flatMap((f) => [f.extension, f.mediaType]).join(",");

/** "100 MB" for a byte count that is a whole number of MB; one decimal otherwise. */
export function sizeText(bytes: number): string {
  const mb = bytes / MB;
  return `${String(Number.isInteger(mb) ? mb : Math.round(mb * 10) / 10)} MB`;
}

const extensionList = (formats: readonly UploadFormat[]) => formats.map((f) => f.extension).join(", ");
/** "PDF, Word, PowerPoint, Excel, HTML, Markdown or CSV". */
export const FORMAT_LABELS = `${UPLOAD_FORMATS.slice(0, -1)
  .map((f) => f.label)
  .join(", ")} or ${UPLOAD_FORMATS[UPLOAD_FORMATS.length - 1].label}`;
/** ".pdf, .docx, .pptx, .xlsx, .html, .md, .csv" */
export const ACCEPTED_EXTENSIONS = extensionList(UPLOAD_FORMATS);

/** The N2 caps, grouped: "PDF up to 100 MB; Word, PowerPoint and Excel up to 50 MB; HTML, Markdown and CSV up to 10 MB". */
export function limitsText(): string {
  const groups = new Map<number, string[]>();
  for (const f of UPLOAD_FORMATS) groups.set(f.maxBytes, [...(groups.get(f.maxBytes) ?? []), f.label]);
  return [...groups].map(([bytes, labels]) => `${joinAnd(labels)} up to ${sizeText(bytes)}`).join("; ");
}

function joinAnd(items: string[]): string {
  return items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}
