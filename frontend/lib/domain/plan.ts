// FE-PLAN-12, FE-DATA-01 (TECH-SPECS §2.15): Plan page types and the port, on the model of the backend plan (`graph.plan`,
// docs/graph/plan/): work areas hold stories, stories hold tasks; a task has a phase, a status and a complexity. The http adapter
// maps the API's snake_case bodies (`/projects/{pid}/plan*`) to these types. Types and one constant only.
import type { Unsubscribe } from "./common";
import type { BlockRef } from "./files";

/** The plan phases, in build order (the `phase` vocabulary of the plan ontology). */
export type TaskPhase = "setup" | "contract" | "data" | "logic" | "integration" | "quality" | "acceptance";
/** Task statuses; `superseded` and `retired` tasks are history and the UI does not list them. */
export type TaskStatus = "pending" | "in_progress" | "completed" | "failed" | "blocked" | "superseded" | "retired";
export type Complexity = "low" | "medium" | "high";
export type StoryProgress = "pending" | "in_progress" | "blocked" | "completed";
export type AreaKind = "module" | "project";
export type StoryKind = "feature" | "interface" | "entity" | "core" | "chapter" | "setup" | "closing";
export const PHASE_ORDER: readonly TaskPhase[] = ["setup", "contract", "data", "logic", "integration", "quality", "acceptance"];

/** A spec node that anchors a feature story: what a plan run plans, shown before the first plan. */
export interface PlanFeature {
  specId: string;
  name: string;
}
/** What a plan run would start from: the features and the estimate of the run (null when it is not known). */
export interface PlanPreview {
  features: PlanFeature[];
  calls: number | null;
  inputTokens: number | null;
}
export interface PlanArea {
  areaId: string;
  kind: AreaKind;
  title: string | null;
  description: string | null;
}
export interface PlanStory {
  storyId: string;
  areaId: string;
  kind: StoryKind;
  title: string;
  summary: string | null;
  progress: StoryProgress;
}
export interface PlanTask {
  taskId: string;
  areaId: string;
  storyId: string;
  title: string;
  description: string;
  phase: TaskPhase;
  complexity: Complexity;
  status: TaskStatus;
  doneWhen: string[];
  waitsFor: string[]; // taskIds of the prerequisites
  implements: { specId: string; name: string | null; label: string | null }[];
  commit: string | null;
  blockReason: string | null;
  ready: boolean; // pending with every prerequisite completed (derived by the API)
  body: string; // DERIVED on read (taskMarkdown)
  version: number; // the task revision
}
export interface PlanSnapshot {
  planId: string;
  areas: PlanArea[];
  stories: PlanStory[];
  tasks: PlanTask[]; // phase order, then area, story and id
}
export type PlanState =
  | { phase: "idle"; failure?: string } // Stage 0; `failure` = the error code of a run that failed before a plan existed
  | { phase: "creating"; runId: string; step: number; total: number; label: string } // Stage 1; step 1…total
  | { phase: "ready"; plan: PlanSnapshot }; // Stage 2
export interface PlanEvent {
  type: "plan.started" | "plan.step" | "plan.ready" | "plan.failed";
  runId: string;
}
export interface PlanPort {
  preview(projectId: string): Promise<PlanPreview>; // no finished build → no features, no estimate
  state(projectId: string): Promise<PlanState>; // no plan and no run → idle
  subscribe(projectId: string, listener: (e: PlanEvent, next: PlanState) => void): Unsubscribe;
  /** Starts a run: idle → creating. `replan` re-plans every area of an existing plan ("Recreate Plan"). */
  create(projectId: string, opts?: { replan?: boolean }): Promise<{ runId: string }>;
  leave(projectId: string): void; // stops watching the run; the run itself goes on
  /** Task text editing is not available yet: rejects with `not_available`. */
  updateTaskBody(projectId: string, taskId: string, block: BlockRef, markdown: string, baseVersion: number): Promise<PlanTask>;
}
