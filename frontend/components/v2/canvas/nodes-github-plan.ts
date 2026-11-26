// Node mapper of the GitHub Projects plan destination: one per project, under Plan. It waits for the plan, syncs while a pass runs (the wire
// moves only then) and stops only for what the API reports: a GitHub connection that expired or went, or the closed code the last pass ended on.
import type { GitHubProjects } from "@/lib/domain/github-projects";
import { V2_COPY } from "../v2-copy";
import type { CanvasInput, NodeMapper, NodeView, Stages } from "./canvas-types";
import { githubPassText, GITHUB_RECONNECT } from "./github-plan-logic";
import { connectedByOf, nodeStates, stoppedIntegration } from "./nodes-shared";

const plural = (n: number): string => `${String(n)} ${n === 1 ? "issue" : "issues"}`;

function planNode(g: GitHubProjects, input: CanvasInput, stages: Stages): NodeView {
  const name = `GitHub · ${g.projectTitle ?? g.repository ?? ""}`;
  const detail = { app: "GitHub", by: connectedByOf(input, "github", V2_COPY.connectedBy), remove: null, reconnect: null } as const;
  const { stopped, quiet, busy, on } = nodeStates(
    { id: "github-plan", kind: "github_plan", zone: "plan", mark: "github", name },
    detail,
    (meta) => `GitHub Projects ${name}: ${meta}`,
  );
  const sync = g.sync;
  const code = sync?.errorCode ?? null;
  const failed = sync?.status === "failed" && code !== null;
  if (stoppedIntegration(input, "github") !== undefined || (failed && GITHUB_RECONNECT.has(code)))
    return stopped(V2_COPY.stoppedReconnect, V2_COPY.stoppedBody, "github");
  if (failed) return stopped(V2_COPY.githubPlanStopped, githubPassText(code), null);
  if (!g.enabled) return quiet(V2_COPY.githubPlanOff);
  if (sync?.status === "skipped" && code !== null) return quiet(githubPassText(code));
  if (!stages.planOn) return quiet(V2_COPY.waitsForPlan);
  if (sync?.status === "running") return busy(V2_COPY.syncing);
  const created = sync?.counts?.created ?? 0;
  const never = sync === null || sync.status === "idle" || sync.counts === null;
  let meta: string = V2_COPY.notSyncedYet;
  if (!never) meta = created > 0 ? `${plural(created)} · GitHub` : V2_COPY.githubPlanSynced;
  const text = sync?.status === "partial" ? `${V2_COPY.githubPlanExports} ${V2_COPY.githubPlanPartial}` : V2_COPY.githubPlanExports;
  return on(meta, text, never ? null : "ok", "on");
}

export const githubPlanNodes: NodeMapper = (input, stages) =>
  input.githubProjects?.repository == null ? [] : [planNode(input.githubProjects, input, stages)];
