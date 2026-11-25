// What the Gaps hub shows, from the gap run (`gaps.check`) and one page of `gaps.list`: how many were found, how many are answered, how many
// of the open ones block work. Gaps are not answered in the app (D10): this is only the count. Pure.
import { GAP_KINDS, GAP_SEVERITIES, GAP_STATUSES } from "@/lib/domain/gaps";
import type { Gap, GapCheck, GapKind, GapPage, GapQuery, GapSeverity } from "@/lib/domain/gaps";
import type { GapRow, GapsSummary } from "./canvas-types";

/** Every status, so the page counts answered gaps too; the biggest page the API gives. */
export const SUMMARY_QUERY: GapQuery = { status: [...GAP_STATUSES], limit: 200 };

const settled = (g: Gap): boolean => g.status !== "open";

/** The open gaps for the read-only list: blockers first, then in the order the API gave them. */
export function openGapRows(page: GapPage): GapRow[] {
  const rows = page.gaps
    .filter((g) => g.status === "open")
    .map((g): GapRow => ({ id: g.id, title: g.title, kind: g.kind, severity: g.severity }));
  return rows.toSorted((a, b) => Number(b.severity === "blocker") - Number(a.severity === "blocker"));
}

export function summarizeGaps(check: GapCheck, page: GapPage): GapsSummary {
  return {
    phase: check.phase,
    stepsDone: check.stepsDone,
    stepsTotal: check.stepsTotal,
    total: page.gaps.length,
    answered: page.gaps.filter(settled).length,
    critical: page.gaps.filter((g) => g.status === "open" && g.severity === "blocker").length,
    more: page.next !== null,
  };
}

/** The open gaps counted by category (kind) and severity: a column for each severity some gap has, and a Total column only when there are
 * two or more; the categories with the most gaps first, then in the API's order of kinds. */
export interface GapCounts {
  severities: GapSeverity[];
  withTotal: boolean;
  rows: { kind: GapKind; counts: number[] }[];
}

export function gapCounts(rows: readonly GapRow[]): GapCounts {
  const severities = GAP_SEVERITIES.filter((s) => rows.some((g) => g.severity === s));
  const withTotal = severities.length > 1;
  const counted = GAP_KINDS.map((kind) => {
    const ofKind = rows.filter((g) => g.kind === kind);
    const counts = severities.map((s) => ofKind.filter((g) => g.severity === s).length);
    return { kind, counts: withTotal ? [...counts, ofKind.length] : counts, total: ofKind.length };
  }).filter((r) => r.total > 0);
  return {
    severities,
    withTotal,
    rows: counted.toSorted((a, b) => b.total - a.total).map(({ kind, counts }) => ({ kind, counts })),
  };
}
