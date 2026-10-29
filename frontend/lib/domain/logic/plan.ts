// FE-PLAN-02, FE-PLAN-06 (TECH-SPECS §2.15; AUTO-17, AUTO-21): pure Plan logic.
// No React, no fetch, no fixtures.
import { PHASE_ORDER, type PlanTask, type TaskPhase } from "@/lib/domain/plan";

/** Phases in `PHASE_ORDER`, empty phases dropped, each group in input order (AUTO-21). */
export function groupByPhase(tasks: PlanTask[]): { phase: TaskPhase; tasks: PlanTask[] }[] {
  return PHASE_ORDER.map((phase) => ({ phase, tasks: tasks.filter((t) => t.phase === phase) })).filter((g) => g.tasks.length > 0);
}

/** The text before the first ". "; else the description without a final ".". */
export function plainName(description: string): string {
  const i = description.indexOf(". ");
  if (i >= 0) return description.slice(0, i);
  return description.endsWith(".") ? description.slice(0, -1) : description;
}
