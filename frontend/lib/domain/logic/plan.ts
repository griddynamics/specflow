// FE-PLAN-06, FE-PLAN-07 (TECH-SPECS §2.15): pure Plan logic.
// No React, no fetch, no fixtures.
import { type PlanArea, type PlanReviewIssue, type PlanSnapshot, type PlanStory, type PlanTask } from "@/lib/domain/plan";

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

/** One story of the outline with its tasks in input order. */
export interface StoryGroup {
  story: PlanStory;
  tasks: PlanTask[];
}
/** One work area of the outline: its stories with tasks, every task of the area, and the review findings about it. */
export interface AreaGroup {
  area: PlanArea;
  stories: StoryGroup[];
  tasks: PlanTask[];
  review: PlanReviewIssue[];
}

/**
 * The plan as work areas, then stories, then tasks: areas and stories in the snapshot's order, the project-wide area last, tasks
 * in input order (phase order). An area or story without a task is left out; a task whose story or area the snapshot does not
 * list is kept under a stand-in named after its id, so no task is lost.
 */
export function groupByArea(plan: Pick<PlanSnapshot, "areas" | "stories" | "review">, tasks: PlanTask[]): AreaGroup[] {
  const areas = new Map(plan.areas.map((a) => [a.areaId, a]));
  const stories = new Map(plan.stories.map((s) => [s.storyId, s]));
  const areaOrder = new Map(plan.areas.map((a, i) => [a.areaId, a.kind === "project" ? plan.areas.length + i : i]));
  const storyOrder = new Map(plan.stories.map((s, i) => [s.storyId, i]));
  const byArea = new Map<string, Map<string, PlanTask[]>>();
  for (const t of tasks) {
    const inArea = byArea.get(t.areaId) ?? new Map<string, PlanTask[]>();
    byArea.set(t.areaId, inArea);
    inArea.set(t.storyId, [...(inArea.get(t.storyId) ?? []), t]);
  }
  const rank = (order: Map<string, number>, id: string) => order.get(id) ?? Number.MAX_SAFE_INTEGER;
  return [...byArea.entries()]
    .toSorted(([a], [b]) => rank(areaOrder, a) - rank(areaOrder, b) || a.localeCompare(b))
    .map(([areaId, inArea]) => {
      const groups = [...inArea.entries()]
        .toSorted(([a], [b]) => rank(storyOrder, a) - rank(storyOrder, b) || a.localeCompare(b))
        .map(([storyId, storyTasks]) => ({ story: stories.get(storyId) ?? standInStory(storyId, areaId), tasks: storyTasks }));
      return {
        area: areas.get(areaId) ?? standInArea(areaId),
        stories: groups,
        tasks: groups.flatMap((g) => g.tasks),
        review: plan.review.filter((r) => r.areaId === areaId),
      };
    });
}

const standInArea = (areaId: string): PlanArea => ({
  areaId,
  kind: "module",
  title: null,
  description: null,
  plannedBy: null,
  notBuildable: [],
});
const standInStory = (storyId: string, areaId: string): PlanStory => ({
  storyId,
  areaId,
  kind: "core",
  title: storyId,
  summary: null,
  progress: "pending",
});
