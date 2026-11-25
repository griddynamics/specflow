// The connector registry (06a, D5, D20): every kind of connection the canvas offers, in the order its plus dialog lists them. Only kinds of
// V2's scope are here: Upload, Google Drive and MCP (06a); the GitHub repository, Slack channel and Jira plan follow in 06b, the Confluence
// link, Jira epic, Jira for gaps and GitHub Projects in the sessions of those connectors. To add one: write its form component and its node
// mapper (`canvas/nodes-*.ts`), then add one spec below. The canvas, the plus dialog and the "Set up first" round trip need no other change.
import { driveNodes, uploadNodes } from "./canvas/nodes-sources";
import { mcpNodes } from "./canvas/nodes-mcp";
import { DriveForm } from "./canvas/DriveForm";
import { McpForm } from "./canvas/McpForm";
import { UploadForm } from "./canvas/UploadForm";
import type { ConnectorSpec } from "./connector-types";

export const CONNECTORS: readonly ConnectorSpec[] = [
  {
    kind: "upload",
    zone: "graph",
    name: "Upload",
    line: "PDF, Word, PowerPoint, Excel, Markdown, CSV",
    mark: "upload",
    app: null,
    title: "Add files",
    check: null,
    Form: UploadForm,
    nodes: uploadNodes,
  },
  {
    kind: "gdrive",
    zone: "graph",
    name: "Google Drive",
    line: "A shared folder, read when it is added",
    mark: "gdrive",
    app: null,
    title: "Connect a Google Drive folder",
    check: { test: "Test connection", testing: "Testing…", add: "Add source" },
    Form: DriveForm,
    nodes: driveNodes,
  },
  {
    kind: "mcp",
    zone: null,
    name: "MCP",
    line: "Coding agents answer gaps and claim stories",
    mark: "mcp",
    app: null,
    title: "Connect a coding agent over MCP",
    check: null,
    Form: McpForm,
    nodes: mcpNodes,
  },
];

export const NODE_MAPPERS = CONNECTORS.map((c) => c.nodes);
