// FE-DATA-01 (TECH-SPECS §2.14). Screens reach data only through these interfaces (R7).
import type { AssistantPort } from "@/lib/domain/assistant";
import type { BoxPort } from "@/lib/domain/box";
import type { DropboxPort } from "@/lib/domain/dropbox";
import type { CommentsPort } from "@/lib/domain/comments";
import type { ConnectorsPort } from "@/lib/domain/connectors";
import type { CreateProjectPort } from "@/lib/domain/create-project";
import type { DiagramsPort } from "@/lib/domain/diagrams";
import type { FilesPort } from "@/lib/domain/files";
import type { GapsPort } from "@/lib/domain/gaps";
import type { GitHubPort } from "@/lib/domain/github";
import type { IntegrationsPort } from "@/lib/domain/integrations";
import type { JiraPort } from "@/lib/domain/jira";
import type { LinearPort } from "@/lib/domain/linear";
import type { LinearGapsPort } from "@/lib/domain/linear-gaps";
import type { LinearPlanPort } from "@/lib/domain/linear-plan";
import type { LinearSourcePort } from "@/lib/domain/linear-source";
import type { GitHubProjectsPort } from "@/lib/domain/github-projects";
import type { JiraGapsPort } from "@/lib/domain/jira-gaps";
import type { NotionConnectionPort } from "@/lib/domain/notion-connection";
import type { NotionPlanPort } from "@/lib/domain/notion-plan";
import type { MembersPort } from "@/lib/domain/members";
import type { PeoplePort } from "@/lib/domain/people";
import type { PlanPort } from "@/lib/domain/plan";
import type { MissionControlPort } from "@/lib/domain/pipeline";
import type { ProjectsPort } from "@/lib/domain/projects";
import type { SlackPort } from "@/lib/domain/slack";
import type { KnowledgePort, SourcesPort } from "@/lib/domain/sources";
import type { WorkPort } from "@/lib/domain/work";

export interface DataPorts {
  people: PeoplePort;
  projects: ProjectsPort;
  createProject: CreateProjectPort;
  missionControl: MissionControlPort;
  files: FilesPort;
  comments: CommentsPort;
  diagrams: DiagramsPort;
  gaps: GapsPort; // the Gaps screen and the gap links of the Plan (FR-GWEB-04, FR-GWEB-05): /projects/{pid}/gaps*, /projects/{pid}/gap/runs/latest
  assistant: AssistantPort;
  connectors: ConnectorsPort;
  plan: PlanPort; // P20 (AUTO-17), TECH-SPECS §2.15
  // Mission Control sources (plans/mission-control-sources/DESIGN.md D11): served by the production http adapter.
  sources: SourcesPort;
  knowledge: KnowledgePort;
  work: WorkPort; // MCP-TOOLING session 10: findings list and claims board of the Plan page
  members: MembersPort; // project collaborators: Settings (General tab) and the Create Project suggestions
  jira: JiraPort; // the Jira export target of a project: Settings (Jira tab)
  githubProjects: GitHubProjectsPort; // the GitHub Projects plan destination of a project: the V2 canvas (Plan stage)
  notionPlan: NotionPlanPort; // the Notion plan destination of a project: the V2 canvas (Plan stage)
  notionConnection: NotionConnectionPort; // the caller's Notion connection: the V2 Integrations page
  jiraGaps: JiraGapsPort; // the Jira gap channel of a project: the V2 canvas (Gaps stage)
  slack: SlackPort; // the Slack channel of a project: Settings (Slack tab)
  github: GitHubPort; // the caller's GitHub connection and the GitHub repositories of a project: Settings (GitHub tab)
  box: BoxPort; // the caller's Box connection: the V2 Integrations page (docs/box/oauth.md)
  dropbox: DropboxPort; // the caller's Dropbox connection: the V2 Integrations page (docs/dropbox/oauth.md)
  integrations: IntegrationsPort; // who each app is connected by for a project: the V2 Integrations page
  linear: LinearPort; // LINEAR S1: the caller's Linear connection: the V2 Integrations page
  linearPlan: LinearPlanPort; // LINEAR S3: the Linear plan destination of a project: the V2 canvas (Plan stage)
  linearSource: LinearSourcePort; // LINEAR S4: a Linear project or issue as a source: the V2 canvas (Graph stage)
  linearGaps: LinearGapsPort; // LINEAR S5: the Linear gap channel of a project: the V2 canvas (Gaps stage)
}

/** In-memory hand-over between screens (§1.5);  */
export interface UiHandoff {
  setSpecBotPrefill(projectId: string, text: string): void;
  takeSpecBotPrefill(projectId: string): string | null;
}
