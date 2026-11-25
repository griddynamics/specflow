// Node mapper of GitHub repositories: one node per repository of the project (D5). A repository is cloned as preparation; the build does not
// read the clone (D19), so a node shows only the clone and adds nothing to the graph. It stops only for what the API reports: a clone that
// failed, or the GitHub integration that needs a reconnect (D2).
import { cloneViewOf } from "@/components/settings/github-logic";
import { SETTINGS_COPY } from "@/components/settings/settings-copy";
import type { ProjectRepository } from "@/lib/domain/github";
import { V2_COPY } from "../v2-copy";
import type { CanvasInput, NodeDetail, NodeMapper, NodeView } from "./canvas-types";
import { connectedByOf, stoppedIntegration } from "./nodes-shared";

const APP = "GitHub";
const RECONNECT_CODES = new Set(["github_reconnect_required", "github_not_connected"]);

function addedBy(r: ProjectRepository): string | null {
  if (r.addedByMe) return SETTINGS_COPY.addedByYou;
  return r.addedByName === null ? null : `${V2_COPY.addedBy} ${r.addedByName}`;
}

const shortName = (fullName: string): string => fullName.split("/").at(-1) ?? fullName;

function repositoryNode(r: ProjectRepository, input: CanvasInput, canManage: boolean): NodeView {
  const view = cloneViewOf(r);
  const stopped = stoppedIntegration(input, "github");
  const by = addedBy(r);
  const detail: Omit<NodeDetail, "text" | "reconnect"> = { app: APP, by, remove: canManage ? "repository" : null };
  const base = {
    id: r.id,
    kind: "github",
    zone: "graph",
    mark: "github",
    name: shortName(r.fullName),
    addedAt: r.createdAt,
    listing: false,
  } as const;
  const label = (meta: string) => `${APP} ${r.fullName}: ${meta}`;
  if (stopped !== undefined) {
    const meta = V2_COPY.stoppedReconnect;
    const by2 = connectedByOf(input, "github", V2_COPY.connectedBy);
    return {
      ...base,
      meta,
      state: "stopped",
      badge: "stop",
      progress: null,
      label: label(meta),
      wire: "off",
      detail: { ...detail, by: by2 ?? by, text: V2_COPY.stoppedBody, reconnect: "github" },
    };
  }
  if (r.cloneState === "failed") {
    const meta = view.word;
    const reconnect = RECONNECT_CODES.has(r.failureCode ?? "") ? "github" : null;
    return {
      ...base,
      meta,
      state: "stopped",
      badge: "stop",
      progress: null,
      label: label(meta),
      wire: "off",
      detail: { ...detail, text: view.reason ?? meta, reconnect },
    };
  }
  if (r.cloneState === "pending" || r.cloneState === "cloning") {
    return {
      ...base,
      meta: view.word,
      state: "busy",
      badge: "spin",
      progress: null,
      label: label(view.word),
      wire: "flow",
      detail: { ...detail, text: view.word, reconnect: null },
    };
  }
  const meta = `${view.word} · ${APP}`;
  const facts = view.facts === "" ? "" : ` ${view.facts}.`;
  return {
    ...base,
    meta,
    state: "on",
    badge: "ok",
    progress: null,
    label: label(view.word),
    wire: "on",
    detail: { ...detail, text: `${view.word}.${facts} ${V2_COPY.notReadYet}`, reconnect: null },
  };
}

export const githubNodes: NodeMapper = (input) =>
  (input.github?.repositories ?? []).map((r) => repositoryNode(r, input, input.github?.canManage === true));
