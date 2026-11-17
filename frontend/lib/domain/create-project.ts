// FE-DATA-01 (TECH-SPECS §2.5). The form asks for the name and the collaborators (by email); sources are added on Mission Control.
// Production sends `{name}`, or `{name, collaborators}` when at least one email was given (docs/projects/api.md#create-with-collaborators).
import type { Project } from "./projects";

export interface CreateProjectInput {
  name: string; // validated with validateProjectName
  collaboratorEmails?: string[]; // the Collaborators field; sent only when non-empty
}

export interface CreateProjectPort {
  create(input: CreateProjectInput): Promise<Project>; // production sends the name and, when non-empty, the emails
}
