// FE-PLAN-12, FE-DATA-01 (TECH-SPECS §2.15; AUTO-17, AUTO-18, AUTO-19): Plan page types and the port. Graph-shaped: the mock
// returns what the backend Cypher would return, in camelCase. A UI "Decision" = an open graph Finding; a "Feature" = a
// `:Entity:Feature` node; a phase = `Task.phase`. `Decision` (§2.10) is unchanged (Q7). Types and one constant only (P20; frozen after P20).
import type { Unsubscribe } from "./common";
import type { Severity } from "./decisions";
import type { BlockRef } from "./files";

export type FindingSeverity = Severity; // §2.10; stored on Finding = backend proposal (§4)
export type TaskPhase = "BOOTSTRAP" | "CONTRACT" | "DATA_MODEL" | "LOGIC" | "INTEGRATION" | "QUALITY" | "TEST";
export type TaskStatus = "PENDING" | "IN_PROGRESS" | "COMPLETED" | "FAILED" | "BLOCKED" | "INVALIDATED"; // graph mirror; the port never returns INVALIDATED
export const PHASE_ORDER: readonly TaskPhase[] = ["BOOTSTRAP", "CONTRACT", "DATA_MODEL", "LOGIC", "INTEGRATION", "QUALITY", "TEST"];

export interface PlanTableRow {
  feature: string; // `FEAT::<SLUG>`; a Feature has no title
  description: string;
  counts: Record<FindingSeverity, number>;
  openTotal: number; // distinct open Findings of this Feature
}
export interface PlanTable {
  rows: PlanTableRow[]; // every Feature, zero rows included; fixture order
  distinct: Record<FindingSeverity, number>; // distinct open Findings, whole project, incl. unlinked
  unlinked: number; // open Findings with no Feature through P1/P2
}
export interface PlanTask {
  taskId: string; // graph name `TASK::<COMM>::<SLUG>` (T01–T19 are reading labels only)
  title: string;
  description: string;
  phase: TaskPhase;
  modelTier: "TIER1" | "TIER2" | "TIER3";
  status: TaskStatus;
  doneWhen: string[];
  dependsOn: string[]; // taskIds of the prerequisites
  implements: { name: string; kind: "Feature" | "Requirement" | "Module" | "Capability"; description: string }[];
  community: string; // `COMM::<SLUG>`
  gitCommitHash?: string | null;
  blockedBy?: string | null; // a Finding name `FND::…`
  ready: boolean; // DERIVED on read (AUTO-19, next-task rule only; not an edit gate); never stored
  body: string; // DERIVED on read (renderTaskMarkdown)
  version: number; // mock edit state
}
export interface PlanSnapshot {
  planName: string; // `PLAN::<12 hex>`
  planStatus: "ACTIVE";
  communities: { name: string; title: string }[];
  tasks: PlanTask[]; // whole-plan order (orderTasks)
}
export type PlanState =
  | { phase: "idle" } // Stage 0
  | { phase: "creating"; runId: string; step: number; total: number; label: string } // Stage 1; step 1…total
  | { phase: "ready"; plan: PlanSnapshot }; // Stage 2
export interface PlanEvent {
  type: "plan.started" | "plan.step" | "plan.ready";
  runId: string;
}
export type AgentId = "claude-code" | "cursor" | "codex";
export interface DevelopContent {
  // built in the frontend, not graph data
  agents: { id: AgentId; config: string; teachFile: { path: string; body: string }; run: string }[];
  workflows: { title: string; steps: string[] }[]; // 4 cards
  tools: { name: string; method: string; note: string }[]; // first five MCP tools
  nextTask: { taskId: string; title: string } | null; // nextTasks(plan)[0]; T12 in the demo
}

export interface PlanPort {
  table(projectId: string): Promise<PlanTable>; // no plan content → rows [], distinct all 0, unlinked 0
  state(projectId: string): Promise<PlanState>; // no plan content → idle
  subscribe(projectId: string, listener: (e: PlanEvent, next: PlanState) => void): Unsubscribe;
  create(projectId: string): Promise<{ runId: string }>; // idle → creating; conflict if not idle; not_found without plan content
  leave(projectId: string): void; // creating → idle + clears its timers; idle and ready untouched
  updateTaskBody(projectId: string, taskId: string, block: BlockRef, markdown: string, baseVersion: number): Promise<PlanTask>;
  develop(projectId: string): Promise<DevelopContent>;
}
