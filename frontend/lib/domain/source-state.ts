// DATA-SRC-0001 / DATA-SRC-0002 / DATA-SRC-0003: the pure rules that turn file states into the state, progress and name of a source.
// No React, no store, no fetch. The demo mock and the production adapter both return `Source` values; every screen derives from here.
import type { FileReason, FileState, Source, SourceRef, SourceState } from "./sources";

// Parity with backend/src/api/fixtures/sources-contract.json (file_states, reason_codes, group_order, page_limit).
export const FILE_STATES: readonly FileState[] = ["waiting", "processing", "done", "not_read", "skipped", "failed", "already_in_project"];
export const REASONS: readonly FileReason[] = [
  "not_readable",
  "not_imported",
  "access_lost",
  "drive_unavailable",
  "read_failed",
  "store_failed",
  "source_failed",
  "unknown",
  "already_in_project",
];
/** The groups of the details modal, in order (INT-SRC-0002): problems first, then work in progress, then Done. */
export type FileGroup = "problem" | "processing" | "waiting" | "done";
export const GROUP_ORDER: readonly FileGroup[] = ["problem", "processing", "waiting", "done"];
/** Rows per page of the file list (INT-SRC-0002). */
export const PAGE_LIMIT = 200;

/** DATA-SRC-0001 table 1, column "Problem". */
export const isProblem = (state: FileState): boolean =>
  state === "not_read" || state === "skipped" || state === "failed" || state === "already_in_project";
/** Not final: the file is still to be worked on. */
const isOpenFile = (state: FileState): boolean => state === "waiting" || state === "processing";

export const groupOf = (state: FileState): FileGroup => {
  if (isProblem(state)) return "problem";
  if (state === "processing") return "processing";
  return state === "waiting" ? "waiting" : "done";
};

type Counted = Pick<Source, "total" | "counts">;

/** Files that reached a final state (everything except Waiting and Processing). */
export const finishedOf = (s: Counted): number => s.total - s.counts.waiting - s.counts.processing;

/** Finished files divided by all files; 0 while the source lists its files and for a source without files. */
export function progressOf(s: Counted & Pick<Source, "listing">): number {
  if (s.listing || s.total === 0) return 0;
  return finishedOf(s) / s.total;
}

/**
 * DATA-SRC-0002, in this order: Failed (the source failed, or it has files and every one failed); Processing (it still lists, or a
 * file is Waiting or Processing); Done with problems (all finished, one is a problem file); Done (also a listed source with no file).
 */
export function sourceState(s: Pick<Source, "failure" | "listing" | "total" | "counts">): SourceState {
  if (s.failure !== null || (s.total > 0 && s.counts.failed === s.total)) return "failed";
  if (s.listing || FILE_STATES.some((f) => isOpenFile(f) && s.counts[f] > 0)) return "processing";
  if (FILE_STATES.some((f) => isProblem(f) && s.counts[f] > 0)) return "done_with_problems";
  return "done";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;
const two = (n: number): string => String(n).padStart(2, "0");

/** Date and time in the user's local time, 24 h: "21 Oct, 10:42" (DATA-SRC-0003). */
export function fmtStamp(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getDate())} ${MONTHS[d.getMonth()]}, ${two(d.getHours())}:${two(d.getMinutes())}`;
}

/** The name of a source as the user reads it: the folder or repository name, or "Local files 21 Oct, 10:42" with " (2)" for a same-minute batch. */
export function fullSourceName(ref: Pick<SourceRef, "kind" | "name" | "nameSuffix" | "addedAt">): string {
  if (ref.kind !== "upload") return ref.name;
  const suffix = ref.nameSuffix > 1 ? ` (${String(ref.nameSuffix)})` : "";
  return `Local files ${fmtStamp(ref.addedAt)}${suffix}`;
}
