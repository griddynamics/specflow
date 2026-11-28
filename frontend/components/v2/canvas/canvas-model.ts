// The canvas view model (AC9: a frontend view model over the existing ports, no new endpoint; D5, D10). Pure: `CanvasInput` in, what the
// canvas shows out. Colour has three jobs (docs/mission-control/README.md "Colour"): grey = not active, ink = working, yellow = a person
// decides now. Yellow belongs to the two decisions (Build or Update graph, Build plan or Replan), never to a status; if both are open the
// graph build keeps the yellow and the plan button is ink. A wire moves only while data moves on it. Nothing here reads a clock but `now`.
import { buildButtonView, plural } from "@/components/shared/sources/kb-logic";
import { sourceState } from "@/lib/domain/source-state";
import type { ProjectSourcesStatus } from "@/lib/domain/sources";
import type { WireMotion } from "@graphico/ui";
import type { CanvasInput, CanvasModel, Decision, HubView, NodeMapper, NodeView, Stages, WireSpec, ZoneId, ZoneView } from "./canvas-types";

/** A gap run that found its gaps. A failed run found none: the plan waits and nothing says "found". */
const GAP_RUN_DONE = new Set(["done", "degraded"]);
const GAP_RUN_WORKING = new Set(["queued", "running"]);
/** The background graph lights up along this curve while the first build runs: decorative, the API reports no build progress (AC10). */
const REVEAL_SECONDS = 120;
const REVEAL_CAP = 0.9;
const ZONES: readonly ZoneId[] = ["graph", "gap", "plan"];
const NAMES: Record<ZoneId, string> = { graph: "Graph", gap: "Gap", plan: "Plan" };
const ZONE_TEXT: Record<ZoneId, { label: string; add: string }> = {
  graph: { label: "01 · Graph", add: "Add source" },
  gap: { label: "02 · Gap", add: "Add channel" },
  plan: { label: "03 · Plan", add: "Add destination" },
};

export function stagesOf(input: CanvasInput): Stages {
  const built = input.status?.ready === true;
  return {
    built,
    graphBuilding: input.status?.build != null || input.pending.build,
    gapsFound: built && input.gaps !== null && GAP_RUN_DONE.has(input.gaps.phase),
    planBuilding: input.plan?.phase === "creating" || input.pending.plan,
    planOn: input.plan?.phase === "ready",
  };
}

/**
 * Sources added, or files removed, after the last build started: what makes the graph due for an update. The sources API keeps the V1 button
 * active after a success ("Update knowledge"), but on the canvas yellow is a decision, so an up-to-date graph shows none (README "Colour").
 */
export function sourcesChanged(status: ProjectSourcesStatus): boolean {
  const since = Date.parse(status.lastBuild?.startedAt ?? "");
  if (Number.isNaN(since)) return false;
  return status.changes.removed > 0 || status.sources.some((s) => Date.parse(s.addedAt) > since);
}

/**
 * Decision 1 comes from the V1 build button's own rules (`buildButtonView`: its mode says whether a build can start and which). The first
 * build is always a decision; an update only when sources changed or the last build failed (a retry).
 */
export function graphDecisionOf(input: CanvasInput): Decision | null {
  const s = input.status;
  if (s === null) return null;
  const view = buildButtonView(s, input.now, input.pending.build);
  if (view.mode === null || view.busy) return null;
  if (view.mode === "first") return { label: "Build graph", yellow: true };
  return s.buildFailure !== null || sourcesChanged(s) ? { label: "Update graph", yellow: true } : null;
}

/** Decision 2: the plan is ready to be decided as soon as the gaps are found, answered or not; with a plan it is a replan. Ink while decision 1 is open. */
export function planDecisionOf(input: CanvasInput, stages: Stages, graphOpen: boolean): Decision | null {
  if (!stages.built || stages.graphBuilding || stages.planBuilding) return null;
  return planLabel(input, stages, !graphOpen);
}

function planLabel(input: CanvasInput, stages: Stages, yellow: boolean): Decision | null {
  if (stages.planOn) return input.planStale ? { label: "Replan", yellow } : null;
  return stages.gapsFound ? { label: "Build plan", yellow } : null;
}

function graphHub(input: CanvasInput, stages: Stages, decision: Decision | null): HubView {
  const base = { id: "graph", label: NAMES.graph, progress: null } as const;
  const s = input.status;
  if (s === null || s.sources.length === 0) return { ...base, state: "off", tooltip: "Add a source to start" };
  const view = buildButtonView(s, input.now, input.pending.build);
  if (stages.graphBuilding) return { ...base, state: "busy", tooltip: view.detail, progress: "unknown" };
  const importing = s.sources.filter((x) => sourceState(x) === "processing").length;
  if (importing > 0) return { ...base, state: "wait", tooltip: `Importing ${String(importing)} of ${plural(s.sources.length, "source")}…` };
  if (decision !== null) return { ...base, state: "ready", tooltip: view.detail, decision };
  return { ...base, state: stages.built ? "on" : "wait", tooltip: view.detail };
}

function gapHub(input: CanvasInput, stages: Stages): HubView {
  const base = { id: "gap", label: NAMES.gap, progress: null } as const;
  const g = input.gaps;
  if (!stages.built) return { ...base, state: "off", tooltip: "Waits for the graph" };
  if (g === null) return { ...base, state: "wait", tooltip: "Reading the gaps" };
  if (g.phase === "failed") return { ...base, state: "wait", tooltip: "The gap run failed" };
  if (g.phase === "never") return { ...base, state: "wait", tooltip: "Waits for the gap run" };
  if (GAP_RUN_WORKING.has(g.phase) || g.phase === "building") {
    const progress = g.stepsTotal > 0 ? g.stepsDone / g.stepsTotal : "unknown";
    return { ...base, state: "busy", tooltip: "Looking for gaps", progress };
  }
  const more = g.more ? "+" : "";
  const tooltip = `${String(g.total)}${more} found · ${String(g.answered)} answered`;
  const pill = g.critical > 0 ? `${String(g.critical)} critical` : undefined;
  return { ...base, state: "on", tooltip, pill };
}

function planHub(input: CanvasInput, stages: Stages, decision: Decision | null): HubView {
  const base = { id: "plan", label: NAMES.plan, progress: null } as const;
  const plan = input.plan;
  if (!stages.built) return { ...base, state: "off", tooltip: "Waits for the graph" };
  if (plan?.phase === "creating") {
    return { ...base, state: "busy", tooltip: "Planning stories and tasks", progress: plan.total > 0 ? plan.step / plan.total : "unknown" };
  }
  if (stages.planBuilding) return { ...base, state: "busy", tooltip: "Planning stories and tasks", progress: "unknown" };
  if (plan?.phase === "ready") {
    const quality = `${plural(plan.plan.stories.length, "story", "stories")} · ${plural(plan.plan.tasks.length, "task")}`;
    if (decision !== null) return { ...base, state: "ready", tooltip: `New information · ${quality}`, decision };
    return { ...base, state: "on", tooltip: quality };
  }
  if (input.gaps?.phase === "failed") return { ...base, state: "wait", tooltip: "The gap run failed; the plan waits" };
  if (!stages.gapsFound) return { ...base, state: "wait", tooltip: "Waits while the gaps are found" };
  const critical = input.gaps?.critical ?? 0;
  const tooltip = critical > 0 ? `Gaps found · ${String(critical)} critical still open · ready to plan` : "Gaps found · ready to plan";
  return { ...base, state: decision === null ? "wait" : "ready", tooltip, ...(decision === null ? {} : { decision }) };
}

/** The share of the background graph that is lit. */
export function revealOf(input: CanvasInput, stages: Stages): number {
  if (stages.built) return 1.01;
  const started = input.status?.build?.startedAt;
  if (!stages.graphBuilding) return 0;
  const t = started == null ? 0 : (input.now.getTime() - Date.parse(started)) / 1000;
  return REVEAL_CAP * (1 - Math.exp(-Math.max(0, Number.isFinite(t) ? t : 0) / REVEAL_SECONDS));
}

const byAddedAt = (a: NodeView, b: NodeView): number => {
  if (a.addedAt === b.addedAt) return 0;
  if (a.addedAt === "") return 1;
  return b.addedAt === "" ? -1 : a.addedAt.localeCompare(b.addedAt);
};

const toGaps = (built: boolean, asking: boolean): WireMotion => {
  if (!built) return "off";
  return asking ? "flow" : "on";
};

const planMotion = (stages: Stages): WireMotion => {
  if (stages.planBuilding) return "flow";
  return stages.planOn ? "on" : "off";
};

function wiresOf(nodes: NodeView[], stages: Stages, input: CanvasInput): WireSpec[] {
  const g = input.gaps;
  const asking = g !== null && GAP_RUN_WORKING.has(g.phase);
  const toPlan = planMotion(stages);
  const hubWires: WireSpec[] = [
    { key: "graph-gap", from: "graph", to: "gap", motion: toGaps(stages.built, asking), zone: null },
    { key: "gap-plan", from: "gap", to: "plan", motion: toPlan, zone: null },
  ];
  const nodeWires = nodes.map((n): WireSpec => {
    const into = n.zone === "graph";
    return { key: n.id, from: into ? n.id : n.zone, to: into ? "graph" : n.id, motion: n.wire, zone: n.zone };
  });
  return [...nodeWires, ...hubWires];
}

export function buildCanvasModel(input: CanvasInput, mappers: readonly NodeMapper[]): CanvasModel {
  const stages = stagesOf(input);
  const graphDecision = graphDecisionOf(input);
  const planDecision = planDecisionOf(input, stages, graphDecision !== null);
  const hubs: Record<ZoneId, HubView> = {
    graph: graphHub(input, stages, graphDecision),
    gap: gapHub(input, stages),
    plan: planHub(input, stages, planDecision),
  };
  const nodes = mappers.flatMap((map) => map(input, stages)).toSorted(byAddedAt);
  const live: Record<ZoneId, boolean> = {
    graph: (input.status?.sources.length ?? 0) > 0,
    gap: stages.gapsFound,
    plan: stages.planOn,
  };
  const zones = Object.fromEntries(
    ZONES.map((id): [ZoneId, ZoneView] => {
      const mine = nodes.filter((n) => n.zone === id);
      const empty = mine.length === 0;
      return [
        id,
        { id, label: ZONE_TEXT[id].label, live: live[id], nodes: mine, bigAdd: id === "gap" || empty, addLabel: ZONE_TEXT[id].add },
      ];
    }),
  ) as Record<ZoneId, ZoneView>;
  return { zones, hubs, wires: wiresOf(nodes, stages, input), reveal: revealOf(input, stages) };
}
