// Node mappers of the sources the sources API reports: Upload (a Local files batch) and Google Drive (a folder). One node per object (D5);
// the card line is the same text the V1 source card shows (`source-logic.ts cardView`). A source that failed is the one stopped state
// here: the API reports it (D2). No resync: a source is read once, when it is added (D7).
import { driveReasonMessage, LISTING } from "@/components/mission-control/sources/drive-logic";
import { clock } from "@/components/mission-control/sources/kb-logic";
import { cardView, failedCardLine } from "@/components/mission-control/sources/source-logic";
import { fullSourceName, progressOf, sourceState } from "@/lib/domain/source-state";
import type { Source, SourceKind } from "@/lib/domain/sources";
import { V2_COPY } from "../v2-copy";
import { BOX_WORDS, boxOpen } from "./box-logic";
import { CONFLUENCE_WORDS } from "./confluence-logic";
import { JIRA_EPIC_WORDS } from "./jira-epic-logic";
import { LINEAR_SOURCE_WORDS } from "./linear-source-logic";
import type { IntegrationApp } from "@/lib/domain/integrations";
import type { NodeMapper, NodeView, Stages } from "./canvas-types";

const APP_NAME: Partial<Record<SourceKind, string>> = {
  upload: "Upload",
  gdrive: "Google Drive",
  confluence: "Confluence",
  jira_epic: "Jira",
  linear_project: "Linear",
  box: "Box",
};

const MARK = { upload: "upload", gdrive: "gdrive", confluence: "confluence", jira_epic: "jira", linear_project: "linear", box: "box" } as const;

const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

/** The name every upload batch has until someone names it (the API's default). */
export const DEFAULT_UPLOAD_NAME = "Local files";

/**
 * The name the person gave the batch, as it is; else "Upload 10:42" for a batch added today, "Upload 25 Nov" for an older one, with " (2)"
 * for a same-minute batch: short enough for a node.
 */
export function uploadName(s: Pick<Source, "addedAt" | "nameSuffix" | "name">, now: Date): string {
  if (s.name !== DEFAULT_UPLOAD_NAME) return s.name;
  const added = new Date(s.addedAt);
  const sameDay = added.toDateString() === now.toDateString();
  const suffix = s.nameSuffix > 1 ? ` (${String(s.nameSuffix)})` : "";
  return `Upload ${sameDay ? clock(s.addedAt) : DAY.format(added)}${suffix}`;
}

const UNIT: Partial<Record<SourceKind, string>> = { confluence: "page", jira_epic: "issue", linear_project: "issue" };

/** A Confluence source counts pages and a Jira epic issues where the others count files. */
export const unitText = (kind: SourceKind, text: string): string => {
  const unit = UNIT[kind];
  return unit === undefined ? text : text.replaceAll(/\bfile(s?)\b/g, `${unit}$1`);
};

/** The words of a node of an imported kind when the API reports its import failed. */
const IMPORT_WORDS: Partial<Record<SourceKind, { noAccessMeta: string; noAccess: string; failedMeta: string; failed: string }>> = {
  confluence: CONFLUENCE_WORDS,
  jira_epic: JIRA_EPIC_WORDS,
  linear_project: LINEAR_SOURCE_WORDS,
  box: BOX_WORDS,
};

/** The app a stopped imported source offers to reconnect (`no_access`: the bound member's account lost its access). */
const RECONNECT: Partial<Record<SourceKind, IntegrationApp>> = {
  confluence: "atlassian",
  jira_epic: "atlassian",
  linear_project: "linear",
  box: "box",
};
/** A node of a Jira epic is named by its key ("NWO-12: Checkout redesign" is its full name, in the label and the dialog). */
const nodeName = (s: Source, kind: SourceKind, now: Date): string => {
  if (kind === "upload") return uploadName(s, now);
  return kind === "jira_epic" ? (fullSourceName(s).split(":")[0] ?? s.name) : fullSourceName(s);
};

/** Why a source stopped, in words: Drive's reasons, or for Confluence what the closed failure reason means (`no_access`, else a failed import). */
function stoppedWords(s: Source, kind: SourceKind): { meta: string; text: string } {
  const words = IMPORT_WORDS[kind];
  if (words !== undefined) {
    return s.failure === "no_access" ? { meta: words.noAccessMeta, text: words.noAccess } : { meta: words.failedMeta, text: words.failed };
  }
  const meta = failedCardLine(s);
  return { meta, text: s.failure === null ? meta : driveReasonMessage(s.failure, null) };
}

function sourceNode(s: Source, kind: keyof typeof MARK, stages: Stages, now: Date): NodeView {
  const state = sourceState(s);
  const card = cardView(s);
  const app = APP_NAME[kind] ?? kind;
  const name = nodeName(s, kind, now);
  const named = kind === "upload" ? name : `${app} ${fullSourceName(s)}`;
  const open = kind === "box" ? boxOpen(s.link) : undefined;
  const detail = { app, by: `${V2_COPY.addedBy} ${s.addedBy.name}`, remove: "source", reconnect: null, ...(open && { open }) } as const;
  const base = { id: s.id, kind, zone: "graph", mark: MARK[kind], name, tag: undefined, addedAt: s.addedAt } as const;
  if (state === "processing") {
    const meta = unitText(kind, card.countLine);
    const text = s.listing ? LISTING : `Importing: ${meta}`;
    return {
      ...base,
      meta,
      state: "busy",
      badge: "spin",
      progress: progressOf(s),
      listing: s.listing,
      label: `${named}: ${meta}`,
      wire: "flow",
      detail: { ...detail, text },
    };
  }
  if (state === "failed") {
    const { meta, text } = stoppedWords(s, kind);
    return {
      ...base,
      meta,
      state: "stopped",
      badge: "stop",
      progress: null,
      listing: false,
      label: `${named}: ${meta}`,
      wire: "off",
      detail: { ...detail, text, reconnect: s.failure === "no_access" ? (RECONNECT[kind] ?? null) : null },
    };
  }
  const count = unitText(kind, card.countLine);
  const meta = `${count} · ${app}`;
  return {
    ...base,
    meta,
    state: "on",
    badge: "ok",
    progress: null,
    listing: false,
    label: `${named}: ${count}`,
    wire: stages.graphBuilding ? "flow" : "on",
    detail: { ...detail, text: `${count}. ${V2_COPY.importedOnce}` },
  };
}

const sourcesOf =
  (kind: "upload" | "gdrive" | "confluence" | "jira_epic" | "linear_project" | "box"): NodeMapper =>
  (input, stages) =>
    (input.status?.sources ?? []).filter((s) => s.kind === kind).map((s) => sourceNode(s, kind, stages, input.now));

export const uploadNodes: NodeMapper = sourcesOf("upload");
export const driveNodes: NodeMapper = sourcesOf("gdrive");
export const confluenceNodes: NodeMapper = sourcesOf("confluence");
export const jiraEpicNodes: NodeMapper = sourcesOf("jira_epic");
export const linearProjectNodes: NodeMapper = sourcesOf("linear_project");
export const boxNodes: NodeMapper = sourcesOf("box");
