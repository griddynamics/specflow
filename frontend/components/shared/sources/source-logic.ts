// sources-ux (FR-SRC-0001..0004, 0008..0011, 0018..0020, 0025; DATA-SRC-0001, NFR-USE-0003): the pure texts of the source card, the
// details modal, the file rows and the toasts. No React. The texts are the units'
// texts word for word, except the product name: the app writes "Specflow" (SD12, brand rule), the units spell it differently.
import { finishedOf, fmtStamp, fullSourceName, sourceState } from "@/lib/domain/source-state";
import {
  CATEGORY_LABEL,
  type FileCategory,
  type FileReason,
  type FileState,
  type Source,
  type SourceFile,
  type SourceKind,
  type SourceState,
  type TooLargeFile,
  type UploadReport,
} from "@/lib/domain/sources";
import { LISTING } from "./drive-logic";
import { plural } from "./kb-logic";
import { nonEmpty } from "@/components/shared/ui/utils";
import { ACCEPTED_EXTENSIONS, FORMAT_LABELS, formatOf, limitsText, sizeText } from "@/lib/domain/upload-formats";

const files = (n: number): string => `${String(n)} ${n === 1 ? "file" : "files"}`;

// ---- Kind ----

export const KIND_LINE: Record<Exclude<SourceKind, "upload">, string> = {
  gdrive: "Google Drive",
  github: "GitHub",
  confluence: "Confluence",
  jira_epic: "Jira epic",
  linear_project: "Linear project",
  box: "Box",
  dropbox: "Dropbox",
  notion: "Notion",
  drawio: "diagrams.net",
};

// ---- The card (FR-SRC-0001..0004, NFR-USE-0003) ----

export interface CardView {
  state: SourceState;
  /** The kind name, or the start of an upload (FR-SRC-0001). */
  line2: string;
  /** FR-SRC-0003: the only numbers on the card. */
  countLine: string;
  /** FR-SRC-0004: the orange warning icon before the count line (Done with problems only). */
  warn: boolean;
  /** The state as text for assistive technology (FR-SRC-0002 notes). */
  srText: string;
}

/** Files that need the member's attention: every problem state. */
export const problemCount = (c: Source["counts"]): number => c.not_read + c.skipped + c.failed + c.already_in_project;

/** DATA-SRC-0001 table 2, column "Card line"; a source with no failure here failed because every file did. */
export function failedCardLine(s: Pick<Source, "failure">): string {
  if (s.failure === null) return "No file could be read";
  return s.failure === "no_access" || s.failure === "not_a_folder" ? "Cannot open the folder" : "Import failed";
}

export function cardView(s: Source): CardView {
  const state = sourceState(s);
  const line2 = s.kind === "upload" ? fmtStamp(s.addedAt) : KIND_LINE[s.kind];
  const finished = finishedOf(s);
  let countLine: string;
  let srText: string;
  switch (state) {
    case "processing":
      countLine = s.listing ? LISTING : `${String(finished)} of ${files(s.total)}`;
      srText = s.listing ? "Processing, listing files" : `Processing, ${String(finished)} of ${files(s.total)}`;
      break;
    case "done":
      countLine = files(s.total);
      srText = "Done";
      break;
    case "done_with_problems": {
      countLine = `${String(s.counts.done)} of ${files(s.total)} processed`;
      const n = problemCount(s.counts);
      srText = `Done, ${String(n)} ${n === 1 ? "file needs" : "files need"} attention`;
      break;
    }
    case "failed":
      countLine = failedCardLine(s);
      srText = "Failed";
      break;
  }
  return { state, line2, countLine, warn: state === "done_with_problems", srText };
}

// ---- The file row (FR-SRC-0010, DATA-SRC-0001 table 1) ----

export const STATE_LABEL: Record<FileState, string> = {
  waiting: "Waiting",
  processing: "Processing",
  done: "Done",
  not_read: "Not read",
  skipped: "Skipped",
  failed: "Failed",
  already_in_project: "Already in project",
};

// Looked up by any string: a category this client does not know reads "these files".
const CATEGORY_LABEL_BY_NAME: Partial<Record<string, string>> = CATEGORY_LABEL;
const categoryLabel = (c: FileCategory): string => (c === "markdown" ? "Markdown files" : (CATEGORY_LABEL_BY_NAME[c] ?? "these files"));

const FAILED_REASON: Partial<Record<FileReason, string>> = {
  access_lost: "Specflow lost access to this file.",
  drive_unavailable: "Google Drive did not answer.",
  read_failed: "Specflow could not read this file.",
  store_failed: "Specflow could not save this file.",
  source_failed: "The import of this source failed.",
};

/** The reason under a problem file; null for the states that have none. An unknown code reads "Something went wrong." (DATA-SRC-0001.AC4). */
export function reasonText(f: Pick<SourceFile, "state" | "reason" | "category" | "holder">): string | null {
  switch (f.state) {
    case "not_read":
      return `Saved, not read: Specflow cannot read ${categoryLabel(f.category)} yet.`;
    case "skipped":
      return `Skipped: Specflow does not import ${categoryLabel(f.category)}.`;
    case "already_in_project":
      // R15: no holder (its source was removed, or an old file): the project is the place.
      return f.holder ? `The same file is already in ${fullSourceName(f.holder)}.` : "The same file is already in this project.";
    case "failed":
      return nonEmpty(f.reason && FAILED_REASON[f.reason]) ?? "Something went wrong.";
    case "waiting":
    case "processing":
    case "done":
      return null;
  }
  return null; // a state this client does not know has no reason
}

// ---- The upload toast (FR-SRC-0020..0022; the texts for added, duplicate and retried files are the ones Add files always had) ----

export function toastFor(r: UploadReport): { title: string; message: string; tone: "success" | "info" | "danger" } {
  const already = r.alreadyInProject + r.duplicates;
  const lines: string[] = [];
  if (r.added > 0) lines.push("Specflow collects them now.");
  if (r.readAgain > 0)
    lines.push(`${plural(r.readAgain, "file")} could not be read before; Specflow reads ${r.readAgain === 1 ? "it" : "them"} again.`);
  if (already > 0) lines.push(`${plural(already, "file")} ${already === 1 ? "is" : "are"} already in this project; nothing new was added.`);
  const big = r.tooLarge ?? [];
  lines.push(...tooLargeLines(big));
  const other = r.notAdded - big.length;
  if (other > 0) lines.push(`${plural(other, "file")} ${other === 1 ? "was" : "were"} not added.`);
  return { title: uploadTitle(r, already), message: lines.join(" "), tone: uploadTone(r) };
}

/** One sentence per cap: "a.pdf is over the 100 MB limit." (the cap is the API's `details.max_bytes` for that file's type). */
function tooLargeLines(files: TooLargeFile[]): string[] {
  const byCap = new Map<number, string[]>();
  for (const f of files) byCap.set(f.maxBytes, [...(byCap.get(f.maxBytes) ?? []), f.name]);
  return [...byCap].map(([cap, names]) => `${names.join(", ")} ${names.length === 1 ? "is" : "are"} over the ${sizeText(cap)} limit.`);
}

function uploadTitle(r: UploadReport, already: number): string {
  if (r.added > 0) return `${plural(r.added, "file")} added`;
  if (r.readAgain > 0) return `Reading ${r.readAgain === 1 ? "the file" : "the files"} again`;
  return already > 0 ? "Already in this project" : "Files not added";
}

function uploadTone(r: UploadReport): "success" | "info" | "danger" {
  if (r.notAdded > 0) return "danger";
  return r.added > 0 || r.readAgain > 0 ? "success" : "info";
}

/** The upload could not start: no source was made, so the toast names every file (SPECS §7.3). */
export const startFailedToast = (names: string[]): { title: string; message: string; tone: "danger" } => ({
  title: "Files not added",
  message: `${names.join(", ")} ${names.length === 1 ? "was" : "were"} not added. Try again.`,
  tone: "danger",
});

/** Only the accepted types are sent (the API's table, by extension): the other picked files are not sent, and one toast names them. */
export function splitUpload(picked: File[]): { files: File[]; skipped: string[] } {
  return { files: picked.filter((f) => formatOf(f.name)), skipped: picked.filter((f) => !formatOf(f.name)).map((f) => f.name) };
}

export const unsupportedFilesToast = (names: string[]): { title: string; message: string; tone: "danger" } => ({
  title: "File type not supported",
  message: `${names.join(", ")} ${names.length === 1 ? "was" : "were"} not added. Specflow reads ${FORMAT_LABELS} files (${ACCEPTED_EXTENSIONS}). Limits: ${limitsText()}.`,
  tone: "danger",
});
