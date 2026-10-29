// FE-GDUI units (GDRIVE plan 07; architecture-notes A4, B4, B5): the production `SourcesPort` against the sources API.
//  - FR-GDUI-0002 identity, FR-GDUI-0009 check, FR-GDUI-0017 save: the answer is data. The envelope `error.code` (or the check's
//    `reason`) becomes a `DriveReason`; the HTTP status and the server `message` are never read (INT-GDUI-0002, NFR-SEC-0001).
//    Anything that is not a Drive reason (401, 404, a network failure, an HTML error page, a wrong shape) is `unknown_error`
//    (FR-GDUI-0012.AC5). The UI owns the 60 s check and 30 s save cutoffs; nothing here times out.
//  - K3: GitHub is not offered (`githubEnabled: false`).
//  - sources-ux (SPECS §7.3): `files` (INT-SRC-0002), `remove` (INT-SRC-0003) and `upload` = start, add each file, end (INT-SRC-0004).
//    `changed` tells the knowledge port after start, each add, end and remove, and after a saved Drive folder (FR-SRC-0024).
//  - Links are never logged or rebuilt: `SourceOut.link` is used as it comes.
import {
  addBatchFile,
  endBatch,
  getDriveIdentity,
  listSourceFiles,
  removeSource,
  saveDriveFolder,
  startBatch,
  testDriveFolder,
  ApiError,
} from "@/lib/api";
import { DataError, isDataError } from "@/lib/domain/errors";
import {
  CATEGORY_ORDER,
  CLASS_ORDER,
  type Connection,
  type DriveCategory,
  type DriveCategoryCount,
  type DriveIdentity,
  type DriveReason,
  type DriveSaveResult,
  type DriveTestResult,
  type ProcessingClass,
  type SourcesPort,
  type UploadReport,
} from "@/lib/domain/sources";
import { expectShape, guard, isRecord, isString } from "./errors";
import { BATCH_OUTCOMES, toFilePage, toProgress, type BatchOutcome } from "./sources-wire";

const REASONS: ReadonlySet<string> = new Set<DriveReason>([
  "invalid_link",
  "no_access",
  "not_a_folder",
  "already_connected",
  "drive_unavailable",
  "setup_error",
  "too_slow",
  "unknown_error",
]);
export const isDriveReason = (v: unknown): v is DriveReason => typeof v === "string" && REASONS.has(v);

/** The reason of a failed call: the envelope code when it is one of the closed set, else `unknown_error`. Never the status. */
export const reasonOfError = (e: unknown): DriveReason => (e instanceof ApiError && isDriveReason(e.code) ? e.code : "unknown_error");

const isCategory = (v: unknown): v is DriveCategory => typeof v === "string" && (CATEGORY_ORDER as string[]).includes(v);
const isClass = (v: unknown): v is ProcessingClass => typeof v === "string" && (CLASS_ORDER as string[]).includes(v);
const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;

const failureReasonOf = (reason: unknown): DriveReason => (isDriveReason(reason) ? reason : "unknown_error");

/** B4 `SourceOut` -> `Connection` (B5 state map: listing and importing = syncing, done = synced, failed = error). `null` = wrong shape. */
export function toConnection(v: unknown): Connection | null {
  if (!isRecord(v) || v.kind !== "gdrive" || !isString(v.id) || !isString(v.link) || !isString(v.name) || !isString(v.connected_by))
    return null;
  const progress = toProgress(v.progress);
  if (!progress || !isString(v.created_at)) return null;
  const state = connectionStateOf(v.state);
  if (!state) return null;
  const failure = state === "error" ? failureReasonOf(v.failure_reason) : null;
  return {
    id: v.id,
    kind: "gdrive",
    url: v.link, // canonical, from the API
    label: v.name,
    detail: "Folder",
    ownerId: v.connected_by,
    state,
    access: null,
    error: null,
    progress: null,
    counts: { documents: 0, skipped: 0, failed: 0 }, // the lean Drive card shows the import line only
    importProgress: progress,
    failure,
    createdAt: v.created_at,
    lastSyncAt: isString(v.last_synced_at) ? v.last_synced_at : null,
    nextSyncAt: null,
    resumeAt: null,
  };
}

function toTest(body: unknown): DriveTestResult {
  if (!isRecord(body)) return { ok: false, reason: "unknown_error" };
  if (body.result === "failed") return { ok: false, reason: isDriveReason(body.reason) ? body.reason : "unknown_error" };
  const folder = body.folder;
  if (
    body.result !== "ok" ||
    !isRecord(folder) ||
    !isString(folder.id) ||
    !isString(folder.name) ||
    !isCount(body.folder_count) ||
    !isCount(body.file_count) ||
    !Array.isArray(body.categories)
  ) {
    return { ok: false, reason: "unknown_error" };
  }
  const categories: DriveCategoryCount[] = [];
  for (const c of body.categories) {
    if (!isRecord(c) || !isCategory(c.category) || !isClass(c.processing_class) || !isCount(c.count))
      return { ok: false, reason: "unknown_error" };
    categories.push({ category: c.category, processingClass: c.processing_class, count: c.count });
  }
  return { ok: true, folderId: folder.id, folderName: folder.name, folderCount: body.folder_count, fileCount: body.file_count, categories };
}

const isOutcome = (v: unknown): v is BatchOutcome => (BATCH_OUTCOMES as readonly string[]).includes(v as string);
/** A 2xx answer whose body is not JSON: the server took the file, so it reads as an empty body (counted as added). */
const keepOdd2xx = (e: unknown): unknown => {
  if (e instanceof ApiError && e.code === "invalid_response" && e.status >= 200 && e.status < 300) return null;
  throw e;
};
const apiCodeOf = (e: unknown): string | undefined => (isDataError(e) ? e.detail?.apiCode : undefined);

/** Opens a Local files batch and returns its source id; `null` when no batch opened. */
async function openBatch(projectId: string, fileCount: number, changed: (projectId: string) => void): Promise<string | null> {
  try {
    const body: unknown = await guard(() => startBatch(projectId, fileCount));
    // Lenient on purpose: a start body that parses badly still opened a batch, which must be ended.
    return isRecord(body) && isRecord(body.source) && isString(body.source.id) && body.source.id !== "" ? body.source.id : null;
  } catch {
    return null;
  } finally {
    changed(projectId);
  }
}

/** Sends one file; a 503 lock timeout ("try again") is retried once. A refusal rejects. */
async function sendBatchFile(projectId: string, sourceId: string, file: File): Promise<BatchOutcome> {
  let body: unknown;
  try {
    body = await guard(() => addBatchFile(projectId, sourceId, file).catch(keepOdd2xx));
  } catch (e) {
    if (!(isDataError(e) && e.code === "unavailable")) throw e;
    body = await guard(() => addBatchFile(projectId, sourceId, file));
  }
  // A malformed 2xx body: the server kept the file, so it counts as added.
  return isRecord(body) && isOutcome(body.outcome) ? body.outcome : "added";
}

/** FR-SRC-0020/0022: an ended or removed batch, or a lost sign-in, stops the upload. */
function endsBatch(e: unknown): boolean {
  const code = apiCodeOf(e);
  return code === "batch_ended" || code === "source_not_found" || (isDataError(e) && e.code === "unauthenticated");
}

function tally(report: UploadReport, outcome: BatchOutcome): void {
  if (outcome === "added") report.added++;
  else if (outcome === "already_in_project") report.alreadyInProject++;
  else if (outcome === "read_again") report.readAgain++;
  else report.duplicates++;
}

/** B5 state map: listing and importing = syncing, done = synced, failed = error; any other state is a wrong shape (`null`). */
function connectionStateOf(state: unknown): Connection["state"] | null {
  if (state === "done") return "synced";
  if (state === "failed") return "error";
  if (state === "listing" || state === "importing") return "syncing";
  return null;
}

/** `changed` tells the knowledge port that sources or files changed, so the screen reads the status again. */
export function createHttpSources(opts: { changed: (projectId: string) => void }): SourcesPort {
  const unsupported = () => Promise.reject(new DataError("not_available", false)); // GitHub and sync controls: no production backend
  return {
    githubEnabled: false,
    pdfOnly: true,
    add: unsupported,
    syncNow: unsupported,
    addToken: unsupported,
    async files(projectId, sourceId, cursor) {
      const body: unknown = await guard(() => listSourceFiles(projectId, sourceId, cursor));
      return toFilePage(body); // a removed source rejects `not_found` with apiCode `source_not_found`
    },
    async remove(projectId, sourceId) {
      // FR-SRC-0018/0019: success only for 204. A gone source rejects `not_found` with apiCode `source_not_found`, which the screen
      // also treats as removed; a plain `not_found` (an old API's unknown route) carries another apiCode and is a failure.
      try {
        const status = await guard(() => removeSource(projectId, sourceId));
        expectShape(status === 204);
      } finally {
        opts.changed(projectId);
      }
    },
    async upload(projectId, files) {
      const report: UploadReport = {
        sourceId: null,
        picked: files.length,
        added: 0,
        alreadyInProject: 0,
        readAgain: 0,
        duplicates: 0,
        notAdded: files.length,
      };
      const sourceId = await openBatch(projectId, files.length, opts.changed);
      if (sourceId === null) return report; // no batch: nothing was added, the screen names every file
      report.sourceId = sourceId;
      try {
        for (const file of files) {
          try {
            tally(report, await sendBatchFile(projectId, sourceId, file));
          } catch (e) {
            if (endsBatch(e)) break; // a refused file does not stop the others; an ended or removed batch, or a lost sign-in, does
          }
          opts.changed(projectId);
        }
      } finally {
        try {
          await guard(() => endBatch(projectId, sourceId)); // always: an open batch would hold the Build lock for 15 min
        } catch {
          // The server closes an abandoned batch itself (INT-SRC-0004).
        }
        opts.changed(projectId);
      }
      report.notAdded = files.length - report.added - report.alreadyInProject - report.readAgain - report.duplicates;
      return report;
    },
    async driveIdentity(): Promise<DriveIdentity> {
      try {
        const body = await getDriveIdentity();
        return isRecord(body) && isString(body.email) && body.email !== ""
          ? { ok: true, email: body.email }
          : { ok: false, reason: "unknown_error" };
      } catch (e) {
        return { ok: false, reason: reasonOfError(e) };
      }
    },
    async testDrive(projectId, link): Promise<DriveTestResult> {
      try {
        return toTest(await testDriveFolder(projectId, link));
      } catch (e) {
        return { ok: false, reason: reasonOfError(e) };
      }
    },
    async saveDrive(projectId, link): Promise<DriveSaveResult> {
      try {
        const body = await saveDriveFolder(projectId, link);
        const connection = isRecord(body) ? toConnection(body.source) : null;
        opts.changed(projectId); // the source exists on the server even if the answer was odd
        return connection ? { ok: true, connection } : { ok: false, reason: "unknown_error" };
      } catch (e) {
        return { ok: false, reason: reasonOfError(e) };
      }
    },
  };
}
