export type NodeId = "n1" | "n2" | "n3" | "n4" | "n5" | "n6" | "n7" | "n8" | "n9" | "n10" | "n11";

export interface GraphNode {
  id: NodeId;
  x: number;
  y: number;
  label: "Area" | "Feature" | "Rule" | "Task";
}

/** Three layers of work. */
export const NODES: GraphNode[] = [
  { id: "n1", x: 270, y: 70, label: "Area" },
  { id: "n2", x: 400, y: 62, label: "Area" },
  { id: "n3", x: 530, y: 76, label: "Area" },
  { id: "n4", x: 240, y: 170, label: "Feature" },
  { id: "n5", x: 345, y: 162, label: "Feature" },
  { id: "n6", x: 455, y: 178, label: "Rule" },
  { id: "n7", x: 575, y: 168, label: "Feature" },
  { id: "n8", x: 285, y: 268, label: "Task" },
  { id: "n9", x: 390, y: 276, label: "Task" },
  { id: "n10", x: 500, y: 266, label: "Task" },
  { id: "n11", x: 595, y: 272, label: "Task" },
];

/** An edge [a, b] means b depends on a. */
export const EDGES: [NodeId, NodeId][] = [
  ["n1", "n4"],
  ["n1", "n5"],
  ["n2", "n5"],
  ["n2", "n6"],
  ["n3", "n6"],
  ["n3", "n7"],
  ["n4", "n8"],
  ["n5", "n8"],
  ["n5", "n9"],
  ["n6", "n9"],
  ["n6", "n10"],
  ["n7", "n10"],
  ["n7", "n11"],
];

export const node = (id: NodeId): GraphNode => NODES.find((n) => n.id === id) as GraphNode;

/** Where each source feeds the graph, by source row. */
export const SOURCES: { label: string; feeds: NodeId }[] = [
  { label: "Confluence", feeds: "n4" },
  { label: "Jira", feeds: "n1" },
  { label: "Google Drive", feeds: "n8" },
  { label: "Upload", feeds: "n4" },
];

export const METHODS = ["AI agents", "Graph algorithms", "Heuristics", "Formal validation"];

/** The problems Gap finds, by item. */
export const FLAGGED: NodeId[] = ["n4", "n6", "n10"];
