// FE-BP-02, FE-BP-08, FE-DATA-01 (TECH-SPECS §3.1, §3.3): the http adapter for the production tree. It serves what the backend
// endpoints can serve (`GET /me`, projects, uploads, the sources API of GDRIVE plan 04, the plan, the gaps); every other port stays mock
// (`lib/data/compose.ts`). All calls go through `lib/api.ts` (browser -> `/api/backend/*` -> API `/api/v1/*`), except `GET /me`
// (`client.ts`, G01-10). No fixture is imported here (R7).
import type { BoxPort } from "@/lib/domain/box";
import type { DropboxPort } from "@/lib/domain/dropbox";
import type { GapsPort } from "@/lib/domain/gaps";
import type { GitHubPort } from "@/lib/domain/github";
import type { IntegrationsPort } from "@/lib/domain/integrations";
import type { JiraPort } from "@/lib/domain/jira";
import type { LinearPort } from "@/lib/domain/linear";
import type { LinearGapsPort } from "@/lib/domain/linear-gaps";
import type { LinearPlanPort } from "@/lib/domain/linear-plan";
import type { LinearSourcePort } from "@/lib/domain/linear-source";
import type { CreateProjectPort } from "@/lib/domain/create-project";
import type { MembersPort } from "@/lib/domain/members";
import type { PeoplePort } from "@/lib/domain/people";
import type { MissionControlPort } from "@/lib/domain/pipeline";
import type { PlanPort } from "@/lib/domain/plan";
import type { ProjectsPort } from "@/lib/domain/projects";
import type { GitHubProjectsPort } from "@/lib/domain/github-projects";
import type { JiraGapsPort } from "@/lib/domain/jira-gaps";
import type { NotionConnectionPort } from "@/lib/domain/notion-connection";
import type { NotionPlanPort } from "@/lib/domain/notion-plan";
import type { SlackPort } from "@/lib/domain/slack";
import type { KnowledgePort, SourcesPort } from "@/lib/domain/sources";
import { createHttpBox } from "./box";
import { createHttpDropbox } from "./dropbox";
import { createHttpGaps } from "./gaps";
import { createHttpGitHub } from "./github";
import { createHttpIntegrations } from "./integrations";
import { createHttpJira } from "./jira";
import { createHttpLinear } from "./linear";
import { createHttpLinearGaps } from "./linear-gaps";
import { createHttpLinearPlan } from "./linear-plan";
import { createHttpLinearSource } from "./linear-source";
import { createHttpKnowledge } from "./knowledge";
import { createHttpMembers } from "./members";
import { createHttpPeople } from "./me";
import { createHttpPlan } from "./plan";
import { createHttpProjects } from "./projects";
import { createHttpGitHubProjects } from "./github-projects";
import { createHttpJiraGaps } from "./jira-gaps";
import { createHttpNotionConnection } from "./notion-connection";
import { createHttpNotionPlan } from "./notion-plan";
import { createHttpSlack } from "./slack";
import { createHttpSources } from "./sources";

export interface HttpPorts {
  people: Pick<PeoplePort, "me">;
  projects: ProjectsPort;
  createProject: Pick<CreateProjectPort, "create">;
  /** Only the Ready gate of Plan and Gaps (`snapshot`); the browser-driven upload run is gone (K5). */
  missionControl: MissionControlPort;
  sources: SourcesPort;
  knowledge: KnowledgePort;
  plan: PlanPort;
  gaps: GapsPort;
  members: MembersPort;
  jira: JiraPort;
  githubProjects: GitHubProjectsPort;
  notionPlan: NotionPlanPort;
  notionConnection: NotionConnectionPort;
  jiraGaps: JiraGapsPort;
  slack: SlackPort;
  github: GitHubPort;
  box: BoxPort;
  dropbox: DropboxPort;
  integrations: IntegrationsPort;
  linear: LinearPort; // LINEAR S1
  linearPlan: LinearPlanPort; // LINEAR S3
  linearSource: LinearSourcePort; // LINEAR S4
  linearGaps: LinearGapsPort; // LINEAR S5
}

/** TECH-SPECS §3.1. `pollMs` and `idleMs` are constructor options for the stub-API tests; production uses the defaults. */
export function createHttpAdapter(opts?: { pollMs?: number; idleMs?: number; planPollMs?: number }): HttpPorts {
  const { projects, createProject, setStatus } = createHttpProjects();
  const { knowledge, missionControl, changes } = createHttpKnowledge({ onStatus: setStatus, pollMs: opts?.pollMs, idleMs: opts?.idleMs });
  return {
    people: createHttpPeople(),
    projects,
    createProject,
    missionControl,
    sources: createHttpSources({
      changed: (projectId) => {
        changes.notify(projectId);
      },
    }),
    knowledge,
    plan: createHttpPlan({ pollMs: opts?.planPollMs }),
    gaps: createHttpGaps(),
    members: createHttpMembers(),
    jira: createHttpJira(),
    githubProjects: createHttpGitHubProjects(),
    notionPlan: createHttpNotionPlan(),
    notionConnection: createHttpNotionConnection(),
    jiraGaps: createHttpJiraGaps(),
    slack: createHttpSlack(),
    github: createHttpGitHub(),
    box: createHttpBox(),
    dropbox: createHttpDropbox(),
    integrations: createHttpIntegrations(),
    linear: createHttpLinear(), // LINEAR S1
    linearPlan: createHttpLinearPlan(), // LINEAR S3
    linearSource: createHttpLinearSource(), // LINEAR S4
    linearGaps: createHttpLinearGaps(), // LINEAR S5
  };
}
