// What the Gaps hub shows, from the gap run (`gaps.check`) and one page of `gaps.list`: how many were found, how many are answered, how many
// of the open ones block work. Gaps are not answered in the app (D10): this is only the count. Pure.
import { GAP_STATUSES, type Gap, type GapCheck, type GapPage, type GapQuery } from "@/lib/domain/gaps";
import type { GapsSummary } from "./canvas-types";

/** Every status, so the page counts answered gaps too; the biggest page the API gives. */
export const SUMMARY_QUERY: GapQuery = { status: [...GAP_STATUSES], limit: 200 };

const settled = (g: Gap): boolean => g.status !== "open";

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
