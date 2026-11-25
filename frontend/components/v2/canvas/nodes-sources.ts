// Node mappers of the sources the sources API reports: Upload (a Local files batch) and Google Drive (a folder). One node per object (D5);
// the card line is the same text the V1 source card shows (`source-logic.ts cardView`). A source that failed is the one stopped state
// here: the API reports it (D2). No resync: a source is read once, when it is added (D7).
import { cardView, failedCardLine } from "@/components/mission-control/sources/source-logic";
import { fullSourceName, progressOf, sourceState } from "@/lib/domain/source-state";
import type { Source, SourceKind } from "@/lib/domain/sources";
import type { ConnectorKind, NodeMapper, NodeView, Stages } from "./canvas-types";

const APP_NAME: Partial<Record<SourceKind, string>> = { upload: "Upload", gdrive: "Google Drive" };

function sourceNode(s: Source, kind: ConnectorKind & SourceKind, stages: Stages): NodeView {
  const state = sourceState(s);
  const card = cardView(s);
  const app = APP_NAME[kind] ?? kind;
  const base = { id: s.id, kind, zone: "graph", mark: kind, name: fullSourceName(s), tag: undefined, addedAt: s.addedAt } as const;
  if (state === "processing") {
    const meta = card.countLine;
    return {
      ...base,
      meta,
      state: "busy",
      badge: "spin",
      progress: progressOf(s),
      listing: s.listing,
      label: `${app} ${base.name}: ${meta}`,
      wire: "flow",
    };
  }
  if (state === "failed") {
    const meta = failedCardLine(s);
    return {
      ...base,
      meta,
      state: "stopped",
      badge: "stop",
      progress: null,
      listing: false,
      label: `${app} ${base.name}: ${meta}`,
      wire: "off",
    };
  }
  const meta = `${card.countLine} · ${app}`;
  return {
    ...base,
    meta,
    state: "on",
    badge: "ok",
    progress: null,
    listing: false,
    label: `${app} ${base.name}: ${card.countLine}`,
    wire: stages.graphBuilding ? "flow" : "on",
  };
}

const sourcesOf =
  (kind: "upload" | "gdrive"): NodeMapper =>
  (input, stages) =>
    (input.status?.sources ?? []).filter((s) => s.kind === kind).map((s) => sourceNode(s, kind, stages));

export const uploadNodes: NodeMapper = sourcesOf("upload");
export const driveNodes: NodeMapper = sourcesOf("gdrive");
