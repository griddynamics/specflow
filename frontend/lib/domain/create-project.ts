// FE-DATA-01 (TECH-SPECS §2.5). Owner decision 2026-10-15 (Mission Control sources redesign): the form asks only for the name and
// the contributors; sources are added on Mission Control. Production still sends the name only (A9, §3.3).
import type { Project } from "./projects";

export interface CreateProjectInput {
  name: string; // validated with validateProjectName
  contributorIds: string[]; // UI only: no backend field yet (A9)
}

export interface CreateProjectPort {
  create(input: CreateProjectInput): Promise<Project>; // production sends input.name only (A9, §3.3)
}
