// Node mapper of the Linear gap channel: one per project, under Gaps. It waits for the gaps; once they are found it counts the questions posted
// as sub-issues and the ones answered. It stops only for what the API reports: a Linear connection that expired or went, or a closed code the
// last pass ended on. The wire moves only while a gap run goes on (questions go out).
import type { LinearGaps } from "@/lib/domain/linear-gaps";
import { V2_COPY } from "../v2-copy";
import type { CanvasInput, NodeMapper, NodeView, Stages } from "./canvas-types";
import { LINEAR_GAP_COPY as COPY, LINEAR_RECONNECT } from "./linear-gap-copy";
import { connectedByOf, nodeStates, stoppedIntegration } from "./nodes-shared";

const WORKING = new Set(["queued", "running"]);

function gapNode(l: LinearGaps, input: CanvasInput, stages: Stages): NodeView {
  const name = `Linear · ${l.teamKey ?? ""} gaps`;
  const detail = { app: "Linear", by: connectedByOf(input, "linear", V2_COPY.connectedBy), remove: null, reconnect: null } as const;
  const { stopped, quiet, on } = nodeStates(
    { id: "linear-gaps", kind: "linear_gaps", zone: "gap", mark: "linear", name },
    detail,
    (meta) => `Linear gaps ${l.teamKey ?? ""}: ${meta}`,
  );
  if (stoppedIntegration(input, "linear") !== undefined || (l.errorCode !== null && LINEAR_RECONNECT.has(l.errorCode)))
    return stopped(V2_COPY.stoppedReconnect, V2_COPY.stoppedBody, "linear");
  if (l.errorCode !== null) return stopped(COPY.stopped, COPY.errors[l.errorCode] ?? COPY.generic, null);
  if (!l.enabled) return quiet(COPY.off, COPY.off);
  const g = input.gaps;
  if (!stages.gapsFound || g === null) return quiet(V2_COPY.waitsForGaps, COPY.posted);
  const meta = `${String(l.posted)} posted · ${String(l.answered)} answered`;
  return on(
    meta,
    l.parentIdentifier === null ? COPY.posted : `${COPY.posted} ${COPY.parent} ${l.parentIdentifier}.`,
    "ok",
    WORKING.has(g.phase) ? "flow" : "on",
  );
}

export const linearGapNodes: NodeMapper = (input, stages) =>
  input.linearGaps?.teamKey == null ? [] : [gapNode(input.linearGaps, input, stages)];
