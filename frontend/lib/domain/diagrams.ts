// FE-DATA-01, FE-DIA-01 (TECH-SPECS §2.9). The domain types carry no positions.
import type { DocRef } from "./common";

export type EdgeKind = "sync" | "async" | "data" | "flow";
export type DiagramNodeKind =
  | "service"
  | "app"
  | "mobile"
  | "gateway"
  | "datastore"
  | "storage"
  | "queue"
  | "ai"
  | "external"
  | "actor"
  | "cloud"
  | "security"
  | "compute"
  | "integration";
export interface DiagramEdge {
  id: string;
  from: string;
  to: string;
  kind: EdgeKind;
  label?: string; // text on the edge ("-" in MOCKS = no label)
  note?: string; // tooltip on the edge, e.g. "Rule open: DEC-10"
}

// Business view (O5): swimlane of the logic flow.
export interface Lane {
  id: string;
  title: string;
  subtitle?: string;
}
export interface FlowStep {
  id: string;
  title: string;
  subtitle?: string;
  lane: string;
  col: number; // ORDER HINT only (MOCKS "Col"): sorts steps inside a layer and breaks ties; never a position
  type: "start" | "step" | "subprocess" | "decision" | "end";
}
export interface BusinessDiagram {
  label: string; // accessible summary of the diagram (aria-label of the canvas)
  lanes: Lane[];
  steps: FlowStep[];
  edges: DiagramEdge[];
  legend?: string[];
  source: DocRef; // "open in Files" document
}

// Architecture view: MOCKS §6.
export interface SystemNode {
  id: string;
  title: string;
  subtitle?: string;
  kind: DiagramNodeKind;
  owner: "Vendor" | "Client" | "Third party";
}
export interface ServiceNode {
  id: string;
  systemId: string;
  title: string;
  subtitle?: string;
  kind: DiagramNodeKind;
}
export interface ArchitectureModel {
  label: string;
  systems: SystemNode[];
  services: ServiceNode[];
  systemEdges: DiagramEdge[];
  serviceEdges: DiagramEdge[];
  source: DocRef;
}

export interface DiagramsPort {
  business(projectId: string): Promise<BusinessDiagram>;
  architecture(projectId: string): Promise<ArchitectureModel>; // both levels; expansion is view state
}
