// FE-PLAN-04.AC2 (P19-12, TECH-SPECS §2.15): the pure part of Stage 1. The labels are never imported: each arrives with a run state
// (`step` and `label` of the `creating` state, fed by the port events), so the list grows one label per event, like SpecBot's ToolSteps.
import type { ToolStep } from "@/components/ui/ToolSteps";

export type KnownLabels = Record<number, string>;

/** The labels seen so far: `run.step` (1-based) now has `run.label`. Returns the same object when nothing changed. */
export function rememberLabel(known: KnownLabels, step: number, label: string): KnownLabels {
  return known[step] === label ? known : { ...known, [step]: label };
}

/** The rows of the step list: every known label up to `step`; the ones before it are done, the current one is running. */
export function stepRows(known: KnownLabels, step: number): ToolStep[] {
  const rows: ToolStep[] = [];
  for (let i = 1; i <= step; i++) {
    const label = known[i];
    if (label) rows.push({ label, status: i < step ? "done" : "running" });
  }
  return rows;
}

/** The bar: the finished steps over the total. The last step stays below 100 % until the plan is ready (the stage then changes). */
export function progressValue(step: number, total: number): number {
  if (total <= 0) return 0;
  return Math.max(0, Math.min(100, ((step - 1) / total) * 100));
}

/**
 * P26 (R23-09): the lines of the hidden live region, one per label received, in step order. Each label arrives with its own event, so a
 * render that batches two events still holds both lines, and a screen reader is given each added line (`aria-atomic="false"`): no label is
 * skipped, whatever `run.label` shows at that render.
 */
export function announceLines(known: KnownLabels): { step: number; label: string }[] {
  return Object.keys(known)
    .map(Number)
    .toSorted((a, b) => a - b)
    .map((step) => ({ step, label: known[step] }));
}
