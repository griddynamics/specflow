// docs/github/projects.md, docs/mission-control/integrations-api.md "GitHub Projects": a project's plan exported to an organization project on
// GitHub (stories as issues, tasks as sub-issues). Screens reach it only through `GitHubProjectsPort`.

export type GitHubSyncStatus = "idle" | "running" | "succeeded" | "partial" | "failed" | "skipped";

/** What the last pass did, per item (`counts` is null until a pass finished). */
export interface GitHubProjectCounts {
  created: number;
  updated: number;
  commented: number;
  removed: number;
  ranked: number;
  unchanged: number;
  failed: number;
}

export interface GitHubProjectSync {
  status: GitHubSyncStatus;
  queued: boolean;
  finishedAt: string | null;
  errorCode: string | null;
  counts: GitHubProjectCounts | null;
}

/** `GET` and `PUT /projects/{pid}/github-projects`. `available` false = GitHub is off on the server (everything else is empty). */
export interface GitHubProjects {
  available: boolean;
  repositoryId: string | null;
  repository: string | null;
  projectNodeId: string | null;
  projectTitle: string | null;
  projectUrl: string | null;
  enabled: boolean;
  connectedByName: string | null;
  connectedByMe: boolean;
  sync: GitHubProjectSync | null;
}

/** An open project of the repository's organization. */
export interface GitHubProjectChoice {
  nodeId: string;
  number: number;
  title: string;
  url: string;
}

/** `organization` is null (and no project listed) when the repository belongs to a user: only organization projects can be used. */
export interface GitHubProjectList {
  organization: string | null;
  projects: GitHubProjectChoice[];
}

/** Exactly one of an existing project and a new project's title (created in the repository's organization when it is saved). */
export type GitHubProjectPick = { nodeId: string } | { newTitle: string };

export interface GitHubProjectsTarget {
  repositoryId: string;
  project: GitHubProjectPick;
}

export type GitHubProjectsField = "repository" | "project";

/** A failed check is an answer, not an error: `field` names the input to fix (null = none). */
export type GitHubProjectsAccess =
  | { result: "ok"; organization: string; projectTitle: string | null }
  | { result: "failed"; reason: string; field: GitHubProjectsField | null };

export interface GitHubProjectsPort {
  get(projectId: string): Promise<GitHubProjects>;
  /** The open projects of the organization that owns the repository (a repository of the project). */
  projects(projectId: string, repositoryId: string): Promise<GitHubProjectList>;
  /** Any member: turning it on (or another repository or project) starts a pass in the background. */
  save(projectId: string, input: GitHubProjectsTarget & { enabled: boolean }): Promise<GitHubProjects>;
  /** Any member: runs as the member GitHub is connected by. */
  checkAccess(projectId: string, target: GitHubProjectsTarget): Promise<GitHubProjectsAccess>;
}
