// The shapes of the canvas view model (D5, D10; docs/mission-control/README.md "The canvas"). Pure types: the model builds them from the
// ports' answers, the screen hands them to the presentational canvas of `@graphico/ui`. No React.
import type { CanvasState, CanvasZoneId, NodeBadge, VendorKind, WireMotion } from "@graphico/ui";
import type { GapCheck, GapKind, GapSeverity } from "@/lib/domain/gaps";
import type { ProjectRepositories } from "@/lib/domain/github";
import type { JiraSettings } from "@/lib/domain/jira";
import type { GitHubProjects } from "@/lib/domain/github-projects";
import type { JiraGaps } from "@/lib/domain/jira-gaps";
import type { LinearGaps } from "@/lib/domain/linear-gaps";
import type { LinearPlan } from "@/lib/domain/linear-plan";
import type { NotionPlan } from "@/lib/domain/notion-plan";
import type { SlackSettings } from "@/lib/domain/slack";
import type { Integration, IntegrationApp } from "@/lib/domain/integrations";
import type { PlanState } from "@/lib/domain/plan";
import type { ProjectSourcesStatus } from "@/lib/domain/sources";

export type ZoneId = CanvasZoneId;

/** The kinds of connection the canvas knows. A session that registers one adds its name here, its spec to `connectors.ts` and its node
 * mapper to the list `CONNECTORS` gives the model. */
export type ConnectorKind =
  | "upload"
  | "gdrive"
  | "github"
  | "confluence"
  | "jira_epic"
  | "notion"
  | "slack"
  | "jira_gap"
  | "jira"
  | "github_plan"
  | "notion_plan"
  | "mcp"
  | "linear_project"
  | "linear_gaps"
  | "linear_plan";

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

/** An open gap, as the read-only Gaps dialog lists it (D10: answering happens in Slack, Jira and over MCP). */
export interface GapRow {
  id: string;
  title: string;
  kind: GapKind;
  severity: GapSeverity;
}

/** Everything the canvas shows is derived from this: the ports' answers, `null` while a read has not come back. */
export interface CanvasInput {
  now: Date;
  status: ProjectSourcesStatus | null;
  gaps: GapsSummary | null;
  /** The open gaps of the first page, for the Gaps dialog. */
  gapRows: readonly GapRow[];
  plan: PlanState | null;
  /** A build or a plan was just started here and the status has not shown it yet: the hub already looks busy and offers no decision. */
  pending: { build: boolean; plan: boolean };
  /** A graph build finished after the plan was seen: the plan is due for a replan (D9). */
  planStale: boolean;
  integrations: readonly Integration[] | null;
  /** The project's repositories (cloned, not read by the build, D19). */
  github: ProjectRepositories | null;
  /** The project's Jira target and its sync. */
  jira: JiraSettings | null;
  /** The GitHub Projects destination of the plan. */
  githubProjects: GitHubProjects | null;
  /** The Notion destination of the plan. */
  notionPlan: NotionPlan | null;
  /** The Jira gap channel of the project (sub-tasks of one Story). */
  jiraGaps: JiraGaps | null;
  /** The Slack channel of the project. */
  slack: SlackSettings | null;
  /** The Linear plan destination of the project. */
  linearPlan: LinearPlan | null;
  /** The Linear gap channel of the project. */
  linearGaps: LinearGaps | null;
}

/** What the stages before a node say, so a mapper can tell "waits for the stage before it" from "working". */
export interface Stages {
  built: boolean;
  graphBuilding: boolean;
  gapsFound: boolean;
  planBuilding: boolean;
  planOn: boolean;
}

/** What the node's dialog says: who set it up, in what state, and what can be done with it. */
export interface NodeDetail {
  /** The app, as a name ("Google Drive"). */
  app: string;
  /** "Added by <member>" or "Connected by <member>"; null when the API names nobody. */
  by: string | null;
  /** The state in a sentence. */
  text: string;
  /** A link to the object in the app (the Linear project, the Notion database of the plan), shown under the state. */
  link?: { href: string; label: string };
  /** Remove is offered for a source or a repository: which port removes it. */
  remove: "source" | "repository" | null;
  /** A stopped node offers to reconnect this app on the Integrations page. */
  reconnect: IntegrationApp | null;
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
  detail: NodeDetail;
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
  /** The stage of the node the wire goes to (geometry decides how it runs there); null between two hubs. */
  zone: ZoneId | null;
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
