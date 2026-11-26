// Node mapper of the Notion plan destination: one per project, under Plan (docs/notion/plan.md "In the canvas"). It waits for the plan, syncs
// while a pass runs (the wire moves only then) and stops only for what the API reports: a Notion connection that expired or went, or the closed
// code the last pass ended on.
import type { NotionPlan } from "@/lib/domain/notion-plan";
import { V2_COPY } from "../v2-copy";
import type { CanvasInput, NodeMapper, NodeView, Stages } from "./canvas-types";
import { CANVAS_COPY } from "./canvas-copy";
import { connectedByOf, nodeStates, stoppedIntegration } from "./nodes-shared";
import { notionCountsText, notionDatabaseLink, notionPassNote, notionPassText, NOTION_RECONNECT } from "./notion-plan-logic";

function planNode(n: NotionPlan, input: CanvasInput, stages: Stages): NodeView {
  const name = n.parentPageTitle ?? "Notion";
  const href = notionDatabaseLink(n.databaseUrl);
  const link = href === null ? {} : { link: { href, label: CANVAS_COPY.notionOpenDatabase } };
  const detail = {
    app: "Notion",
    by: connectedByOf(input, "notion", V2_COPY.connectedBy),
    remove: null,
    reconnect: null,
    ...link,
  } as const;
  const title = name === "Notion" ? name : `Notion ${name}`;
  const { stopped, quiet, busy, on } = nodeStates(
    { id: "notion-plan", kind: "notion_plan", zone: "plan", mark: "notion", name },
    detail,
    (meta) => `${title}: ${meta}`,
  );
  const sync = n.sync;
  const code = sync?.errorCode ?? null;
  const failed = sync?.status === "failed" && code !== null;
  if (stoppedIntegration(input, "notion") !== undefined || (failed && NOTION_RECONNECT.has(code)))
    return stopped(V2_COPY.stoppedReconnect, V2_COPY.stoppedBody, "notion");
  if (failed) return stopped(V2_COPY.notionPlanStopped, notionPassText(code), null);
  if (!n.enabled) return quiet(V2_COPY.notionPlanOff);
  if (sync?.status === "skipped" && code !== null) return quiet(notionPassText(code));
  if (!stages.planOn) return quiet(V2_COPY.waitsForPlan);
  if (sync?.status === "running") return busy(V2_COPY.syncing);
  const counts = sync?.counts ?? null;
  const never = sync === null || sync.status === "idle" || counts === null;
  const meta = never ? V2_COPY.notSyncedYet : notionCountsText(counts);
  const note = never ? "" : notionPassNote(sync.status, counts);
  return on(meta, note === "" ? V2_COPY.notionPlanExports : `${V2_COPY.notionPlanExports} ${note}`, never ? null : "ok", "on");
}

export const notionPlanNodes: NodeMapper = (input, stages) =>
  input.notionPlan?.parentPageId == null ? [] : [planNode(input.notionPlan, input, stages)];
