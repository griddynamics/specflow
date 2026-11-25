// Node mappers of the sources the sources API reports: Upload (a Local files batch) and Google Drive (a folder). One node per object (D5);
// the card line is the same text the V1 source card shows (`source-logic.ts cardView`). A source that failed is the one stopped state
// here: the API reports it (D2). No resync: a source is read once, when it is added (D7).
import { driveReasonMessage, LISTING } from "@/components/mission-control/sources/drive-logic";
import { clock } from "@/components/mission-control/sources/kb-logic";
import { cardView, failedCardLine } from "@/components/mission-control/sources/source-logic";
import { fullSourceName, progressOf, sourceState } from "@/lib/domain/source-state";
import type { Source, SourceKind } from "@/lib/domain/sources";
import { V2_COPY } from "../v2-copy";
import { CONFLUENCE_WORDS } from "./confluence-logic";
import type { ConnectorKind, NodeMapper, NodeView, Stages } from "./canvas-types";

const APP_NAME: Partial<Record<SourceKind, string>> = { upload: "Upload", gdrive: "Google Drive", confluence: "Confluence" };

const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

/** "Upload 10:42" for a batch added today, "Upload 25 Nov" for an older one, with " (2)" for a same-minute batch: short enough for a node. */
export function uploadName(s: Pick<Source, "addedAt" | "nameSuffix">, now: Date): string {
  const added = new Date(s.addedAt);
  const sameDay = added.toDateString() === now.toDateString();
  const suffix = s.nameSuffix > 1 ? ` (${String(s.nameSuffix)})` : "";
  return `Upload ${sameDay ? clock(s.addedAt) : DAY.format(added)}${suffix}`;
}

/** A Confluence source counts pages where the others count files. */
export const unitText = (kind: SourceKind, text: string): string =>
  kind === "confluence" ? text.replaceAll(/\bfile(s?)\b/g, "page$1") : text;

/** Why a source stopped, in words: Drive's reasons, or for Confluence what the closed failure reason means (`no_access`, else a failed import). */
function stoppedWords(s: Source, kind: SourceKind): { meta: string; text: string } {
  if (kind === "confluence") {
    return s.failure === "no_access"
      ? { meta: CONFLUENCE_WORDS.noAccessMeta, text: CONFLUENCE_WORDS.noAccess }
      : { meta: CONFLUENCE_WORDS.failedMeta, text: CONFLUENCE_WORDS.failed };
  }
  const meta = failedCardLine(s);
  return { meta, text: s.failure === null ? meta : driveReasonMessage(s.failure, null) };
}

function sourceNode(s: Source, kind: ConnectorKind & SourceKind, stages: Stages, now: Date): NodeView {
  const state = sourceState(s);
  const card = cardView(s);
  const app = APP_NAME[kind] ?? kind;
  const name = kind === "upload" ? uploadName(s, now) : fullSourceName(s);
  const named = kind === "upload" ? name : `${app} ${name}`;
  const detail = { app, by: `${V2_COPY.addedBy} ${s.addedBy.name}`, remove: "source", reconnect: null } as const;
  const base = { id: s.id, kind, zone: "graph", mark: kind, name, tag: undefined, addedAt: s.addedAt } as const;
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
      detail: { ...detail, text, reconnect: kind === "confluence" && s.failure === "no_access" ? "atlassian" : null },
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
  (kind: "upload" | "gdrive" | "confluence"): NodeMapper =>
  (input, stages) =>
    (input.status?.sources ?? []).filter((s) => s.kind === kind).map((s) => sourceNode(s, kind, stages, input.now));

export const uploadNodes: NodeMapper = sourcesOf("upload");
export const driveNodes: NodeMapper = sourcesOf("gdrive");
export const confluenceNodes: NodeMapper = sourcesOf("confluence");
