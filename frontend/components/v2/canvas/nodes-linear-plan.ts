// Node mapper of the Linear plan destination: one per project, under Plan. It waits for the plan, syncs while a pass runs (the wire moves only
// then) and stops only for what the API reports: a Linear connection that expired or went, or the closed code the last pass ended on. The
// dialog says how the last pass went (what it created, updated, commented and failed) and links to the Linear project.
import type { LinearPlan } from "@/lib/domain/linear-plan";
import { V2_COPY } from "../v2-copy";
import type { CanvasInput, NodeMapper, NodeView, Stages } from "./canvas-types";
import { LINEAR_PLAN_COPY, LINEAR_RECONNECT, linearDetailText, linearPassText, linearSyncedMeta } from "./linear-plan-logic";
import { connectedByOf, nodeStates, stoppedIntegration } from "./nodes-shared";

function planNode(plan: LinearPlan, input: CanvasInput, stages: Stages): NodeView {
  const name = `Linear · ${plan.linearProjectName ?? plan.teamKey ?? ""}`;
  const link = plan.linearProjectUrl === null ? {} : { link: { href: plan.linearProjectUrl, label: LINEAR_PLAN_COPY.openProject } };
  const detail = {
    app: "Linear",
    by: connectedByOf(input, "linear", V2_COPY.connectedBy),
    remove: null,
    reconnect: null,
    ...link,
  } as const;
  const { stopped, quiet, busy, on } = nodeStates(
    { id: "linear-plan", kind: "linear_plan", zone: "plan", mark: "linear", name },
    detail,
    (meta) => `Linear plan ${name}: ${meta}`,
  );
  const sync = plan.sync;
  const code = sync?.errorCode ?? null;
  const failed = sync?.status === "failed" && code !== null;
  if (stoppedIntegration(input, "linear") !== undefined || (failed && LINEAR_RECONNECT.has(code)))
    return stopped(V2_COPY.stoppedReconnect, V2_COPY.stoppedBody, "linear");
  if (failed) return stopped(LINEAR_PLAN_COPY.stopped, linearPassText(code), null);
  if (!plan.enabled) return quiet(LINEAR_PLAN_COPY.off);
  if (sync?.status === "skipped" && code !== null) return quiet(linearPassText(code));
  if (!stages.planOn) return quiet(V2_COPY.waitsForPlan);
  if (sync?.status === "running") return busy(V2_COPY.syncing);
  const counts = sync?.counts ?? null;
  const never = sync === null || sync.status === "idle" || counts === null;
  return on(never ? V2_COPY.notSyncedYet : linearSyncedMeta(counts), linearDetailText(plan), never ? null : "ok", "on");
}

export const linearPlanNodes: NodeMapper = (input, stages) =>
  input.linearPlan?.teamKey == null ? [] : [planNode(input.linearPlan, input, stages)];
