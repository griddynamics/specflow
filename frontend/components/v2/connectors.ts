// The connector registry (06a, D5, D20): every kind of connection the canvas offers, in the order its plus dialog lists them. Only kinds of
// V2's scope are here: Upload, Google Drive and MCP (06a), the GitHub repository, Slack channel and Jira plan (06b), the Confluence
// link (08), the Jira epic (09), the Jira gap sub-tasks (10), the GitHub Projects plan destination (11b). To add one: write its form component and its node
// mapper (`canvas/nodes-*.ts`), then add one spec below. The canvas, the plus dialog and the "Set up first" round trip need no other change.
import { confluenceNodes, driveNodes, jiraEpicNodes, uploadNodes } from "./canvas/nodes-sources";
import { githubNodes } from "./canvas/nodes-github";
import { ConfluenceForm } from "./canvas/ConfluenceForm";
import { JiraEpicForm } from "./canvas/JiraEpicForm";
import { jiraNodes } from "./canvas/nodes-jira";
import { githubPlanNodes } from "./canvas/nodes-github-plan";
import { jiraGapNodes } from "./canvas/nodes-jira-gaps";
import { slackNodes } from "./canvas/nodes-slack";
import { GitHubForm } from "./canvas/GitHubForm";
import { GitHubPlanForm } from "./canvas/GitHubPlanForm";
import { JiraGapForm } from "./canvas/JiraGapForm";
import { JiraForm } from "./canvas/JiraForm";
import { SlackForm } from "./canvas/SlackForm";
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
    line: "Files from your computer",
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
    kind: "github",
    zone: "graph",
    name: "GitHub repository",
    line: "Cloned; not read by builds yet",
    mark: "github",
    app: "github",
    title: "Add repositories",
    check: null,
    Form: GitHubForm,
    nodes: githubNodes,
  },
  {
    kind: "confluence",
    zone: "graph",
    name: "Confluence link",
    line: "A page or a space, following its links",
    mark: "confluence",
    app: "atlassian",
    title: "Add a Confluence link",
    check: { test: "Check link", testing: "Checking…", add: "Add source" },
    Form: ConfluenceForm,
    nodes: confluenceNodes,
  },
  {
    kind: "jira_epic",
    zone: "graph",
    name: "Jira epic",
    line: "An epic and every issue under it",
    mark: "jira",
    app: "atlassian",
    title: "Add a Jira epic",
    check: { test: "Check epic", testing: "Checking…", add: "Add source" },
    Form: JiraEpicForm,
    nodes: jiraEpicNodes,
  },
  // The Slack channel IS the project's Slack integration (it is bound from inside Slack), so the form needs no app first.
  {
    kind: "slack",
    zone: "gap",
    name: "Slack channel",
    line: "Gaps answered in a channel thread",
    mark: "slack",
    app: null,
    title: "Connect a Slack channel",
    check: { test: "Test connection", testing: "Posting…", add: "Add channel" },
    Form: SlackForm,
    nodes: slackNodes,
  },
  {
    kind: "jira_gap",
    zone: "gap",
    name: "Jira sub-tasks",
    line: "Gaps as sub-tasks of one Story",
    mark: "jira",
    app: "atlassian",
    title: "Send gaps to Jira",
    check: { test: "Check access", testing: "Checking…", add: "Add channel" },
    Form: JiraGapForm,
    nodes: jiraGapNodes,
  },
  {
    kind: "jira",
    zone: "plan",
    name: "Jira",
    line: "Stories and sub-tasks under an epic",
    mark: "jira",
    app: "atlassian",
    title: "Send the plan to Jira",
    check: { test: "Check access", testing: "Checking…", add: "Add destination" },
    Form: JiraForm,
    nodes: jiraNodes,
  },
  {
    kind: "github_plan",
    zone: "plan",
    name: "GitHub Projects",
    line: "Stories as issues on a project",
    mark: "github",
    app: "github",
    title: "Send the plan to GitHub Projects",
    check: { test: "Check access", testing: "Checking…", add: "Add destination" },
    Form: GitHubPlanForm,
    nodes: githubPlanNodes,
  },
  {
    kind: "mcp",
    zone: null,
    name: "MCP",
    line: "Coding agents answer gaps and claim stories",
    mark: "mcp",
    app: null,
    title: "Coding agents over MCP",
    check: null,
    Form: McpForm,
    nodes: mcpNodes,
  },
];

export const NODE_MAPPERS = CONNECTORS.map((c) => c.nodes);
