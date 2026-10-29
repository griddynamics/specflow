// sources-ux (FR-SRC-0001..0004, 0008..0011, 0018..0020, 0025; DATA-SRC-0001, NFR-USE-0003): the pure texts of the source card, the
// details modal, the file rows and the toasts. No React. The texts are the units'
// texts word for word, except the product name: the app writes "Specflow" (SD12, brand rule), the units spell it differently.
import type { IconName } from "@/components/ui/icon-map";
import { finishedOf, fmtStamp, fullSourceName, isProblem, progressOf, sourceState } from "@/lib/domain/source-state";
import {
  CATEGORY_LABEL,
  type FileCategory,
  type FileReason,
  type FileState,
  type Source,
  type SourceFile,
  type SourceKind,
  type SourceRef,
  type SourceState,
  type UploadReport,
} from "@/lib/domain/sources";
import { driveReasonMessage, LISTING } from "./drive-logic";
import { plural } from "./kb-logic";
import { nonEmpty } from "@/components/ui/utils";

const files = (n: number): string => `${String(n)} ${n === 1 ? "file" : "files"}`;

// ---- Kind ----

export const KIND_LINE: Record<Exclude<SourceKind, "upload">, string> = { gdrive: "Google Drive", github: "GitHub" };
/** The small label above the name in the details modal. */
export const KIND_LABEL = { gdrive: "Google Drive", github: "GitHub", upload: "Upload" } as const satisfies Record<SourceKind, string>;

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

/** The ring's arc, 0..1 (FR-SRC-0002): the progress of a Processing source, nothing while it lists. */
export const ringProgress = (s: Source): number => (sourceState(s) === "processing" ? progressOf(s) : 0);

// ---- The details header (FR-SRC-0008) ----

export interface HeaderText {
  kind: (typeof KIND_LABEL)[SourceKind];
  added: string;
  /** "[finished] of [total] files finished", or "Listing files…". */
  progress: string;
  /** 0..100 for the bar. */
  value: number;
  /** The details-header message of DATA-SRC-0001 table 2 for a failed source; it replaces the bar. */
  failure: string | null;
}

export function headerText(s: Source, driveEmail: string | null = null): HeaderText {
  return {
    kind: KIND_LABEL[s.kind],
    added: `Added by ${s.addedBy.name} · ${fmtStamp(s.addedAt)}`,
    progress: s.listing ? LISTING : `${String(finishedOf(s))} of ${files(s.total)} finished`,
    value: Math.round(progressOf(s) * 100),
    failure: s.failure ? driveReasonMessage(s.failure, driveEmail) : null,
  };
}

// ---- The groups (FR-SRC-0009) ----

export type GroupId = "attention" | "progress" | "done";
export const GROUPS: readonly GroupId[] = ["attention", "progress", "done"];
export const GROUP_LABEL: Record<GroupId, string> = { attention: "Needs attention", progress: "In progress", done: "Done" };

export function groupIdOf(state: FileState): GroupId {
  if (isProblem(state)) return "attention";
  return state === "done" ? "done" : "progress";
}

/** The count in each group heading, for the whole source (also before all its files are loaded). */
export function groupTotals(c: Source["counts"]): Record<GroupId, number> {
  return { attention: problemCount(c), progress: c.processing + c.waiting, done: c.done };
}

export const heading = (g: GroupId, n: number): string => `${GROUP_LABEL[g]} · ${String(n)}`;

/** True while an unfolded group has files that are not loaded yet; the list then asks for the next page. */
export function wantsMore(
  counts: Source["counts"],
  items: readonly Pick<SourceFile, "state">[],
  hasMore: boolean,
  folded: Record<GroupId, boolean>,
): boolean {
  if (!hasMore) return false;
  const total = groupTotals(counts);
  const loaded: Record<GroupId, number> = { attention: 0, progress: 0, done: 0 };
  for (const f of items) loaded[groupIdOf(f.state)] += 1;
  return GROUPS.some((g) => !folded[g] && loaded[g] < total[g]);
}

export const EMPTY_SOURCE = "No files in this source.";
export const LOAD_FAILED = "Could not load the files.";

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

/** Only glyphs of the design system's set exist (USAGE rule 7); there are no file-type glyphs for images or videos. */
export function fileIcon(category: FileCategory): IconName {
  if (category === "markdown") return "file-md";
  return category === "videos" ? "play-circle" : "file-text";
}

// ---- Remove (FR-SRC-0015, 0018, 0019, 0023, 0025) ----

export const REMOVE_TITLE = "Remove this source?";
export const REMOVE_FAILED = "Specflow could not remove this source. Try again.";
export const REMOVE_CUTOFF_MS = 60_000;

/** FR-SRC-0015: the warning in three parts, so that the name can be set in bold; joined it is the unit's sentence. */
export function removeWarning(s: Pick<Source, "total"> & SourceRef): [string, string, string] {
  return ["This removes ", fullSourceName(s), ` and its ${files(s.total)} from the project. You cannot undo this.`];
}
export const removeLabel = (ref: SourceRef): string => `To confirm, type "${fullSourceName(ref)}"`;
/** FR-SRC-0023: the exact full name, same case; spaces at both ends are ignored. */
export const nameMatches = (typed: string, ref: SourceRef): boolean => typed.trim() === fullSourceName(ref);

export const removedToast = (ref: SourceRef): { title: string; tone: "success" } => ({
  title: `${fullSourceName(ref)} removed`,
  tone: "success",
});
export const goneToast = (ref: SourceRef): { title: string; tone: "info" } => ({
  title: `${fullSourceName(ref)} was removed`,
  tone: "info",
});

// ---- The upload toast (FR-SRC-0020..0022; the texts for added, duplicate and retried files are the ones Add files always had) ----

export function toastFor(r: UploadReport): { title: string; message: string; tone: "success" | "info" | "danger" } {
  const already = r.alreadyInProject + r.duplicates;
  const lines: string[] = [];
  if (r.added > 0) lines.push("Specflow collects them now.");
  if (r.readAgain > 0)
    lines.push(`${plural(r.readAgain, "file")} could not be read before; Specflow reads ${r.readAgain === 1 ? "it" : "them"} again.`);
  if (already > 0) lines.push(`${plural(already, "file")} ${already === 1 ? "is" : "are"} already in this project; nothing new was added.`);
  if (r.notAdded > 0) lines.push(`${plural(r.notAdded, "file")} ${r.notAdded === 1 ? "was" : "were"} not added.`);
  return { title: uploadTitle(r, already), message: lines.join(" "), tone: uploadTone(r) };
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

/** Production reads PDF only (the upload API): the other picked files are not sent, and one toast names them. */
export function splitUpload(picked: File[], pdfOnly: boolean): { files: File[]; skipped: string[] } {
  if (!pdfOnly) return { files: picked, skipped: [] };
  const isPdf = (f: File) => f.type === "application/pdf" || /\.pdf$/i.test(f.name);
  return { files: picked.filter(isPdf), skipped: picked.filter((f) => !isPdf(f)).map((f) => f.name) };
}

export const pdfOnlyToast = (names: string[]): { title: string; message: string; tone: "danger" } => ({
  title: "PDF only",
  message: `${names.join(", ")} ${names.length === 1 ? "was" : "were"} not added; Specflow reads PDF files.`,
  tone: "danger",
});
