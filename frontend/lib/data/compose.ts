// FE-DATA-01, FE-BP-02, FE-BP-08, SPL-DEMO-01: the production port bundle. http serves what the backend has (`people.me`, `projects`,
// `createProject.create`, the Ready gate `missionControl`, `sources`, `knowledge`); `unavailable` answers empty for everything else
// (decisions, connectors, plan, assistant, files, comments, diagrams, the people directory and mentions). No fixture is read.
import type { HttpPorts } from "./http";
import type { DataPorts } from "./ports";
import { unavailable, unavailableIntro } from "./unavailable";

export function composeProductionPorts(http: HttpPorts): DataPorts {
  return {
    ...unavailable,
    people: {
      me: () => http.people.me(), // GET /me (FE-PPL-01.AC3, FE-BP-02.AC8)
      directory: () => Promise.resolve([]), // no users endpoint: no suggestions
      mentionables: () => Promise.resolve([]),
    },
    projects: http.projects,
    createProject: {
      create: (input) => http.createProject.create(input), // POST /projects with the name only (A9)
    },
    missionControl: http.missionControl, // the Ready gate only (>= 1 processed document)
    sources: http.sources, // Drive folders, Local files; no GitHub
    knowledge: http.knowledge,
    assistant: {
      ...unavailable.assistant,
      // The SpecBot greeting names the signed-in user (GET /me). If /me fails, the greeting has no name.
      async intro() {
        try {
          return unavailableIntro(firstName((await http.people.me()).name));
        } catch {
          return unavailableIntro("");
        }
      },
    },
  };
}

const firstName = (name: string) => name.trim().split(/\s+/)[0] ?? "";
