// FE-BP-02.AC1-AC4, AC7 (TECH-SPECS §2.4, §2.5, §3.3, §3.5; A9): projects through `lib/api.ts`.
// list = `GET /projects?limit=100` (newest first, `next_cursor` ignored: known limit), get = `GET /projects/{pid}`,
// create = `POST /projects` with the NAME ONLY (A9: contributors, links and org switches stay UI-only; the backend forbids
// extra fields). The list endpoint has no document state, so every project is `status: "unknown"` until Mission Control
// reads its documents (§3.5).
import { createProject, getProject, listProjects } from "@/lib/api";
import type { Unsubscribe } from "@/lib/domain/common";
import type { CreateProjectPort } from "@/lib/domain/create-project";
import { DataError } from "@/lib/domain/errors";
import { PROJECT_NAME_MAX, type Project, type ProjectStatus, type ProjectsPort } from "@/lib/domain/projects";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

/** Pure: the API project -> the domain Project (TECH-SPECS §3.5). A body of another shape throws `invalid_response` (QA B1). */
export function toProject(p: unknown): Project {
  return checked(() => {
    expectShape(isRecord(p) && isString(p.id) && p.id !== "" && isString(p.name) && isString(p.created_at));
    return { id: p.id, name: p.name, createdAt: p.created_at, status: "unknown" };
  });
}

/**
 * The backend's name rule (`schemas.py:27-38`): trimmed, 1-200 characters counted as code points (Python `len`), so 150 emoji
 * pass and 201 code points fail. `validateProjectName` (frozen) counts UTF-16 units; the adapter uses the backend count.
 */
export function backendName(raw: string): string | null {
  const name = raw.trim();
  const length = Array.from(name).length;
  return length >= 1 && length <= PROJECT_NAME_MAX ? name : null;
}

export interface HttpProjects {
  projects: ProjectsPort;
  createProject: Pick<CreateProjectPort, "create">;
  /** Mission Control's reading of a project's status (TECH-SPECS §3.5); the list then shows it instead of "unknown". */
  setStatus: (projectId: string, status: ProjectStatus) => void;
}

export function createHttpProjects(): HttpProjects {
  const listeners = new Set<() => void>();
  const changed = () => {
    listeners.forEach((l) => {
      l();
    });
  };
  const statuses = new Map<string, ProjectStatus>(); // only what Mission Control read in this session
  const withStatus = (p: Project): Project => ({ ...p, status: statuses.get(p.id) ?? p.status });

  const projects: ProjectsPort = {
    async list() {
      const page: unknown = await guard(() => listProjects());
      return checked(() => {
        expectShape(isRecord(page) && Array.isArray(page.items));
        return (page.items as unknown[]).map((p) => withStatus(toProject(p)));
      });
    },
    get: async (projectId) => withStatus(toProject(await guard(() => getProject(projectId)))),
    subscribe(listener): Unsubscribe {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };

  const createPort: Pick<CreateProjectPort, "create"> = {
    async create(input) {
      // FE-BP-02.AC2 (A9): only the name is sent; the same name rule as the backend (trim, 1-200 code points), checked first.
      const name = backendName(input.name);
      if (name === null) throw new DataError("validation", false);
      const created = toProject(await guard(() => createProject(name)));
      changed(); // the switcher reads the list again
      return created;
    },
  };

  function setStatus(projectId: string, status: ProjectStatus): void {
    if (statuses.get(projectId) === status) return;
    statuses.set(projectId, status);
    changed(); // ProjectsPort.subscribe: "a status changed"
  }

  return { projects, createProject: createPort, setStatus };
}
