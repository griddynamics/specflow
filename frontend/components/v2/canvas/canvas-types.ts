// The shapes of the canvas view model (D5, D10; docs/mission-control/README.md "The canvas"). Pure types: the model builds them from the
// ports' answers, the screen hands them to the presentational canvas of `@graphico/ui`. No React.
import type { CanvasState, CanvasZoneId, NodeBadge, VendorKind, WireMotion } from "@graphico/ui";
import type { GapCheck } from "@/lib/domain/gaps";
import type { Integration } from "@/lib/domain/integrations";
import type { PlanState } from "@/lib/domain/plan";
import type { ProjectSourcesStatus } from "@/lib/domain/sources";

export type ZoneId = CanvasZoneId;

/** The kinds of connection the canvas knows. A session that registers one adds its name here, its spec to `connectors.ts` and its node
 * mapper to the list `CONNECTORS` gives the model. */
export type ConnectorKind = "upload" | "gdrive" | "mcp";

/** What the open gaps say, from `gaps.check` and the first page of `gaps.list` (`more`: the page was full, so the counts are "at least"). */
export interface GapsSummary {
  phase: GapCheck["phase"];
  stepsDone: number;
  stepsTotal: number;
  total: number;
  answered: number;
  /** Open gaps of the severity that blocks work (`blocker`): the count on the Gaps hub. */
  critical: number;
  more: boolean;
}

/** Everything the canvas shows is derived from this: the ports' answers, `null` while a read has not come back. */
export interface CanvasInput {
  now: Date;
  status: ProjectSourcesStatus | null;
  gaps: GapsSummary | null;
  plan: PlanState | null;
  /** A graph build finished after the plan was seen: the plan is due for a replan (D9). */
  planStale: boolean;
  integrations: readonly Integration[] | null;
}

/** What the stages before a node say, so a mapper can tell "waits for the stage before it" from "working". */
export interface Stages {
  built: boolean;
  graphBuilding: boolean;
  gapsFound: boolean;
  planBuilding: boolean;
  planOn: boolean;
}

export interface NodeView {
  id: string;
  kind: ConnectorKind;
  zone: ZoneId;
  mark: VendorKind;
  name: string;
  meta: string;
  state: CanvasState;
  badge: NodeBadge | null;
  progress: number | null;
  listing: boolean;
  label: string;
  /** "Automatic" for MCP. */
  tag?: string;
  /** The wire between the node and its hub. */
  wire: WireMotion;
  /** Sort key inside the zone: the time the object was added (ISO), or "" to keep the mapper's order. */
  addedAt: string;
}

export type NodeMapper = (input: CanvasInput, stages: Stages) => NodeView[];

export interface Decision {
  label: string;
  /** Yellow only while it is the open decision that comes first; the other one is ink. */
  yellow: boolean;
}

export interface HubView {
  id: ZoneId;
  state: CanvasState;
  tooltip: string;
  label: string;
  progress: number | "unknown" | null;
  pill?: string;
  decision?: Decision;
}

export interface WireSpec {
  key: string;
  from: string;
  to: string;
  motion: WireMotion;
  /** Leave the hub downwards (the gap channels hang under their hub). */
  down: boolean;
}

export interface ZoneView {
  id: ZoneId;
  label: string;
  live: boolean;
  nodes: NodeView[];
  /** The plus tile is node-sized (an empty stage, and always under Gaps). */
  bigAdd: boolean;
  addLabel: string;
}

export interface CanvasModel {
  zones: Record<ZoneId, ZoneView>;
  hubs: Record<ZoneId, HubView>;
  wires: WireSpec[];
  /** 0..1.01: how much of the background graph is lit. Decorative (AC10): time since the first build started. */
  reveal: number;
}
