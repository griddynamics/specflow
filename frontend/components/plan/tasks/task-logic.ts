// FE-PLAN-06, FE-PLAN-07 (TECH-SPECS §2.15; G-PLAN-2, G-PLAN-5; DECISIONS P24-*): pure helpers of the Plan tab. No React. The grouping and the
// order come from `lib/domain/logic/plan.ts` (`groupByPhase`); nothing here sorts.
import type { PlanTask, TaskPhase, TaskStatus } from "@/lib/domain/plan";

/** Plain phase names (MOCKS §15.8 / PLAN FE-PLAN-06.AC2), one per plan phase. */
export const PHASE_NAMES: Record<TaskPhase, string> = {
  setup: "Set up",
  contract: "Contracts",
  data: "Data model",
  logic: "Logic",
  integration: "Integration",
  quality: "Quality",
  acceptance: "Acceptance",
};

export type StatusTone = "success" | "info" | "neutral" | "warning" | "danger";

/** G-PLAN-5: the status word and its Tag tone. `superseded` and `retired` tasks are history and never listed: null. */
export const STATUS_TAGS: Partial<Record<TaskStatus, { label: string; tone: StatusTone }>> = {
  completed: { label: "Completed", tone: "success" },
  in_progress: { label: "In progress", tone: "info" },
  pending: { label: "Pending", tone: "neutral" },
  blocked: { label: "Blocked", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
};

/** The tasks the UI shows: `superseded` and `retired` are filtered, the order is kept. */
export const visibleTasks = (tasks: PlanTask[]): PlanTask[] => tasks.filter((t) => STATUS_TAGS[t.status] !== undefined);

/** "n of m done": n = completed tasks of the phase. */
export function phaseProgress(tasks: Pick<PlanTask, "status">[]): { done: number; total: number; percent: number } {
  const done = tasks.filter((t) => t.status === "completed").length;
  const total = tasks.length;
  return { done, total, percent: total === 0 ? 0 : (done / total) * 100 };
}

/** Offset of the `## Implements` heading line: the body from there on comes from graph edges and is read-only (as the port's rule). */
export function graphSectionsStart(body: string): number {
  const m = /^## Implements[ \t]*$/m.exec(body);
  return m ? m.index : body.length;
}

/** True when an edit range starts in the read-only graph sections. */
export const isGraphBlock = (body: string, block: { start: number }): boolean => block.start >= graphSectionsStart(body);

/** The title block: the first line of the body, `# Title`. Its source starts with the Markdown `# ` prefix (P26 m3: the editor hides it). */
export function isTitleBlock(body: string, block: { start: number }): boolean {
  const m = /^\s*# /.exec(body);
  return !!m && block.start === m[0].length - 2;
}

/** What the editor shows for a block: the title without its `# ` prefix, any other block as it is. */
export const editorText = (body: string, block: { start: number }, source: string): string =>
  isTitleBlock(body, block) ? source.replace(/^# /, "") : source;

/** What is saved for a block: a plain title gets its `# ` back; a title typed with `# ` (or `#`) is kept as typed. */
export const savedText = (body: string, block: { start: number }, text: string): string =>
  isTitleBlock(body, block) && !text.startsWith("#") ? `# ${text}` : text;

/** The comments-port path of a task's threads (TECH-SPECS §2.15, DECISIONS P27-3): `plan:<taskId>`. It is not a Files path, so no Files view lists them. */
export const taskCommentPath = (taskId: string): string => `plan:${taskId}`;
