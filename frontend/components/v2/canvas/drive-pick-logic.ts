// The list of files under a tested Drive folder (the V2 form), free of React: which files the list shows, what a row says after its name, and
// the status line. The selection itself is the shared model of `pick-logic.ts`.
import type { DriveFile, DriveTestResult } from "@/lib/domain/sources";
import { pickedCount, type Unchecked } from "./pick-logic";

type Tested = Extract<DriveTestResult, { ok: true }>;

/** The files a person can pick: those whose category the import takes. A category the import skips (shortcuts, Forms) is left out of the list;
 * the summary above it already counts them as skipped, and an Add never reads them. The order is the API's: by path, then name. */
export function pickableFiles(tested: Tested): DriveFile[] {
  const skipped = new Set(tested.categories.filter((c) => c.processingClass === "skipped").map((c) => c.category));
  return tested.files.filter((f) => !skipped.has(f.category));
}

/** What a row says after the file name: the folder it is in (`design/archive`), or nothing for a file in the root. */
export const folderHint = (file: DriveFile): string | undefined => (file.path === "" ? undefined : file.path);

const plural = (n: number): string => `${String(n)} ${n === 1 ? "file" : "files"}`;

/** What an Add would take: "12 of 14 files", or "No files" with nothing checked. */
export function selectionText(files: readonly DriveFile[], unchecked: Unchecked): string {
  const picked = pickedCount(files, unchecked);
  return picked === 0 ? "No files" : `${String(picked)} of ${plural(files.length)}`;
}
