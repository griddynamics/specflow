// Node mapper of the Jira gap channel: one per project, under Gaps. It waits for the gaps; once they are found it counts the questions posted
// as sub-tasks and the ones answered. It stops only for what the API reports: an Atlassian connection that expired or went, or a closed code the
// last pass ended on. The wire moves only while a gap run goes on (questions go out).
import { syncErrorText } from "@/components/settings/jira-logic";
import { COPY } from "@/lib/copy";
import type { JiraGaps } from "@/lib/domain/jira-gaps";
import { V2_COPY } from "../v2-copy";
import type { CanvasInput, NodeMapper, NodeView, Stages } from "./canvas-types";
import { connectedByOf, nodeStates, stoppedIntegration } from "./nodes-shared";

const WORKING = new Set(["queued", "running"]);
const RECONNECT = new Set(["jira_not_connected", "jira_reconnect_required"]);

function gapNode(j: JiraGaps, input: CanvasInput, stages: Stages): NodeView {
  const name = `Jira · ${j.jiraProject ?? ""} gaps`;
  const detail = { app: "Jira", by: connectedByOf(input, "atlassian", V2_COPY.connectedBy), remove: null, reconnect: null } as const;
  const { stopped, quiet, on } = nodeStates(
    { id: "jira-gaps", kind: "jira_gap", zone: "gap", mark: "jira", name },
    detail,
    (meta) => `Jira gaps ${j.jiraProject ?? ""}: ${meta}`,
  );
  if (stoppedIntegration(input, "atlassian") !== undefined || (j.errorCode !== null && RECONNECT.has(j.errorCode)))
    return stopped(V2_COPY.stoppedReconnect, V2_COPY.stoppedBody, "atlassian");
  if (j.errorCode === "jira_unavailable") return stopped(V2_COPY.jiraGapsStopped, COPY.jiraUnavailable, null);
  if (j.errorCode !== null) return stopped(V2_COPY.jiraGapsStopped, syncErrorText(j.errorCode, true), null);
  if (!j.enabled) return quiet(V2_COPY.jiraGapsOff, V2_COPY.jiraGapsOff);
  const g = input.gaps;
  if (!stages.gapsFound || g === null) return quiet(V2_COPY.waitsForGaps, V2_COPY.jiraGapsPosted);
  const meta = `${String(j.posted)} posted · ${String(j.answered)} answered`;
  return on(
    meta,
    j.storyKey === null ? V2_COPY.jiraGapsPosted : `${V2_COPY.jiraGapsPosted} Story ${j.storyKey}.`,
    "ok",
    WORKING.has(g.phase) ? "flow" : "on",
  );
}

export const jiraGapNodes: NodeMapper = (input, stages) =>
  input.jiraGaps?.jiraProject == null ? [] : [gapNode(input.jiraGaps, input, stages)];
