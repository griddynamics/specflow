// FE-PLAN-06, FE-PLAN-07 (TECH-SPECS §2.15): pure Plan logic.
// No React, no fetch, no fixtures.
import { PHASE_ORDER, type PlanTask, type TaskPhase } from "@/lib/domain/plan";

/** Phases in `PHASE_ORDER`, empty phases dropped, each group in input order. */
export function groupByPhase(tasks: PlanTask[]): { phase: TaskPhase; tasks: PlanTask[] }[] {
  return PHASE_ORDER.map((phase) => ({ phase, tasks: tasks.filter((t) => t.phase === phase) })).filter((g) => g.tasks.length > 0);
}

/** The task fields the Markdown body is made of. */
export type TaskText = Pick<PlanTask, "title" | "description" | "doneWhen" | "implements" | "waitsFor">;

/**
 * The read-only Markdown of a task: `# title`, the description, `## Done when`, then the sections that come from graph edges,
 * `## Implements` (spec nodes by name) and `## Depends on` (prerequisite tasks by title, else by id). Empty sections are left out.
 */
export function taskMarkdown(task: TaskText, titles: ReadonlyMap<string, string>): string {
  const parts = [`# ${task.title}`];
  if (task.description.trim() !== "") parts.push(task.description.trim());
  if (task.doneWhen.length > 0) parts.push(["## Done when", ...task.doneWhen.map((d) => `- ${d}`)].join("\n"));
  if (task.implements.length > 0) {
    const lines = task.implements.map((i) => {
      const name = i.name ?? i.specId;
      return i.label === null || i.label === "" ? `- ${name}` : `- ${name} (${i.label})`;
    });
    parts.push(["## Implements", ...lines].join("\n"));
  }
  if (task.waitsFor.length > 0) parts.push(["## Depends on", ...task.waitsFor.map((id) => `- ${titles.get(id) ?? id}`)].join("\n"));
  return `${parts.join("\n\n")}\n`;
}
