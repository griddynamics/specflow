// Node mapper of the Jira plan destination: one target per project (`jira.get`). It waits for the plan, syncs while a pass runs (the wire
// moves only then) and stops only for what the API reports: an Atlassian connection that expired or went, or a pass that failed (D2).
import { statusWord, syncErrorText } from "@/components/shared/settings/jira-logic";
import type { JiraSettings } from "@/lib/domain/jira";
import { V2_COPY } from "../v2-copy";
import type { CanvasInput, NodeMapper, NodeView, Stages } from "./canvas-types";
import { stoppedIntegration } from "./nodes-shared";

const nameOf = (j: JiraSettings): string => `Jira · ${j.epicKey ?? j.createdEpicKey ?? j.jiraProject ?? ""}`;

const progressOf = (j: JiraSettings): number | null => {
  const p = j.sync?.progress;
  return p === null || p === undefined || p.total <= 0 ? null : Math.min(1, p.done / p.total);
};

function jiraNode(j: JiraSettings, input: CanvasInput, stages: Stages): NodeView {
  const name = nameOf(j);
  const by = j.connection?.connectedByName ?? null;
  const base = { id: "jira-plan", kind: "jira", zone: "plan", mark: "jira", name, addedAt: "", listing: false, progress: null } as const;
  const detail = { app: "Jira", by: by === null ? null : `${V2_COPY.connectedBy} ${by}`, remove: null, reconnect: null } as const;
  const label = (meta: string) => `Jira ${name}: ${meta}`;
  const sync = j.sync;
  if (j.connection?.status !== undefined && j.connection.status !== "active") {
    return {
      ...base,
      meta: V2_COPY.stoppedReconnect,
      state: "stopped",
      badge: "stop",
      label: label(V2_COPY.stoppedReconnect),
      wire: "off",
      detail: { ...detail, text: V2_COPY.stoppedBody, reconnect: "atlassian" },
    };
  }
  if (stoppedIntegration(input, "atlassian") !== undefined) {
    return {
      ...base,
      meta: V2_COPY.stoppedReconnect,
      state: "stopped",
      badge: "stop",
      label: label(V2_COPY.stoppedReconnect),
      wire: "off",
      detail: { ...detail, text: V2_COPY.stoppedBody, reconnect: "atlassian" },
    };
  }
  if (!j.enabled)
    return {
      ...base,
      meta: V2_COPY.syncOff,
      state: "wait",
      badge: null,
      label: label(V2_COPY.syncOff),
      wire: "off",
      detail: { ...detail, text: V2_COPY.syncOff },
    };
  if (!stages.planOn)
    return {
      ...base,
      meta: V2_COPY.waitsForPlan,
      state: "wait",
      badge: null,
      label: label(V2_COPY.waitsForPlan),
      wire: "off",
      detail: { ...detail, text: V2_COPY.waitsForPlan },
    };
  if (sync?.status === "running") {
    return {
      ...base,
      progress: progressOf(j),
      meta: V2_COPY.syncing,
      state: "busy",
      badge: "spin",
      label: label(V2_COPY.syncing),
      wire: "flow",
      detail: { ...detail, text: V2_COPY.syncing },
    };
  }
  if (sync?.status === "failed") {
    const text = syncErrorText(sync.errorCode ?? "", true);
    return {
      ...base,
      meta: statusWord("failed"),
      state: "stopped",
      badge: "stop",
      label: label(statusWord("failed")),
      wire: "off",
      detail: { ...detail, text },
    };
  }
  const never = sync === null || sync.status === "idle";
  const word = never ? V2_COPY.notSyncedYet : statusWord(sync.status);
  return {
    ...base,
    meta: word,
    state: "on",
    badge: never ? null : "ok",
    label: label(word),
    wire: "on",
    detail: { ...detail, text: word },
  };
}

export const jiraNodes: NodeMapper = (input, stages) => (input.jira?.jiraProject == null ? [] : [jiraNode(input.jira, input, stages)]);
