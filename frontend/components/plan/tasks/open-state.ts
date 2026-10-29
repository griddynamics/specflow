// P26 (P24 QA m4): which phases and tasks are open on the Plan tab, kept in module memory for the session, so Plan -> Develop -> Plan
// keeps them. In-memory only (no browser storage, FE-PLAN-09.AC7); a reload starts empty. A new creation run forgets it (`CreatingView`
// calls `forgetOpen`), so after "Reset demo", a project switch or a second plan the first phase opens again (FE-PLAN-06.AC5).
import type { TaskPhase } from "@/lib/domain/plan";

export interface OpenState {
  phases: ReadonlySet<TaskPhase>;
  tasks: ReadonlySet<string>;
}

const memory = new Map<string, OpenState>();

export const recallOpen = (projectId: string): OpenState | undefined => memory.get(projectId);
export const rememberOpen = (projectId: string, state: OpenState): void => {
  memory.set(projectId, state);
};
export const forgetOpen = (projectId: string): void => {
  memory.delete(projectId);
};
