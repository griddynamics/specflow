// FE-BP-02, FE-BP-08, FE-DATA-01 (TECH-SPECS §3.1, §3.3): the http adapter for the production tree. It serves what the backend
// endpoints can serve (`GET /me`, projects, uploads, the sources API of GDRIVE plan 04, the plan, the gaps); every other port stays mock
// (`lib/data/compose.ts`). All calls go through `lib/api.ts` (browser -> `/api/backend/*` -> API `/api/v1/*`), except `GET /me`
// (`client.ts`, G01-10). No fixture is imported here (R7).
import type { BoxPort } from "@/lib/domain/box";
import type { GapsPort } from "@/lib/domain/gaps";
import type { GitHubPort } from "@/lib/domain/github";
import type { IntegrationsPort } from "@/lib/domain/integrations";
import type { JiraPort } from "@/lib/domain/jira";
import type { CreateProjectPort } from "@/lib/domain/create-project";
import type { MembersPort } from "@/lib/domain/members";
import type { PeoplePort } from "@/lib/domain/people";
import type { MissionControlPort } from "@/lib/domain/pipeline";
import type { PlanPort } from "@/lib/domain/plan";
import type { ProjectsPort } from "@/lib/domain/projects";
import type { GitHubProjectsPort } from "@/lib/domain/github-projects";
import type { JiraGapsPort } from "@/lib/domain/jira-gaps";
import type { SlackPort } from "@/lib/domain/slack";
import type { WorkPort } from "@/lib/domain/work";
import type { KnowledgePort, SourcesPort } from "@/lib/domain/sources";
import { createHttpBox } from "./box";
import { createHttpGaps } from "./gaps";
import { createHttpGitHub } from "./github";
import { createHttpIntegrations } from "./integrations";
import { createHttpJira } from "./jira";
import { createHttpKnowledge } from "./knowledge";
import { createHttpMembers } from "./members";
import { createHttpPeople } from "./me";
import { createHttpPlan } from "./plan";
import { createHttpProjects } from "./projects";
import { createHttpGitHubProjects } from "./github-projects";
import { createHttpJiraGaps } from "./jira-gaps";
import { createHttpSlack } from "./slack";
import { createHttpSources } from "./sources";
import { createHttpWork } from "./work";

export interface HttpPorts {
  people: Pick<PeoplePort, "me">;
  projects: ProjectsPort;
  createProject: Pick<CreateProjectPort, "create">;
  /** Only the Ready gate of Plan and Gaps (`snapshot`); the browser-driven upload run is gone (K5). */
  missionControl: MissionControlPort;
  sources: SourcesPort;
  knowledge: KnowledgePort;
  plan: PlanPort;
  work: WorkPort;
  gaps: GapsPort;
  members: MembersPort;
  jira: JiraPort;
  githubProjects: GitHubProjectsPort;
  jiraGaps: JiraGapsPort;
  slack: SlackPort;
  github: GitHubPort;
  box: BoxPort;
  integrations: IntegrationsPort;
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
    work: createHttpWork(),
    gaps: createHttpGaps(),
    members: createHttpMembers(),
    jira: createHttpJira(),
    githubProjects: createHttpGitHubProjects(),
    jiraGaps: createHttpJiraGaps(),
    slack: createHttpSlack(),
    github: createHttpGitHub(),
    box: createHttpBox(),
    integrations: createHttpIntegrations(),
  };
}
