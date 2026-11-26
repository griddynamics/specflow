// What the plan destinations over HTTP (GitHub Projects, Linear) read the same way: nullable text, counters, and the last sync pass.
import { expectShape, isRecord, isString } from "./errors";

export const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return v ?? null;
};

export const count = (v: unknown): number => {
  expectShape(typeof v === "number" && Number.isSafeInteger(v) && v >= 0);
  return v;
};

/** `sync` of a plan body: null while no pass ran. `statuses` are the closed values of `status`; `toCounts` reads the destination's counters. */
export function toSync<S extends string, C>(
  v: unknown,
  statuses: readonly S[],
  toCounts: (counts: unknown) => C | null,
): { status: S; queued: boolean; finishedAt: string | null; errorCode: string | null; counts: C | null } | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v) && typeof v.queued === "boolean");
  const status = statuses.find((s) => s === v.status);
  expectShape(status !== undefined);
  const finishedAt = textOrNull(v.finished_at);
  expectShape(finishedAt === null || !Number.isNaN(Date.parse(finishedAt)));
  return { status, queued: v.queued, finishedAt, errorCode: textOrNull(v.error_code), counts: toCounts(v.counts) };
}
