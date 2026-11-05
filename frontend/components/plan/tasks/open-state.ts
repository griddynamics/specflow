// P26 (P24 QA m4): which areas, stories and tasks are open on the Plan tab, kept in module memory for the session, so Plan -> Develop -> Plan
// keeps them. In-memory only (no browser storage, FE-PLAN-09.AC7); a reload starts empty. A new creation run forgets it (`CreatingView`
// calls `forgetOpen`), so after a project switch or a second plan the first area opens again (FE-PLAN-06.AC5).

export interface OpenState {
  areas: ReadonlySet<string>;
  stories: ReadonlySet<string>;
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
