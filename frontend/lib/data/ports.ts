// FE-DATA-01 (TECH-SPECS §2.14). Screens reach data only through these interfaces (R7).
import type { AssistantPort } from "@/lib/domain/assistant";
import type { CommentsPort } from "@/lib/domain/comments";
import type { ConnectorsPort } from "@/lib/domain/connectors";
import type { CreateProjectPort } from "@/lib/domain/create-project";
import type { DiagramsPort } from "@/lib/domain/diagrams";
import type { FilesPort } from "@/lib/domain/files";
import type { GapsPort } from "@/lib/domain/gaps";
import type { JiraPort } from "@/lib/domain/jira";
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
  slack: SlackPort; // the Slack channel of a project: Settings (Slack tab)
}

/** In-memory hand-over between screens (§1.5);  */
export interface UiHandoff {
  setSpecBotPrefill(projectId: string, text: string): void;
  takeSpecBotPrefill(projectId: string): string | null;
}
