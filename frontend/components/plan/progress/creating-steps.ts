// FE-PLAN-04.AC2 (P19-12, TECH-SPECS §2.15): the pure part of Stage 1. The labels are never imported: each arrives with a run state
// (`step` and `label` of the `creating` state, fed by the port events), so the list grows one label per event, like SpecBot's ToolSteps.
import type { ToolStep } from "@/components/ui/ToolSteps";
import type { StepItems } from "@/lib/domain/plan";

export type KnownLabels = Record<number, string>;

/** The labels seen so far: `run.step` (1-based) now has `run.label`. Returns the same object when nothing changed. */
export function rememberLabel(known: KnownLabels, step: number, label: string): KnownLabels {
  return known[step] === label ? known : { ...known, [step]: label };
}

/** "n of m parts" of a long step, as the running row shows it. */
export const itemsText = (items: StepItems): string => `${String(items.done)} of ${String(items.total)} parts`;

/**
 * The rows of the step list: every known label up to `step`; the ones before it are done, the current one is running. The running
 * row of a long step also shows how many of its items are done ("Writing the tasks · 84 of 105 parts"); the live region keeps the
 * bare label, so a screen reader hears each step once, not every count.
 */
export function stepRows(known: KnownLabels, step: number, items: StepItems | null = null): ToolStep[] {
  const rows: ToolStep[] = [];
  for (let i = 1; i <= step; i++) {
    const label = known[i];
    if (!label) continue;
    const running = i === step;
    rows.push({ label: running && items ? `${label} · ${itemsText(items)}` : label, status: running ? "running" : "done" });
  }
  return rows;
}

/**
 * The bar: the finished steps over the total, plus the done share of the running step's items. The last step stays below 100 % until
 * the plan is ready (the stage then changes).
 */
export function progressValue(step: number, total: number, items: StepItems | null = null): number {
  if (total <= 0) return 0;
  const within = items && items.total > 0 ? items.done / items.total : 0;
  return Math.max(0, Math.min(100, ((step - 1 + Math.min(within, 0.99)) / total) * 100));
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
