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
/** Who wrote a task or area: a planner model, or the fallback rules when the model's answer stayed invalid. */
export type PlannedBy = "model" | "rules";
export type ReviewSeverity = "blocking" | "advisory";
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
/** A member of an area the planner holds as no code work (a process, governance or policy rule). */
export interface NotBuildable {
  specId: string;
  name: string | null;
  reason: string; // the reason code (process, governance, release_gate, policy_only)
  note: string | null;
}
export interface PlanArea {
  areaId: string;
  kind: AreaKind;
  title: string | null;
  description: string | null;
  plannedBy: PlannedBy | null;
  notBuildable: NotBuildable[];
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
  plannedBy: PlannedBy;
  body: string; // DERIVED on read (taskMarkdown)
  version: number; // the task revision
}
/** One finding of the run's review (advisory: it changed nothing). */
export interface PlanReviewIssue {
  areaId: string;
  taskId: string | null;
  problem: string;
  severity: ReviewSeverity;
}
/** How the run that wrote the plan went: fallbacks per planner, review counts, unplaced nodes, oversized tasks. */
export interface PlanQuality {
  runId: string;
  status: "succeeded" | "partially_succeeded";
  fallbacks: Partial<Record<"describe" | "project" | "tasks", number>>;
  review: Partial<Record<ReviewSeverity, number>>;
  reviewFailed: boolean;
  unplaced: number;
  oversized: number;
}
export interface PlanSnapshot {
  planId: string;
  areas: PlanArea[];
  stories: PlanStory[];
  tasks: PlanTask[]; // phase order, then area, story and id
  quality: PlanQuality | null; // null when the run's result is gone
  review: PlanReviewIssue[];
}
export type PlanState =
  | { phase: "idle"; failure?: string } // Stage 0; `failure` = the error code of a run that failed before a plan existed
  | { phase: "creating"; runId: string; step: number; total: number; label: string; items: StepItems | null } // Stage 1; step 1…total
  | { phase: "ready"; plan: PlanSnapshot }; // Stage 2
/** How far a long step is: `done` of its `total` items (the tasks step: planning parts). */
export interface StepItems {
  done: number;
  total: number;
}
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
