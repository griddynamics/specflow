// INT-SRC-0001, INT-SRC-0002 (sources-ux SPECS §4.1, §4.2): the strict parse of the v2 sources API bodies. A body that is not exactly
// the contract is `invalid_response` (an old API answers the legacy shape, SPECS §9). The key lists are checked against
// backend/src/api/fixtures/sources-contract.json.
import {
  CATEGORY_ORDER,
  type DriveCategory,
  type DriveReason,
  type FileCategory,
  type FilePage,
  type FileReason,
  type FileState,
  type Source,
  type SourceFile,
  type SourceRef,
} from "@/lib/domain/sources";
import { FILE_STATES, REASONS } from "@/lib/domain/source-state";
import { expectShape, isRecord, isString } from "./errors";

export const SOURCE_FIELDS = [
  "id",
  "kind",
  "name",
  "name_suffix",
  "added_by",
  "created_at",
  "listing_finished",
  "open",
  "total",
  "counts",
  "failure_reason",
  "folder_id",
  "link",
] as const;
export const FILE_FIELDS = ["id", "name", "category", "state", "reason", "holder"] as const;
export const HOLDER_FIELDS = ["id", "kind", "name", "name_suffix", "created_at"] as const;
/** INT-SRC-0004: what one added file can come back as. */
export const BATCH_OUTCOMES = ["added", "already_in_project", "read_again", "duplicate"] as const;
export type BatchOutcome = (typeof BATCH_OUTCOMES)[number];

const DRIVE_REASONS: ReadonlySet<string> = new Set<DriveReason>([
  "invalid_link",
  "no_access",
  "not_a_folder",
  "already_connected",
  "drive_unavailable",
  "setup_error",
  "too_slow",
  "unknown_error",
]);
const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;
const hasKeys = (v: unknown, keys: readonly string[]): v is Record<string, unknown> =>
  isRecord(v) && keys.every((k) => Object.hasOwn(v, k));

/** `{listing_finished, total, finished}` of INT-GDUI-0004. */
export function toProgress(v: unknown): { listingFinished: boolean; total: number; finished: number } | null {
  if (!isRecord(v) || typeof v.listing_finished !== "boolean" || !isCount(v.total) || !isCount(v.finished)) return null;
  return { listingFinished: v.listing_finished, total: v.total, finished: v.finished };
}

function toRef(v: Record<string, unknown>, addedAt: unknown): SourceRef {
  expectShape(
    (v.kind === "gdrive" || v.kind === "upload") &&
      isString(v.id) &&
      v.id !== "" &&
      isString(v.name) &&
      typeof v.name_suffix === "number" &&
      Number.isInteger(v.name_suffix) &&
      v.name_suffix >= 1 &&
      isString(addedAt),
  );
  return {
    id: v.id,
    kind: v.kind,
    name: v.name,
    nameSuffix: v.name_suffix,
    addedAt: addedAt,
  };
}

/** A source of a kind this client does not know (a newer API) is skipped by the status instead of rejecting it. */
export const isUnknownKind = (v: unknown): boolean => isRecord(v) && isString(v.kind) && v.kind !== "gdrive" && v.kind !== "upload";

/** `SourceSummaryOut` -> `Source`. A failure reason outside the Drive set reads as `unknown_error`. */
export function toSource(v: unknown): Source {
  expectShape(hasKeys(v, SOURCE_FIELDS));
  const by = v.added_by;
  expectShape(isRecord(by) && isString(by.id) && isString(by.display_name));
  expectShape(typeof v.listing_finished === "boolean" && typeof v.open === "boolean" && isCount(v.total) && isRecord(v.counts));
  expectShape(v.failure_reason === null || isString(v.failure_reason));
  expectShape((v.folder_id === null || isString(v.folder_id)) && (v.link === null || isString(v.link)));
  const counts: Record<FileState, number> = {
    waiting: 0,
    processing: 0,
    done: 0,
    not_read: 0,
    skipped: 0,
    failed: 0,
    already_in_project: 0,
  };
  let sum = 0;
  for (const state of FILE_STATES) {
    const n = v.counts[state];
    expectShape(isCount(n));
    counts[state] = n;
    sum += n;
  }
  const total = Math.max(v.total, sum); // the contract says sum(counts) == total; a rolling-deploy mismatch is accepted (total = max)
  const failure = driveFailureOf(v.failure_reason);
  return {
    ...toRef(v, v.created_at),
    addedBy: { id: by.id, name: by.display_name },
    listing: !v.listing_finished,
    open: v.open,
    total,
    counts,
    failure,
    github: null,
  };
}

/** A source's failure reason: none, a known Drive reason, or `unknown_error` for any other text. */
function driveFailureOf(reason: string | null): DriveReason | null {
  if (reason === null) return null;
  return DRIVE_REASONS.has(reason) ? (reason as DriveReason) : "unknown_error";
}

/** A file's reason: none, a known reason, or `unknown` for any other text (DATA-SRC-0001.AC4). */
function fileReasonOf(reason: string | null): FileReason | null {
  if (reason === null) return null;
  return (REASONS as readonly string[]).includes(reason) ? (reason as FileReason) : "unknown";
}

function toHolder(v: unknown): SourceRef | null {
  if (v === null) return null;
  expectShape(hasKeys(v, HOLDER_FIELDS));
  return toRef(v, v.created_at);
}

/** One file row. An unknown state is a wrong shape; an unknown reason reads `unknown` (DATA-SRC-0001.AC4); an unknown category reads Other files. */
export function toFile(v: unknown): SourceFile {
  expectShape(hasKeys(v, FILE_FIELDS) && isString(v.id) && v.id !== "" && isString(v.name) && isString(v.state) && isString(v.category));
  expectShape((FILE_STATES as readonly string[]).includes(v.state));
  expectShape(v.reason === null || isString(v.reason));
  const category: FileCategory =
    v.category === "markdown" || (CATEGORY_ORDER as string[]).includes(v.category)
      ? (v.category as DriveCategory | "markdown")
      : "other_files";
  const reason = fileReasonOf(v.reason);
  return { id: v.id, name: v.name, category, state: v.state as FileState, reason, holder: toHolder(v.holder) };
}

/** `GET .../files` body. */
export function toFilePage(body: unknown): FilePage {
  expectShape(isRecord(body) && Array.isArray(body.items) && (body.next_cursor === null || isString(body.next_cursor)));
  return { source: toSource(body.source), items: body.items.map(toFile), nextCursor: body.next_cursor };
}
