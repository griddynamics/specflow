// MCP joins the canvas by itself (docs/mission-control/README.md): under Gap when the gaps are found, under Plan when the plan is built.
// Selecting it shows how a coding agent connects and the tools of that stage (docs/mcp-server/tools.md).
import type { NodeMapper, NodeView, ZoneId } from "./canvas-types";

export const MCP_NODE_IDS: Record<Extract<ZoneId, "gap" | "plan">, string> = { gap: "mcp-gap", plan: "mcp-plan" };

const META: Record<keyof typeof MCP_NODE_IDS, string> = { gap: "Agents answer gaps", plan: "Agents claim stories" };

const mcpNode = (zone: keyof typeof MCP_NODE_IDS): NodeView => ({
  id: MCP_NODE_IDS[zone],
  kind: "mcp",
  zone,
  mark: "mcp",
  name: "MCP",
  meta: META[zone],
  state: "on",
  badge: null,
  progress: null,
  listing: false,
  label: "MCP, added automatically: how to connect a coding agent",
  detail: { app: "MCP", by: null, text: "Added automatically", remove: null, reconnect: null },
  tag: "Automatic",
  wire: "on",
  addedAt: "",
});

export const mcpNodes: NodeMapper = (_input, stages) => [
  ...(stages.gapsFound ? [mcpNode("gap")] : []),
  ...(stages.planOn ? [mcpNode("plan")] : []),
];
