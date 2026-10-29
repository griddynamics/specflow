// FE-BP-02, FE-BP-08, FE-DATA-01 (TECH-SPECS §3.1, §3.3): the http adapter for the production tree. It serves what the backend
// endpoints can serve (`GET /me`, projects, uploads, the sources API of GDRIVE plan 04); every other port stays mock
// (`lib/data/compose.ts`). All calls go through `lib/api.ts` (browser -> `/api/backend/*` -> API `/api/v1/*`), except `GET /me`
// (`client.ts`, G01-10). No fixture is imported here (R7).
import type { CreateProjectPort } from "@/lib/domain/create-project";
import type { PeoplePort } from "@/lib/domain/people";
import type { MissionControlPort } from "@/lib/domain/pipeline";
import type { ProjectsPort } from "@/lib/domain/projects";
import type { KnowledgePort, SourcesPort } from "@/lib/domain/sources";
import { createHttpKnowledge } from "./knowledge";
import { createHttpPeople } from "./me";
import { createHttpProjects } from "./projects";
import { createHttpSources } from "./sources";

export interface HttpPorts {
  people: Pick<PeoplePort, "me">;
  projects: ProjectsPort;
  createProject: Pick<CreateProjectPort, "create">;
  /** Only the Ready gate of Plan and Decisions (`snapshot`); the browser-driven upload run is gone (K5). */
  missionControl: MissionControlPort;
  sources: SourcesPort;
  knowledge: KnowledgePort;
}

/** TECH-SPECS §3.1. `pollMs` and `idleMs` are constructor options for the stub-API tests; production uses the defaults. */
export function createHttpAdapter(opts?: { pollMs?: number; idleMs?: number }): HttpPorts {
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
  };
}
