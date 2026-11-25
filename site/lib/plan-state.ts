import { EDGES, NODES, type NodeId } from "./how-graph";

export type Status = "done" | "prog" | "ready" | "wait" | "block";

export const AGENTS = ["Claude Code", "Cursor", "Codex"];

/** The item that waits for an answer until the open question is answered. */
export const BLOCKED_BY_QUESTION: NodeId = "n6";

export interface PlanState {
  status: Record<NodeId, Status>;
  /** Which agent holds an item that is in progress. */
  holder: Partial<Record<NodeId, string>>;
  answered: boolean;
}

const ids = NODES.map((n) => n.id);
const parents = (id: NodeId): NodeId[] => EDGES.filter(([, b]) => b === id).map(([a]) => a);

/** Ready items, in graph order, go to idle agents; an item is ready when everything it depends on is done and no open question blocks it. */
function settle(state: PlanState): PlanState {
  const status = { ...state.status };
  for (const id of ids) {
    if (status[id] === "done" || status[id] === "prog") continue;
    const blocked = id === BLOCKED_BY_QUESTION && !state.answered;
    const depsDone = parents(id).every((p) => status[p] === "done");
    status[id] = blocked ? "block" : depsDone ? "ready" : "wait";
  }
  const holder = { ...state.holder };
  const busy = new Set(Object.values(holder));
  for (const agent of AGENTS.filter((a) => !busy.has(a))) {
    const id = ids.find((x) => status[x] === "ready");
    if (id) {
      status[id] = "prog";
      holder[id] = agent;
    }
  }
  return { ...state, status, holder };
}

/** The start: the three areas are done, everything else follows from them. */
export function initialPlan(): PlanState {
  const status = Object.fromEntries(ids.map((id) => [id, "wait"])) as Record<NodeId, Status>;
  for (const id of ["n1", "n2", "n3"] as const) status[id] = "done";
  return settle({ status, holder: {}, answered: false });
}

/** The agents finish what they hold and claim the next ready items. */
export function nextRound(state: PlanState): PlanState {
  const status = { ...state.status };
  for (const id of ids) if (status[id] === "prog") status[id] = "done";
  return settle({ ...state, status, holder: {} });
}

export function answerQuestion(state: PlanState): PlanState {
  return settle({ ...state, answered: true });
}

export function count(state: PlanState, wanted: Status): number {
  return ids.filter((id) => state.status[id] === wanted).length;
}

/** The status line under the plan figure. */
export function planSummary(state: PlanState): string {
  if (count(state, "done") === ids.length) return "Everything is built.";
  const working = count(state, "prog");
  const blocked = count(state, "block") ? ", 1 blocked by an open question" : "";
  return `${working} agent${working === 1 ? "" : "s"} working in parallel. ${count(state, "ready")} ready, ${count(state, "wait")} waiting${blocked}.`;
}
