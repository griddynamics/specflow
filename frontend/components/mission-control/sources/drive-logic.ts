// Google Drive source (GDUI units): the pure rules and the exact screen texts of the Drive modal, the Drive card and the Build
// knowledge lock. No React. Texts are the units' texts, word for word, except the
// product name: the app writes "Specflow" (brand rule D21), the units spell it differently in two texts.
import {
  CATEGORY_LABEL,
  CLASS_ORDER,
  type DriveCategoryCount,
  type DriveReason,
  type DriveTestResult,
  type ImportProgress,
  type ProcessingClass,
} from "@/lib/domain/sources";
import { COPY } from "@/lib/copy";

// FR-GDUI-0002
export const EMAIL_LOADING = COPY.loadingEmail;
export const EMAIL_UNAVAILABLE = "Drive access is not available right now. Try again later.";
// FR-GDUI-0003
export const COPY_FALLBACK_HINT = "Press Ctrl+C or Cmd+C to copy";
export const COPIED_MS = 2000;
// FR-GDUI-0007: the four steps; `{email}` marks the Drive account email in step 2.
export const GUIDE_TOGGLE = "How to share a folder (4 steps)";
export const GUIDE_STEPS: { text: string; image: string }[] = [
  { text: "In Google Drive, open the menu of the folder and choose Share → Share.", image: "1-share-with-us.png" },
  { text: "Add {email}, set the role to Viewer, and click Send.", image: "2-add-specflow-sa.png" },
  {
    text: "Google warns that the account is outside Grid Dynamics. This is Specflow's read-only account. Click Share anyway.",
    image: "3-confirm-outside-user.png",
  },
  { text: "Open the folder menu again, choose Share → Copy link, and paste the link below.", image: "4-copy-link.png" },
];
// NFR-REL-0001, NFR-REL-0002: the UI owns the cutoffs.
export const TEST_DEADLINE_MS = 60_000;
export const SAVE_DEADLINE_MS = 30_000;
export const SAVE_TOO_LONG = "Saving took too long. Check Mission Control before you save again.";

/** FR-GDUI-0012: the plain message of a reason. `email` = the Drive account email; without it (not read) the message names "the Drive account". */
export function driveReasonMessage(reason: DriveReason, email: string | null): string {
  switch (reason) {
    case "invalid_link":
      return "This is not a Google Drive folder link.";
    case "no_access":
      return `We cannot open this folder. Check that it is shared with ${email ?? "the Drive account"} as Viewer, then test again.`;
    case "not_a_folder":
      return "This link opens a file, not a folder. Paste the link of the folder.";
    case "already_connected":
      return "This folder is already a source of this project.";
    case "drive_unavailable":
      return "Google Drive did not answer. Try again in a minute.";
    case "setup_error":
      return "Drive access is not set up on this server. Contact the Specflow team.";
    case "too_slow":
      return "Checking this folder took too long. Try again, or link a smaller folder.";
    case "unknown_error":
      return "Something went wrong. Try again.";
  }
}

const FLAG: Record<ProcessingClass, string> = { processed: "", stored_only: " (stored, not read yet)", skipped: " (skipped)" };
const CLOSING = "Only Google Docs, Slides, Sheets and PDFs become knowledge for now.";
export const NOTHING_TO_IMPORT = "There is nothing to import from this folder.";
const count = (n: number, one: string, many: string) => `${String(n)} ${n === 1 ? one : many}`;

/** FR-GDUI-0010: bullets by class, then count (highest first), then label A to Z; categories without files are left out. */
export function summaryBullets(
  categories: DriveCategoryCount[],
): { label: string; count: number; text: string; processingClass: ProcessingClass }[] {
  return categories
    .filter((c) => c.count > 0)
    .map((c) => ({
      label: CATEGORY_LABEL[c.category],
      count: c.count,
      processingClass: c.processingClass,
      text: `${CATEGORY_LABEL[c.category]}: ${String(c.count)}${FLAG[c.processingClass]}`,
    }))
    .toSorted(
      (a, b) =>
        CLASS_ORDER.indexOf(a.processingClass) - CLASS_ORDER.indexOf(b.processingClass) ||
        b.count - a.count ||
        a.label.localeCompare(b.label),
    );
}

export interface DriveSummary {
  folderName: string;
  headline: string;
  bullets: string[];
  /** FR-GDUI-0011: shown when at least one bullet has a flag. */
  closing: string | null;
  /** FR-GDUI-0013: no file of the class processed or stored only; Save is not offered. */
  nothingToImport: boolean;
}

export function driveSummary(r: Extract<DriveTestResult, { ok: true }>): DriveSummary {
  const bullets = summaryBullets(r.categories);
  const files = r.categories.reduce((n, c) => n + c.count, 0);
  return {
    folderName: r.folderName,
    headline: `We found ${count(files, "file", "files")} in ${count(r.folderCount, "folder", "folders")}.`,
    bullets: bullets.map((b) => b.text),
    closing: bullets.some((b) => b.processingClass !== "processed") ? CLOSING : null,
    nothingToImport: !r.categories.some((c) => c.count > 0 && c.processingClass !== "skipped"),
  };
}

// FR-SRC-0003 (the card says "Listing files…") and FR-GDUI-0021 (the Build knowledge lock)
export const LISTING = "Listing files…";
export const importedLine = (p: ImportProgress): string => `${String(p.finished)} of ${String(p.total)} files imported`;

const LOCK = "Build knowledge starts after all files are imported.";
/** The tooltip of the unavailable Build knowledge button; null = nothing to wait for. */
export function importLock(p: ImportProgress): string | null {
  if (!p.listingFinished) return `${LOCK} ${LISTING}`;
  if (p.finished < p.total) return `${LOCK} ${importedLine(p)}.`;
  return null;
}

export type Settled<T> = { kind: "ok"; value: T } | { kind: "timeout" } | { kind: "lost" };
/**
 * The answer of a port call, or "timeout" when there is none after `ms` (NFR-REL-0001/0002: the UI owns the cutoffs), or "lost" when
 * the call rejects (a lost connection, an API error, an expired sign-in: FR-GDUI-0012 shows them as "Unknown error"). Settles once.
 */
export function settle<T>(p: Promise<T>, ms: number): Promise<Settled<T>> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      resolve({ kind: "timeout" });
    }, ms);
    p.then(
      (value) => {
        clearTimeout(timer);
        resolve({ kind: "ok", value });
      },
      () => {
        clearTimeout(timer);
        resolve({ kind: "lost" });
      },
    );
  });
}
